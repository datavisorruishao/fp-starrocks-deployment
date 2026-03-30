# CRE-6630 接口梳理 — 已搞清楚 vs 还没搞清楚

---

## ✅ 已搞清楚的

### 1. Dimension CRUD

```
POST /{tenant}/dimension
Body:     { name, type: "expression"|"column", expr: "CASE WHEN country='CN' THEN 'China' ..." }
Response: Dimension 对象
```

- 创建时校验表达式合法性，写入 MySQL `dimension` 表
- 逻辑完整，无歧义

---

### 2. Cube 创建

```
POST /{tenant}/cube
Body: {
  name: "Country Stats",
  dimension_id: 1,
  target_features: ["txn_amount", "age"],
  measures: ["count", "sum", "p95", "distinct_count"]
}
Response: Cube（含每个 MV 的初始状态）
```

- fp-async 收到请求后，对每个 target_feature 生成 DDL，提交 StarRocks `CREATE MATERIALIZED VIEW`
- MV 命名规则确定：`mv_{tenant}_{dim_name}_{feature}`
- MV 创建是异步的（StarRocks 开始调度刷新），接口立即返回，不阻塞

---

### 3. Measure → StarRocks 函数映射（硬编码在 fp-async）

| 用户 Measure | MV 中存什么 | 窗口查询合并 |
|-------------|-----------|------------|
| `count` | `count(col) AS cnt` | `sum(cnt)` |
| `sum` | `sum(col) AS sum_val` | `sum(sum_val)` |
| `min` | `min(col) AS min_val` | `min(min_val)` |
| `max` | `max(col) AS max_val` | `max(max_val)` |
| `mean` | 复用 `sum_val` + `cnt` | `sum(sum_val) / sum(cnt)` |
| `std` | 额外 `sum(col*col) AS sum_sq` | `sqrt(sum(sum_sq)/sum(cnt) - pow(sum(sum_val)/sum(cnt), 2))` |
| `p50/p90/p95/p99` | `percentile_union(percentile_hash(col)) AS pct_state` | `percentile_approx_raw(percentile_union(pct_state), 0.95)` |
| `distinct_count` | `hll_union(hll_hash(col)) AS hll_state` | `hll_union_agg(hll_state)` |
| `missing_count` | `count_if(col IS NULL) AS missing_cnt` | `sum(missing_cnt)` |

不需要数据库存映射，新增 Measure 只需扩展映射表 + 代码发布。

---

### 4. MV DDL 生成（完整示例）

用户提交 Cube 后，fp-async 对每个 target_feature 生成如下 DDL：

```sql
CREATE MATERIALIZED VIEW mv_sofi_country_txn_amount
PARTITION BY (stat_date)
DISTRIBUTED BY HASH(segment_value)
REFRESH ASYNC EVERY (INTERVAL 1 DAY)
PROPERTIES ("partition_refresh_number" = "1")
AS
SELECT
    'sofi'                                        AS tenant,
    date_trunc('day', event_time)                 AS stat_date,
    CASE WHEN country='CN' THEN 'China'
         WHEN country='US' THEN 'US'
         ELSE 'Other' END                         AS segment_value,
    count(txn_amount)                             AS cnt,
    sum(txn_amount)                               AS sum_val,
    max(txn_amount)                               AS max_val,
    percentile_union(percentile_hash(txn_amount)) AS pct_state,
    hll_union(hll_hash(txn_amount))               AS hll_state
FROM iceberg_catalog.db.raw_events
WHERE tenant = 'sofi'
GROUP BY tenant, stat_date, segment_value;
```

---

### 5. Cube 修改/删除规则

| 操作 | 处理方式 |
|------|---------|
| 修改 Dimension expr | DROP MV → 重建（新 expr 需要全量重算） |
| 新增 target_feature | CREATE 新 MV（不影响已有 MV） |
| 新增 Measure | DROP MV → 重建（需要新增聚合列） |
| 删除 Cube | DROP 所有关联 MV |
| 删除单个 Feature | DROP 对应 MV |

---

### 6. MySQL 数据模型（三张表）

```sql
-- 维度定义
CREATE TABLE dimension (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant       VARCHAR(128) NOT NULL,
    name         VARCHAR(256) NOT NULL,
    type         VARCHAR(32)  NOT NULL,  -- EXPRESSION / COLUMN
    expr         TEXT         NOT NULL,  -- CASE WHEN 表达式 或 列名
    status       VARCHAR(32)  NOT NULL DEFAULT 'ACTIVE',
    creator      VARCHAR(128),
    create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_tenant_name (tenant, name)
);

-- 分析视角
CREATE TABLE cube_config (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant          VARCHAR(128) NOT NULL,
    dimension_id    BIGINT       NOT NULL,
    name            VARCHAR(256) NOT NULL,
    target_features TEXT         NOT NULL,  -- JSON: ["txn_amount", "age"]
    measures        TEXT         NOT NULL,  -- JSON: ["count", "sum", "p95"]
    status          VARCHAR(32)  NOT NULL DEFAULT 'ACTIVE',
    creator         VARCHAR(128),
    create_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (dimension_id) REFERENCES dimension(id),
    UNIQUE KEY uk_tenant_name (tenant, name)
);

-- 每个 Cube × Feature 对应一个 StarRocks MV
CREATE TABLE cube_mv (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    cube_id       BIGINT        NOT NULL,
    feature_name  VARCHAR(512)  NOT NULL,
    mv_name       VARCHAR(512)  NOT NULL,  -- mv_sofi_country_txn_amount
    mv_ddl        TEXT          NOT NULL,  -- 生成的 DDL 留档
    status        VARCHAR(32)   NOT NULL DEFAULT 'CREATING',  -- CREATING / ACTIVE / ERROR
    last_refresh  DATETIME,
    error_message TEXT,
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cube_id) REFERENCES cube_config(id)
);
```

---

### 7. Partial Window 查询逻辑

MV 数据不足完整窗口时（新 Cube 刚创建、新 tenant 刚上线），查询仍然可以返回，但附带覆盖度信息：

```sql
SELECT
    segment_value,
    sum(cnt)                              AS total_count,
    count(DISTINCT stat_date)             AS actual_days,
    count(DISTINCT stat_date) / 30.0      AS coverage
FROM mv_sofi_country_txn_amount
WHERE stat_date BETWEEN current_date - INTERVAL 30 DAY AND current_date
GROUP BY segment_value;
```

应用层根据 `coverage` 标记为 `COMPLETE` 或 `PARTIAL`，前端展示"基于 N 天数据计算"。

---

### 8. 查询改写路径（fp-async 侧无感知）

fp-async 发 SQL 查 `raw_events`，FE CBO 透明改写为查 MV，应用代码不需要感知 MV 存在：

```
fp-async:  SELECT ... FROM raw_events WHERE tenant='sofi' AND event_time BETWEEN ...
               ↓ StarRocks FE CBO 自动改写
StarRocks: SELECT ... FROM mv_sofi_country_txn_amount WHERE stat_date BETWEEN ...
               ↓ CN Query 合并 N 个 daily partition（毫秒级）
fp-async:  拿到聚合结果，封装成 Stats API response 返回
```

---

## ❌ 还没搞清楚的

### 1. Stats API 的具体接口（最关键缺口）

系统设计文档里只有一行示意：

```
GET /sofi/stats?cube=Country%20Stats&window=30d
```

**没有文档化的**：

| 问题 | 说明 |
|------|------|
| `cube` 参数用 id 还是 name？ | 两种都能唯一定位，但接口规范要定死 |
| `window` 支持哪些值？ | 只有 30d/90d/180d？还是任意天数？ |
| 是否支持一次查多个 features？ | `features=["txn_amount","age"]` 还是每次只能查一个？ |
| 是否支持额外 filter？ | 比如 `event_type=transaction`，还是只能按 Dimension 分组 |
| Response 的 JSON schema？ | `coverage`、`actual_days`、`segment_value` 怎么组织？ |
| 错误码规范？ | MV 还在 CREATING 时返回什么？StarRocks 不可达时降级逻辑？ |

---

### 2. Cube 状态查询 API

MV 创建是异步的，用户创建 Cube 后怎么知道 MV ready 了？

- 需不需要 `GET /{tenant}/cube/{id}` 查状态接口？
- `cube_mv.status` 的状态机：`CREATING → ACTIVE / ERROR`，fp-async 怎么感知 StarRocks 那边的刷新状态并更新这张表？（轮询？回调？）
- 文档没有描述这个闭环。

---

### 3. Dimension 列引用型（type=column）的完整行为

文档描述了 `expression` 类型（CASE WHEN），对 `column` 类型只提了"直接列引用"，未明确：

- DDL 生成时 `segment_value` 就是列的原始值吗？
- 这个列名是 FP feature name 还是 Iceberg raw_events 的列名？两者理论上一一对应，但需要确认。

---

### 4. target_features 的列名来源与校验时机

`target_features: ["txn_amount", "age"]` 这里的名字：

- 是 FP 里的 feature name？还是 Iceberg `raw_events` 的列名？（理论上是同一个，但需要确认）
- 如果 feature 在 FP 里存在，但 SMT 还没同步到 Iceberg schema（新 feature，尚未有数据），会怎样？
- **校验时机**：在 `POST /cube` 时校验并报错？还是 MV 刷新时失败写入 `cube_mv.error_message`？

---

### 5. 多租户 raw_events 表结构（影响 MV DDL 的 FROM 子句）

两种方案，还没有最终结论：

| 方案 | Iceberg 表结构 | MV DDL FROM 子句 |
|------|--------------|----------------|
| 共享表 + tenant 分区 | `raw_events` PARTITIONED BY (tenant, day) | `FROM iceberg_catalog.db.raw_events WHERE tenant='sofi'` |
| 每个 tenant 独立表 | `{tenant}.event_result`（当前 dev_a 是这样） | `FROM iceberg_catalog.qaautotest.event_result` |

**当前 dev_a 部署用的是独立表**（`qaautotest.event_result`），系统设计文档写的是共享表。两者不一致，需要确认生产方案。

---

### 6. MV 刷新频率是否可配

系统设计写的是：

```sql
REFRESH ASYNC EVERY (INTERVAL 1 DAY)
```

但 TODO.md 里有未关闭的待确认项：

> MV 刷新频率确认（daily 还是 hourly？）

如果改成 hourly，查询延迟从"次日凌晨后可查"降到"1 小时内可查"，但 CN Refresh 资源消耗增加约 24 倍。这个决定没有最终结论。

---

## 优先级建议

| 缺口 | 为什么优先 |
|------|----------|
| Stats API request/response schema | Dev 开始写代码前必须定死，否则前后端对不上 |
| Cube 状态查询 API | MV 创建异步，没有这个接口 UI 没法展示进度 |
| 多租户 raw_events 表结构 | 直接决定 MV DDL 的 FROM 子句，影响所有已部署配置 |
| target_features 校验时机 | 影响错误处理路径和用户体验 |
| MV 刷新频率 | 影响资源规划和 CN Refresh CronJob 配置 |
