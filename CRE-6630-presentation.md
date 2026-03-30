# CRE-6630 Feature Stats — Presentation

> 目标：给 team 完整讲清楚这个项目的背景、分工、部署内容、数据流、验证方式和后续计划。

---

## 1. 需求是什么（Problem Statement）

### 1.1 业务背景

客户（如 SoFi）需要对历史事件数据做**多维度、多时间窗口的聚合统计**，也就是 OLAP 分析能力：

- 按照自定义**维度**分组（如按国家、按 IP 段）
- 对某个**特征列**做聚合（如 `txn_amount`、`age`）
- 在 **30d / 90d / 180d** 等时间窗口上返回 count / sum / mean / p95 / distinct_count

### 1.2 为什么现有 ClickHouse 不够用

| 能力 | ClickHouse（现有） | StarRocks + Iceberg（新） |
|------|--------------------|--------------------------|
| 存事件明细 | ✅ | ✅ |
| 按固定 schema 查询 | ✅ | ✅ |
| 自定义 CASE WHEN 维度表达式 | ❌ 难以动态定义 | ✅ 通过 Cube 配置生成 MV |
| 30d/90d/180d 大窗口聚合 | ❌ 全表扫，慢 | ✅ MV 预聚合，毫秒级 |
| 列式 Parquet + S3 存储成本 | ❌ 自有存储 | ✅ Iceberg on S3，按量付费 |
| 弹性扩缩容 | ❌ 静态部署 | ✅ CN 无状态，可按需水平扩展 |

**一句话**：ClickHouse 适合固定 schema 的事件查询，但支撑不了"用户自定义维度 + 大时间窗口预聚合"这个场景。

### 1.3 用户侧的使用方式

用户通过配置抽象定义"想看什么统计视角"，系统自动处理底层数据：

**Step 1 — 创建 Dimension（分组轴）**
```json
POST /sofi/dimension
{
  "name": "By Country",
  "type": "expression",
  "expr": "CASE WHEN country='CN' THEN 'China' WHEN country='US' THEN 'United States' ELSE 'Other' END"
}
```

**Step 2 — 创建 Cube（选维度 + 度量 + 目标特征列）**
```json
POST /sofi/cube
{
  "name": "Country Stats",
  "dimension_id": 1,
  "target_features": ["txn_amount", "age"],
  "measures": ["count", "sum", "mean", "p95", "distinct_count"]
}
```

**Step 3 — 查询（按窗口返回聚合结果）**
```
GET /sofi/stats?cube=Country%20Stats&window=30d

返回:
  segment_value=China:        count=8000, sum=2400000, mean=300, p95=1200
  segment_value=United States: count=5000, sum=1500000, mean=300, p95=950
  segment_value=Other:         count=1000, sum=300000,  mean=300, p95=800
```

---

## 2. 分工（谁做什么）

| 角色 | 负责内容 | 状态 |
|------|---------|------|
| **你（Infra）** | 部署 5 个基础设施组件：Iceberg + Kafka Connect + StarRocks FE+CN。验证数据能写进 S3、StarRocks 能查到 | ✅ dev_a 验证完成 |
| **fp-async 团队（Dev）** | Cube/Dimension CRUD API；根据 Cube 生成 MV DDL 并提交到 StarRocks；实现 Stats 查询接口（连 StarRocks JDBC） | ❌ 待开发 |

**你做这件事的目的**：证明基础设施链路 end-to-end 可行。数据从 Kafka 流进来，经 SMT 转换，落到 S3 Iceberg 格式，StarRocks 能查到——这是 Dev 后续写代码的必要前提，不是终点。

---

## 3. 部署了哪些东西（5 个组件）

| # | 组件 | K8s 类型 | Service | 镜像 |
|---|------|---------|---------|------|
| 1 | Iceberg Catalog MySQL | StatefulSet (1) + PVC | `iceberg-catalog-mysql:3306` | `mysql:8.0` |
| 2 | Iceberg REST Catalog | Deployment (1) | `iceberg-rest-catalog:8181` | `tabulario/iceberg-rest:1.6.0` |
| 3 | Kafka Connect + SMT | Deployment (1) | `iceberg-kafka-connect:8083` | `confluentinc/cp-kafka-connect-base:7.7.1`（含自定义 SMT jar） |
| 4 | StarRocks FE | StatefulSet (1) + PVC | `starrocks-fe:9030` | `starrocks/fe-ubuntu:3.3-latest (v3.3.22)` |
| 5 | StarRocks CN | Deployment (1) | `starrocks-cn:9050` | `starrocks/cn-ubuntu:3.3-latest (v3.3.22)` |

**外部依赖（已有，不动）**：

| 依赖 | 地址 | 用途 |
|------|------|------|
| Kafka | `kafka3.duckdb:9092` (3 brokers) | Kafka Connect 消费 `velocity-al` topic |
| FP MySQL | `fp-mysql.duckdb:3306` | SMT 查 feature 表做 ID→name 映射（`dv.ro` 只读） |
| S3 Bucket | `datavisor-dev-us-west-2-iceberg` | Kafka Connect 写 Parquet；StarRocks 读 Parquet |
| fp-async | `fp-async.duckdb:8080` | 产生 velocity-al 消息，是我们的数据源头 |

---

## 4. 这些东西怎么串起来工作

### 4.1 总架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                      已有链路（不变）                            │
│                                                                 │
│  客户请求 → FP API → Kafka: velocity → fp-async Consumer        │
│                                  ├──→ YugabyteDB（实时聚合）    │
│                                  └──→ Kafka: velocity-al ───┐  │
└──────────────────────────────────────────────────────────────┼──┘
                                                               │
                    ┌──────────────────────────────────────────┘
                    │  velocity-al topic（我们接管的起点）
                    ↓
         ┌─────────────────────┐      ┌─────────────────────────┐
         │ ConsumerForCH（已有）│      │ Kafka Connect（新增）    │
         │ 写 ClickHouse        │      │ + SMT 转换               │
         └─────────────────────┘      └──────────┬──────────────┘
                                                  │ 写 Parquet（每10min一批）
                                                  ↓
                                    ┌─────────────────────────────┐
                                    │ S3: Parquet（Iceberg 格式）  │
                                    │ .../qaautotest/event_result/ │
                                    └──────────┬──────────────────┘
                                               │ commit snapshot
                                               ↓
                                    ┌─────────────────────────────┐
                                    │ Iceberg REST Catalog (:8181) │
                                    │ → Iceberg Catalog MySQL      │
                                    └──────────┬──────────────────┘
                                               │ External Catalog
                                               ↓
                              ┌────────────────────────────────────┐
                              │ StarRocks FE (:9030)               │
                              │  - 查询规划 / CBO 改写              │
                              │  - MV 刷新调度                     │
                              └──────────┬─────────────────────────┘
                                         │
                                         ↓
                              ┌────────────────────────────┐
                              │ StarRocks CN（单节点，Spot）│
                              │ 白天: 在线查询（读 MV）     │
                              │ 凌晨: MV refresh（扫 S3）   │
                              └────────────┬───────────────┘
                                           ↑
                                    fp-async Stats API
                                    (JDBC 9030)
```

### 4.2 数据流详细说明（每一跳发生了什么）

**跳 1：FP → Kafka velocity-al**

fp-async Consumer 处理完事件后，把每个 EventResult 序列化成 JSON 写到 `velocity-al` topic：

```json
{
  "eventId": "abc-123",
  "eventType": "transaction",
  "userId": "user_42",
  "eventTime": 1774597200000,
  "processingTime": 1774597201000,
  "featureMap": {
    "8":  299.99,
    "7":  "US",
    "15": "merchant_xyz"
  }
}
```

> **为什么 featureMap 是整数 ID？** 为了节省 Kafka 消息体积。一个事件有约 700 个 feature，如果每个 key 都是字符串名字，消息大 10 倍。整数 ID 是内部压缩格式。

**跳 2：Kafka Connect + SMT 转换**

SMT（`FeatureResolverTransform`）把 featureMap 的整数 ID 转成有名字、有类型的列：

```
输入（Kafka 消息）:  featureMap: {"8": 299.99, "7": "US"}
                                    ↓ SMT 查 FP MySQL 缓存（每 60s 刷新）
输出（Parquet 行）:  amount=299.99 (DOUBLE), country="US" (STRING), ...
```

Schema 是**动态的**：新增 feature 时 SMT 自动刷新缓存，Iceberg 支持 schema evolve（自动加列）。每个 tenant 约 700 个 feature 列。

**跳 3：Kafka Connect → S3（Iceberg 格式）**

每 10min 做一次 Iceberg commit，写 Parquet 数据文件到 S3，同时更新 Iceberg 元数据（snapshot + manifest）。

**跳 4：Iceberg Catalog → StarRocks**

StarRocks FE 通过 Iceberg External Catalog 发现新分区：

```sql
-- StarRocks 侧只需要配置一次
CREATE EXTERNAL CATALOG iceberg_catalog
PROPERTIES (
  "type" = "iceberg",
  "iceberg.catalog.type" = "rest",
  "iceberg.catalog.uri" = "http://iceberg-rest-catalog:8181"
);

-- 之后直接查
SELECT COUNT(*) FROM iceberg_catalog.qaautotest.event_result;
```

**跳 5（凌晨批处理）：CN 刷 MV**

```
同一个 CN 节点（凌晨无查询流量，独占全部资源）
扫 S3 新分区（约 1000 万行/天/租户）
→ GROUP BY (dimension × measure)
→ 写 MV 的 daily 分区（只有几百行/天，几 KB）
```

> **注**：不需要独立的 CN Refresh 节点。ToB 系统凌晨无用户查询，1 个 CN 同时承担查询和 refresh，天然串行无竞争（详见 design-qa D5）。

**跳 6（在线查询）：fp-async → FE → CN → 结果**

```
fp-async: SELECT country, sum(amount) FROM raw_events WHERE event_time > now()-30d
      ↓ FE CBO 透明改写
SELECT country, sum(amount) FROM mv_country_txn_amount
  WHERE stat_date BETWEEN today-30 AND today
      ↓ CN 合并 30 个 daily 分区
      ↓ 毫秒级返回
```

---

## 5. MV（物化视图）是怎么回事

### 5.1 MV 的本质

MV = **预计算 + 存结果**。把"每天的聚合结果"提前算好存起来，查询时只需合并 N 天的预聚合行，而不是扫原始的几亿行。

```
原始 raw_events（每天约 1000 万行）
         ↓ 凌晨 GROUP BY
MV daily 分区（每天约几百行，几 KB）

查询 30d:  合并 30 个分区 × 几百行 = 几秒内完成
查询 180d: 合并 180 个分区 × 几百行 = 仍然很快
```

### 5.2 MV 刷新节奏（类比 Cron）

可以把 Cube 类比为"配置并启用一个每天刷新的定时任务"：
- 创建 Cube → 后台生成 `CREATE MATERIALIZED VIEW ... REFRESH ASYNC EVERY (INTERVAL 1 DAY)`
- 每天凌晨同一个 CN 节点触发增量刷新（只算昨天新增的分区）
- 不是实时刷新，写入延迟约 10min（Kafka Connect commit interval）+ 凌晨刷 MV

### 5.3 第一次查询慢的问题（Cold Start）

| 状态 | 查询路径 | 速度 |
|------|---------|------|
| MV 尚未刷新（刚创建 Cube） | FE 回退扫 raw_events 原始 Parquet | 慢（秒～分钟） |
| MV 有部分天数数据 | FE CBO 改写，合并已有日分区 | 快，但返回 `coverage<100%` |
| MV 完整 | FE CBO 改写，合并全部日分区 | 毫秒级 |

**Fallback 的 latency 差距是数量级的**：MV 查询几十毫秒 vs 原始 Parquet 扫描几分钟到十几分钟。最常见的触发场景是**新 tenant onboard 时历史 MV 还没建好**。

**应对策略**：onboard 时立刻触发历史 MV 回填，MV ready 前前端显示"数据处理中"，ready 后开放查询。Scaling 不是 fallback 的解法，确保 MV 始终 ready 才是（详见附录 D13）。

---

## 6. 最终要达到的效果

### 阶段一：基础设施验证（已完成 ✅）

```
数据写到 S3 + StarRocks 能查到

验证命令:
SELECT COUNT(*) FROM iceberg_catalog.qaautotest.event_result;
→ 返回 35 行 ✅
```

### 阶段二：完整 OLAP 能力（后续）

```
fp-async 创建 MV:
  POST /sofi/cube → fp-async 生成 DDL → CREATE MATERIALIZED VIEW → StarRocks FE

fp-async Stats API 查询:
  GET /sofi/stats?cube=Country Stats&window=30d
  → fp-async (JDBC) → StarRocks FE (CBO 改写) → CN (读 MV)
  → 毫秒级返回:
     China:         count=8000, sum=2400000, p95=1200
     United States: count=5000, sum=1500000, p95=950
```

### 全链路最终效果图

```
[写入路径 — 实时，约 10min 延迟]
FP 处理事件 → fp-async → velocity-al → Kafka Connect → S3 Parquet

[刷新路径 — 凌晨批处理]
CN 扫 S3 昨日分区 → GROUP BY → 写 MV daily 分区（几百行/天）

[查询路径 — 毫秒级]
客户请求 Stats API → fp-async → FE (CBO 改写) → CN 合并 N 个 daily 分区 → 返回结果

[降级路径 — MV 未就绪时]
FE 自动回退 → CN 扫原始 Parquet（慢但数据最新）
```

---

## 7. 验证方式（逐层 Check）

验证逻辑：从数据入口到查询出口，每一跳都可以独立验证。

### ① Kafka 有数据
```bash
kubectl -n duckdb exec kafka3-0 -- bash -c "
  echo '=== earliest ===' && \
  kafka-get-offsets.sh --bootstrap-server localhost:9092 \
    --topic duckdb_fp_velocity-al-.qaautotest --time -2 && \
  echo '=== latest ===' && \
  kafka-get-offsets.sh --bootstrap-server localhost:9092 \
    --topic duckdb_fp_velocity-al-.qaautotest
"
# 期望: latest - earliest > 0
```

### ② Connector 运行中
```bash
kubectl -n duckdb exec <connect-pod> -- \
  curl -s http://localhost:8083/connectors/iceberg-sink-qaautotest/status
# 期望: connector.state=RUNNING, tasks[*].state=RUNNING
```

### ③ Consumer 追上了（LAG=0）
```bash
# 注意：consumer group 名不是你配的 CONNECT_GROUP_ID
# 而是 connect-<connector-name> 格式
kubectl -n duckdb exec kafka3-0 -- bash -c \
  "kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
   --describe --group connect-iceberg-sink-qaautotest"
# 期望: LAG=0
```

### ④ S3 有 Parquet 文件
```bash
aws s3 ls s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/qaautotest/ --recursive
# 期望: .../event_result/data/00000-xxx.parquet 文件
```

### ⑤ StarRocks 能查到数据
```bash
kubectl -n duckdb exec starrocks-fe-0 -- \
  mysql -h 127.0.0.1 -P 9030 -u root -e "
    SHOW CATALOGS;
    SHOW COMPUTE NODES\G
    SELECT COUNT(*) FROM iceberg_catalog.qaautotest.event_result;
    SELECT event_id, event_type, user_id, event_time
      FROM iceberg_catalog.qaautotest.event_result LIMIT 5;
  "
```

### 实际验证结果

```
手动部署（2026-03-27）:
  06:27  部署所有组件
  06:43  StarRocks init 完成（CN 注册 + Iceberg catalog 创建）
  06:44  写入 30 条测试消息
  06:50  Commit: 30 records → qaautotest.event_result ✅
  06:50  SELECT COUNT(*) = 30 ✅

Helm 部署（同日）:
  06:58  helm install
  07:01  所有 pod Running
  07:01  写入 35 条测试消息
  07:05  Commit: 35 records → qaautotest.event_result ✅
  07:05  SELECT COUNT(*) = 35 ✅
```

实际查询结果（Helm 部署）：
```
mysql> SELECT COUNT(*) FROM iceberg_catalog.qaautotest.event_result;
+------------+
| total_rows |
+------------+
|         35 |
+------------+

mysql> SELECT event_id, event_type, user_id FROM iceberg_catalog.qaautotest.event_result LIMIT 5;
+-------------+------------+-------------+
| event_id    | event_type | user_id     |
+-------------+------------+-------------+
| helm-test-1 | payment    | helm_user_1 |
| helm-test-2 | payment    | helm_user_2 |
| helm-test-3 | payment    | helm_user_3 |
| helm-test-4 | payment    | helm_user_4 |
| helm-test-5 | payment    | helm_user_5 |
+-------------+------------+-------------+
```

> **注意**：测试数据的 featureMap 为空 `{}`，所以约 700 个 feature 列全为 NULL。
> 真实 fp-async 数据（如 QA 环境的 yysecurity 租户）会有完整 feature 值（country="US", amount=299.99 等）。

---

## 8. 环境布置（dev_a，duckdb namespace）

```
集群:     dev_a (us-west-2)
命名空间: duckdb
验证租户: qaautotest
S3 路径:  s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/
```

**S3 数据路径（已验证）**：
```
cre-6630/duckdb/qaautotest/event_result/
  data/     ← Parquet 数据文件（每次 commit 一批，~188KB）
  metadata/ ← Iceberg 元数据（schema、snapshot、manifest）
```
> Iceberg 元数据层级细节见附录 QA C4/C5。

**部署顺序**（依赖关系要求严格按序）：
```
00-secrets.yaml           → Secrets（MySQL 密码、SMT 密码）
10-iceberg-catalog-mysql  → 等 StatefulSet Ready
11-iceberg-rest-catalog   → 等 Deployment Ready（依赖上面的 MySQL）
19-smt-source.yaml        → ConfigMap（SMT jar + pom.xml）
20-kafka-connect          → 等 Deployment Ready
21-kafka-connect-register → Job：注册 connector
30-starrocks-fe           → 等 StatefulSet Ready
31-starrocks-cn           → 等 Deployment Ready
32-starrocks-init         → Job：注册 CN + 创建 Iceberg catalog
```

---

## 9. 踩过的坑

部署过程中遇到的问题和解法，详见[附录 — 踩坑记录](#踩坑记录)。

---

## 10. 后续 TODO（谁做 / 优先级）

### 10.1 fp-async 团队（Dev）

| 工作 | 说明 | 优先级 |
|------|------|--------|
| fp-async 连 StarRocks JDBC | Stats API 通过 JDBC 9030 查询 StarRocks | P0 |
| fp-async 创建 MV DDL | 根据 Cube 配置生成 `CREATE MATERIALIZED VIEW` 并提交 FE | P0 |
| Dimension / Cube CRUD API | `/sofi/dimension`、`/sofi/cube` 接口 | P0 |

### 10.2 Infra

| 工作 | 说明 | 优先级 |
|------|------|--------|
| CN 节点 Spot 化 | 创建 Spot ASG + Launch Template（m5.2xlarge），CN 跑在 Spot 上省 ~74% 成本 | P0 |
| CN 节点规格升级 | 当前 1C/4Gi 是 dev 规格，生产需要 8C/32Gi（详见 design-qa D3） | P0 |
| Kafka Connect commit interval 调整 | 从 60s 改为 10min，减少小文件（详见 design-qa D14） | P0 |
| Kafka Connect 模板化 | 当前 connector 配置是 per-tenant 手写，需要参数化支持新 tenant onboarding | P1 |
| ~~Warehouse 拆分 / CN Query Pool HPA / CN Refresh CronJob~~ | ~~已评估，当前阶段不需要。ToB 凌晨无查询，1 CN 足够（详见 design-qa D4/D5）~~ | 按需 |

### 10.3 需要手动确认的变量

| 变量 | 说明 | 状态 |
|------|------|------|
| `kafka_bootstrap_servers` | FP 的 Kafka 地址 | 看 FP 现有 K8s Service |
| `iceberg_catalog_mysql` | Iceberg Catalog MySQL | **已定：独立实例**（附录 D17） |
| `iceberg_smt_mysql_url` | SMT 查 feature 表的 MySQL | FP 的 risk 库，dv.ro 只读 |
| S3 IAM 权限 | 节点需要 S3 读写权限 | 用 Node Instance Profile，不用 IRSA |

> IAM 配置细节见[附录 — 部署变量与 IAM](#部署变量与-iam)。

---

## 11. 数据延迟 SLA（需要对外说清楚）

| 路径 | 延迟 | 说明 |
|------|------|------|
| 事件发生 → S3 Parquet | ~10min | Kafka Connect commit interval（已从 60s 调整为 10min） |
| S3 Parquet → MV 可查 | ~凌晨后 | CN 每天凌晨批处理（同一个 CN，无查询竞争） |
| Stats API 查询延迟 | 毫秒级 | MV 已就绪时；首次查询（cold start）可能秒级 |
| 新 feature 列出现 | ~60s | SMT 每 60s 刷新 feature 缓存；Iceberg schema 自动 evolve |

---

## 12. 多租户隔离

每个 tenant 一个独立的 Iceberg namespace + 同名表 `event_result`：

```
iceberg_catalog.qaautotest.event_result   ← qaautotest 的数据
iceberg_catalog.yysecurity.event_result   ← yysecurity 的数据
```

- S3 路径物理隔离：`s3://.../qaautotest/event_result/` vs `s3://.../yysecurity/event_result/`
- Schema 独立演进：不同 tenant 的 feature 列集合互不影响
- 每个 tenant 一个 Kafka Connect connector

不用共享表 + WHERE tenant 过滤的原因：MV DDL 更干净、S3 路径天然隔离、排查直接（详见附录 D9）。

---

## 13. Cost & Scaling 设计决策

### 13.1 当前阶段：1 个 CN，不做弹性 Scaling

**如果要做 Scaling，唯一的对象是 CN**（其他组件都不需要）。但当前阶段 1 个 CN 就够，原因：

- **在线查询打的是 MV（预聚合数据）**，每次查询只读几百行，几十毫秒完成。单个 CN（8 CPU）同时处理几十个并发 MV 查询都不到 60% CPU。
- **凌晨 MV refresh** 是批处理，ToB 系统凌晨无用户，不在意速度。1 个 CN 跑 2-3 小时，天亮前完成即可。
- 两种负载**天然串行**（凌晨 refresh，白天查询），不存在资源竞争。

| 组件 | 需要 Scaling？ | 原因 |
|------|--------------|------|
| Iceberg MySQL / REST Catalog | 不需要 | 元数据路径，写入频率固定 |
| Kafka Connect | 不需要 | 内部 task 并行，不靠 pod 数 |
| StarRocks FE | 不需要 | 只做查询规划，CPU 极低 |
| **StarRocks CN** | **未来可能** | 唯一的计算层——但当前 MV 查询极轻，1 个 CN 足够 |

### 13.2 CN 部署方案

| | 方案 A | 方案 B ★ | 方案 C |
|--|--|--|--|
| 架构 | 1× on-demand | 1× spot | 2× spot（HA） |
| 月成本 | ~$276 | ~$72 | ~$144（或降规格后 ~$100） |
| 中断影响 | 无 | 偶发 2-3min | 极少（两个同时被回收概率很低） |

**为什么常驻服务也能用 Spot？** CN 是无状态的——数据在 S3，本地只有 block cache（丢掉可从 S3 重建）。Spot 被回收时：in-flight 查询失败（fp-async 重试即可），数据零丢失，新 CN 2-3 分钟内恢复。"常驻"不等于"不能被中断"，关键是中断的代价够低。

**选方案 B**：ToB 系统，fp-async 可重试，偶发 2-3min 中断可接受，省 74%。ASG 开启 Capacity Rebalancing（AWS 检测到回收风险时先起新节点再终止旧节点）进一步减少中断 gap。

**如果需要 HA → 方案 C 变体：2× 小规格 spot**

2 个 CN 时，单节点不需要承担全部算力，可以降规格：

| | 方案 B（1× 大） | 方案 C 变体（2× 小） |
|--|--|--|
| 规格 | m5.2xlarge（8C/32Gi） | m5.xlarge（4C/16Gi）× 2 |
| 总算力 | 8 CPU / 32Gi | 8 CPU / 32Gi（相同） |
| Spot 单价 | ~$0.10/h | ~$0.05/h × 2 |
| 月成本 | ~$72 | ~$72（基本相同） |
| HA | 无，中断 2-3min | 1 个被回收另 1 个继续服务 |
| 凌晨 MV refresh | 8 CPU 独占，快 | StarRocks FE 分发到 2 个 CN 并行，速度相当 |
| Block cache | 16Gi 集中 | 8Gi × 2 分散，cache 命中率略低 |

方案 C 变体的 tradeoff：**成本几乎一样，换来 HA，单节点 block cache 更小**（8Gi vs 16Gi）。但实际影响可忽略：MV 查询数据量极小（几 KB），8Gi 绑绑有余；fallback 扫原始 Parquet 时数据量远超任何 cache 大小（13 万个文件），16Gi 也放不下，瓶颈在 S3 I/O 不在 cache。

如果 2-3min 偶发中断可接受 → 方案 B（更简单）。如果团队/老板对中断零容忍 → 方案 C 变体（同价换 HA）。

### 13.3 CN 节点规格

**方案 B（1 CN）**：m5.2xlarge（8 CPU / 32Gi），CPU requests == limits 避免 throttle。内存分配：JVM heap 8Gi（25%）+ block cache 16Gi（50%）+ 系统 8Gi（25%）。

**方案 C 变体（2 CN HA）**：m5.xlarge（4 CPU / 16Gi）× 2，JVM heap 4Gi + block cache 8Gi + 系统 4Gi。需配 PodDisruptionBudget `minAvailable: 1`。

### 13.4 什么场景下需要 Scaling

当前不需要，但以下场景会触发：

| 场景 | 现象 | 对应措施 |
|------|------|---------|
| **租户数量大幅增长 + 查询并发上升** | CN CPU 持续 >60%，查询 P99 变慢 | 加 HPA（管 pod 数）+ Cluster Autoscaler + Spot ASG（管节点数）。HPA 扩 pod → pod Pending → CA 自动向 ASG 申请新 Spot 节点 |
| **租户增多导致 MV refresh 延伸到白天** | 凌晨跑不完，和白天查询争资源 | 拆 CN 为 Query/Refresh 两个 Deployment，独立 Scaling 策略 |
| **单个大 tenant onboard，历史数据回填慢** | 初始 MV backfill 需要 24h+ | 临时扩 CN 加速回填，完成后缩回 |
| **MV 未就绪时 fallback 扫原始 Parquet** | 查询从毫秒级降到分钟级 | **不靠 HPA**——根本解法是 onboard 时先触发 MV 回填，ready 前不开放查询（见 Section 5.3）。回填本身太慢时可手动临时加 CN 并行加速 |

### 13.5 为什么用 Cluster Autoscaler + ASG，不用 dcluster

dcluster 技术上可以做这件事（比如写一个 bridge 监控 CPU → 调 dcluster API 加减节点），但 CA + ASG 是更优路径。

**两条路的实际工作量对比：**

```
CA + ASG（标准路径）：
  1. 创建 Spot ASG + Launch Template        ← AWS 配置
  2. 打 Cluster Autoscaler tag              ← 2 行 tag
  3. 需要时加 HPA YAML                      ← 1 个文件
  → 完成。零代码，全是 K8s/AWS 标准组件。

dcluster 路线：
  1. 常驻 CN 得单独管理（不走 dcluster，否则被 Monitor 当孤儿回收）
  2. 写 bridge：监控 CPU → 超阈值调 POST /node/launch → 低阈值调 POST /node/destroy
     （含 Prometheus query、API 失败重试、防抖逻辑）
  3. 或者改 dcluster Monitor 加"service 类型"识别 → 有回归风险
  4. bridge + dcluster + StarRocks 三方联调
  → 多一个自研组件，出 bug 只能自己修。
```

**核心论点不是 dcluster 做不到，而是做同一件事成本不同：**

| 角度 | CA + ASG | dcluster |
|------|---------|----------|
| 工程成本 | 零代码，纯配置 | 需写 bridge 或改 Monitor |
| 风险 | 成熟方案，无回归风险 | 改 Monitor 可能影响现有 Spark/Flink |
| 后续维护 | 标准组件，文档齐全 | bridge 是自研，只能自己维护 |
| dcluster 的优势 | — | right-sizing、Spot Fleet、实例降级、job 生命周期 |
| **这些优势用得上吗** | — | **用不上**——CN 规格固定，无 job 生命周期 |

dcluster 的设计目标是"跑一个 Spark job → 算好资源 → 跑完销毁"。StarRocks CN 是"长期跑着 → 忙了多加几个 → 闲了减掉"——这正好是 CA + HPA 的原生场景。dcluster 的核心优势（right-sizing、Spot Fleet、job 生命周期管理）在这个场景下全部用不到。

---
---

# 附录

## 踩坑记录

| 坑 | 现象 | 原因 | 解法 |
|----|------|------|------|
| Consumer group 名字不对 | `kafka-consumer-groups --group iceberg-connect` 找不到 | Kafka Connect 的 consumer group 名是 `connect-<connector-name>`，不是 `CONNECT_GROUP_ID` | 用 `--list \| grep iceberg` 先找 group 名，实际是 `connect-iceberg-sink-qaautotest` |
| 第一个 commit cycle 是 0 tables | 日志：`committed to 0 table(s)` | Iceberg Sink 第一个周期用于协调机制初始化，不写数据 | 等第二个 commit cycle，会看到 `addedRecords=20` |
| StarRocks CN 注册失败 | `SHOW COMPUTE NODES` 空 | init job 里的 `ALTER SYSTEM ADD COMPUTE NODE` 在 FE 未完全 Ready 时执行 | init job 加 init container 等待 FE 9030 端口 |
| featureMap 全 NULL | 查询结果 feature 列全 NULL | 测试数据用了空的 `featureMap: {}` | 正常 fp-async 数据会有完整 featureMap；测试时需手动构造含真实 feature ID 的消息 |

---

## 部署变量与 IAM

### 完整变量列表

| 变量 | 说明 | 从哪获取 |
|------|------|---------|
| `kafka_bootstrap_servers` | FP 的 Kafka 地址 | 看 FP 现有 K8s Service |
| `iceberg_catalog_mysql_host` | Iceberg Catalog MySQL | **已定：独立实例**（D17） |
| `iceberg_catalog_mysql_password` | MySQL 密码 | Vault / K8s Secret |
| `iceberg_smt_mysql_url` | SMT 查 feature 表的 MySQL URL | FP 的 risk 库 |
| `iceberg_smt_mysql_password_secret` | K8s Secret 名 | 需提前创建 |
| 节点 IAM Role 的 S3 Policy | 节点需要有 S3 读写权限 | 见下方 |

### IAM 凭证方式（已确认）

当前部署（duckdb namespace, kwestdeva）用 **Node Instance Profile**，不用 IRSA：
- StarRocks FE 配置了 `aws_s3_use_instance_profile = true`
- Kafka Connect / Iceberg REST Catalog 走 AWS SDK 默认 credential chain → EC2 IMDS → Node Instance Profile

不需要的：`iceberg_kafka_connect_role_arn`、`iceberg_starrocks_role_arn`、OIDC provider ID（都是 IRSA 用的）。

唯一需要确认：节点 IAM Role 上是否已有 S3 权限：
```bash
aws iam list-attached-role-policies --role-name <kwestdeva-node-role>
# 需要: s3:GetObject / s3:PutObject / s3:DeleteObject / s3:ListBucket
# 作用于: arn:aws:s3:::datavisor-dev-us-west-2-iceberg/*
```


---

## 设计问答（Design Q&A）

记录 Infra 设计阶段的关键问题、取舍和结论。每个 Q 标注为 `[设计决策]` 或 `[概念理解]`，方便区分哪些是要做的选择、哪些是学习理解。

**目录**

**一、Scaling & Cost（核心设计决策）**
- [D1: 架构中哪些组件需要 Scaling？](#d1架构中哪些组件需要-scaling)
- [D2: CN Spot 部署方案对比](#d2cn-spot-部署方案对比)
- [D3: CN 节点规格怎么选？](#d3cn-节点规格怎么选)
- [D4: 当前阶段是否需要 HPA？](#d4当前阶段是否需要-hpa)
- [D5: 是否需要把 CN 拆成 Query Pool 和 Refresh Pool？](#d5是否需要把-cn-拆成-query-pool-和-refresh-pool)
- [D6: 凌晨 MV refresh——1 个 CN 还是 2 个？](#d6凌晨-mv-refresh1-个-cn-还是-2-个)
- [D7: 为什么用 Cluster Autoscaler + ASG + Spot，而不是 dcluster？](#d7为什么用-cluster-autoscaler--asg--spot而不是-dcluster)
- [D8: dcluster 解决了哪些 CA 解决不了的问题？](#d8dcluster-解决了哪些-ca-解决不了的问题)

**二、多租户与数据模型**
- [D9: 多租户表结构——共享表 vs 独立 namespace](#d9多租户表结构共享表-vs-独立-namespace)
- [D10: Iceberg event_result 列名从哪里来？](#d10iceberg-event_result-列名从哪里来)

**三、MV 刷新与查询**
- [D11: MV 刷新频率（Daily vs Hourly）](#d11mv-刷新频率daily-vs-hourly)
- [D12: target_features 校验时机](#d12target_features-校验时机)
- [D13: MV fallback 场景（MV 未就绪时降级扫原始 Parquet）](#d13mv-fallback-场景mv-未就绪时降级扫原始-parquet)

**四、数据写入管线配置**
- [D14: Kafka Connect commit interval（60s 是否合适）](#d14kafka-connect-commit-interval60s-是否合适)
- [D15: SMT metadata.refresh.interval.ms（60s 是否合理）](#d15smt-metadatarefreshintervalms60s-是否合理)

**五、基础设施与职责边界**
- [D16: 哪些设计点需要和 Dev 同步？](#d16哪些设计点需要和-dev-同步)
- [D17: Iceberg Catalog MySQL——独立实例还是共用 FP MySQL？](#d17iceberg-catalog-mysql独立实例还是共用-fp-mysql)

**六、概念理解（Iceberg / StarRocks / FP 业务概念）**
- [C1: raw_events 和 event_result 是什么关系？](#c1raw_events-和-event_result-是什么关系)
- [C2: per-tenant namespace 在 Iceberg catalog 物理层面是什么？](#c2per-tenant-namespace-在-iceberg-catalog-物理层面是什么)
- [C3: StarRocks FE 和 CN 各自的职责](#c3starrocks-fe-和-cn-各自的职责)
- [C4: Iceberg 数据写入顺序——先写 S3 还是先更新 Catalog？](#c4iceberg-数据写入顺序先写-s3-还是先更新-catalog)
- [C5: Iceberg 各组件分别存了什么？](#c5iceberg-各组件分别存了什么)
- [C6: StarRocks 查询 Iceberg 的完整链路](#c6starrocks-查询-iceberg-的完整链路)
- [C7: Iceberg 三个 K8s Pod 各自的职责](#c7iceberg-三个-k8s-pod-各自的职责)
- [C8: Feature / Dimension / Measure / Cube 的业务概念](#c8feature--dimension--measure--cube-的业务概念)
- [C9: StarRocks MV 数据存在哪？](#c9starrocks-mv-数据存在哪)
- [C10: Iceberg 只有 event_result 一张主表吗？](#c10iceberg-只有-event_result-一张主表吗)

---

### 一、Scaling & Cost（核心设计决策）

## D1：架构中哪些组件需要 Scaling？

`[设计决策]`

**结论：只有 StarRocks CN 需要 Scaling。**

| 组件 | 负载性质 | 需要 Scaling？ | 原因 |
|------|---------|--------------|------|
| Iceberg Catalog MySQL | 元数据存储，每 commit 一次 | 不需要 | 写入频率固定，不随查询量增长 |
| Iceberg REST Catalog | 轻量 REST proxy | 不需要 | 极轻量，metadata 路径不在热路径 |
| Kafka Connect | 消费 Kafka → S3，`tasks.max` 内部并行 | 不需要 pod HPA | 吞吐靠内部 task 并行，不靠 pod 数 |
| StarRocks FE | Query planning，协调 CN | 不需要 | 不做实际计算，CPU 极低 |
| **StarRocks CN** | **执行查询 + MV refresh，扫 S3 做聚合** | **需要** | 唯一的计算层 |

核心原因：计算存储分离架构。数据在 S3（永久存储），CN 是纯计算（无状态）。只有 CN 的负载随查询量线性增长。

---

## D2：CN Spot 部署方案对比

`[设计决策]`

| | 方案 A | 方案 B ★ | 方案 C | ~~方案 D~~（已排除） |
|--|--|--|--|--|
| **架构** | 1× on-demand | 1× spot | 2× spot | 1× spot + 凌晨临时扩 spot |
| **月成本** | ~$276 | ~$72 | ~$144 | ~$90 |
| **在线查询可用性** | 高，无中断 | 偶发 2-3min 中断 | 极少中断 | 同方案 B |
| **spot 中断影响** | 无 | 查询报错需重试 | 1 个被回收另 1 个继续 | 同方案 B |
| **凌晨 MV refresh** | 独占，正常 | 独占，正常 | 独占，正常 | 同方案 B |
| **实现复杂度** | 最简单 | 简单 | 简单 + PDB | 需 KEDA/CronJob |

**方案 D 排除原因**：设计前提是"凌晨查询与 refresh 争资源"——ToB 系统凌晨无用户，前提不成立。

**推荐：方案 B**。ToB 内部系统，fp-async 调用方可做重试，2-3min 偶发中断可接受，省 74% 成本。

**Spot 中断防护**：ASG 开启 **Capacity Rebalancing**。AWS 检测到 spot 有被回收风险时，主动提前申请新 spot → 新节点 Ready 后再终止旧节点，中断 gap 极小。无需改代码，纯 AWS 配置。

---

## D3：CN 节点规格怎么选？

`[设计决策]`

推荐起始规格：**m5.2xlarge（8 CPU / 32Gi）**

**内存分配：**

```
容器 32Gi
├── JVM heap (-Xmx8192m)      ~25% = 8Gi   ← SQL 执行工作内存
├── Off-heap block cache       ~50% = 16Gi  ← S3 数据缓存，命中则不回 S3
└── 系统 + network buffer      ~25% = 8Gi
```

**为什么不用 4 CPU / 16Gi：**
- StarRocks CN 查询内部并行，4 CPU 以下并行度不足
- Block cache 只有 ~8Gi，S3 scan fallback 时 cache miss 率高，延迟不稳

**为什么 CPU requests == limits：**
- K8s CPU throttle 对查询延迟影响直接（毫秒级 throttle 可见）
- requests < limits 会在高负载时触发 throttle

**cn.conf 对应配置：**
```
JAVA_OPTS="-Xmx8192m -XX:+UseG1GC ..."
storage_page_cache_size=16384   # 16Gi，单位 MB
```

---

## D4：当前阶段是否需要 HPA？

`[设计决策]`。**此结论推翻了早期 Q8 中的 HPA YAML 设计。**

**结论：不需要。**

MV 查询对 CN 几乎没有计算压力（每次查询读几百行预聚合数据，几十毫秒完成）。单个 CN（8 CPU）可以同时处理几十个并发 MV 查询而不到 60% CPU。

**当前推荐配置：**
```
replicas: 1
spot ASG: min=1, max=1   # 不伸缩，常驻
```

**什么时候加 HPA：**
- CN CPU 持续 > 60%（通过监控确认，不是猜测）
- 通常需要租户数量 × 查询频率达到相当规模

**HPA 参考配置（留给将来）：**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment          # CN 是 Deployment，不是 StatefulSet
    name: starrocks-cn
  minReplicas: 2
  maxReplicas: 8
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 120
      policies:
        - type: Pods
          value: 2
          periodSeconds: 300
    scaleDown:
      stabilizationWindowSeconds: 600
      policies:
        - type: Pods
          value: 1
          periodSeconds: 600
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
```

Spot 配置（ASG spot node group + toleration + affinity）参见早期设计，仍然适用：

```yaml
tolerations:
  - key: node.kubernetes.io/lifecycle
    value: spot
    effect: NoSchedule
affinity:
  nodeAffinity:
    preferredDuringScheduling:   # preferred，防止 Spot 枯竭时卡死
      - weight: 80
        preference:
          matchExpressions:
            - key: node.kubernetes.io/lifecycle
              operator: In
              values: [spot]
```

---

## D5：是否需要把 CN 拆成 Query Pool 和 Refresh Pool？

`[设计决策]`

**结论：当前阶段不需要，是过度设计。**

### 早期提案

拆分为 `starrocks-cn-query` + `starrocks-cn-refresh` 两个 Deployment，通过 StarRocks Warehouse 隔离：

| 维度 | CN Query | CN Refresh |
|------|----------|------------|
| 触发方式 | 随时来请求 | 每天凌晨固定窗口 |
| 延迟要求 | P99 < 500ms | 无 |
| 可中断性 | 不可中断 | 可重跑 |
| Spot 策略 | On-Demand 保底 | 纯 Spot |
| 伸缩方式 | HPA | CronJob scaler |

### 为什么不需要拆

拆分前提是"MV refresh 和在线查询会同时竞争资源"。但实际：

| 时间 | 查询负载 | Refresh 负载 | 竞争？ |
|------|---------|------------|--------|
| 凌晨 1-4am | **0**（ToB 系统，用户不上班） | MV refresh 运行 | **无竞争** |
| 白天 | MV 查询极轻（几十 ms） | 不运行 | **无竞争** |

两种工作负载天然串行，不存在资源竞争。拆分会带来真实代价（两个 Deployment、两个 Warehouse、两套运维），解决的是一个不存在的问题。

### 触发重新考虑拆分的条件
- 租户增长到 MV refresh 窗口延伸到业务时间
- 查询 QPS 增长到单 CN 扛不住

---

## D6：凌晨 MV refresh——1 个 CN 还是 2 个？

`[设计决策]`

**结论：1 个 CN 慢慢跑，cost 最优。**

```
凌晨的约束:
  用户查询: 0（ToB）
  截止时间: 天亮前跑完即可（6am 上班前）
  资源竞争: 无

1 个 CN:
  8 CPU 全部给 MV refresh
  跑 2-3 小时 → 完全满足时间窗口
  额外成本: $0（本来就开着这个 spot）

2 个 CN（临时扩）:
  1-1.5 小时跑完，快 1 倍
  额外成本: $0.10 × 3h × 30天 ≈ $9/月
  快完成的业务价值: 凌晨 3am 无人在意
```

没有截止时间压力，跑快没有业务价值，多花钱没有意义。

---

## D7：为什么用 Cluster Autoscaler + ASG + Spot，而不是 dcluster？

`[设计决策]`

dcluster 是公司内部为 Spark/Flink batch job 开发的集群管理服务，**不适合 StarRocks CN** 这种长期运行服务：

| 维度 | dcluster | CA + ASG + Spot |
|------|---------|-----------------|
| 设计目标 | Spark/Flink batch job 完整生命周期 | 通用 pod 弹性伸缩 |
| 触发机制 | 显式 REST API 调用（POST /node/launch） | 自动感知 Pending Pod |
| 与 HPA 协作 | 不支持，需自定义 bridge | 原生串联（Pending Pod 是接触点） |
| 节点清理 | 依赖"job 结束"事件，Monitor 会把无 job 的节点当垃圾回收 | 依赖节点利用率 |
| 改造成本 | 中等（需改 Monitor，有破坏现有 Spark/Flink 的风险） | 零（配置 ASG + Launch Template） |

**"用 dcluster 管临时扩出来的 CN 节点"可以吗？**

技术上可行：常驻 CN 不走 dcluster，临时扩的 CN 用 dcluster 当"job"管理，缩容时自然被 dcluster Monitor 回收。但需要写一个 bridge（监控 CPU → 调 dcluster API），且常驻 CN 和临时 CN 走两套管理机制，运维复杂度翻倍。

CA + ASG 用同一套机制管所有 CN（常驻 + 临时），HPA 统一控制 pod 数，CA 统一控制节点数。dcluster 的核心优势（right-sizing、Spot Fleet、实例降级、job 生命周期）在 CN 规格固定、无 job 概念的场景下全部用不到。

**结论**：dcluster 不动，StarRocks CN 用 CA + ASG + Spot 标准路径，两套互不干扰。

**HPA + CA + ASG 三者串联逻辑（将来启用 HPA 时）：**

```
HPA 增加 CN replicas
  → 新 Pod 变成 Pending（没有合适节点）
  → Cluster Autoscaler 看到 Pending Pod
  → CA 调用 ASG scale-out
  → ASG 用 Launch Template 起 Spot EC2
  → 节点加入 K8s（带 label: role=starrocks-cn）
  → Scheduler 把 Pending Pod 调度上去

HPA 减少 CN replicas
  → 节点变空/低利用
  → CA 调用 ASG scale-in → EC2 终止
```

三者通过 **Pending Pod → underutilized node** 两个信号自动串联，无需自定义代码。

---

## D8：dcluster 解决了哪些 CA 解决不了的问题？

`[概念理解]`。解释 dcluster 的存在价值，不是 StarRocks 的设计决策。

CA 的能力边界：「有 Pending Pod → 加节点；节点空闲 → 减节点」，对上层工作负载完全无感知。

dcluster 针对 Spark/Flink 额外解决的问题：

| 能力 | 说明 |
|------|------|
| **Right-sizing / bin-packing** | 读 job 配置 → 查 EC2 API 拿实例规格 → 计算 workers_per_slave → 申请最小够用实例 |
| **Spot Fleet** | 同时向多种实例类型竞价，weighted capacity 保证总算力达标 |
| **实例降级 fallback** | r7i.2xlarge 不可用 → 自动降到 r6i → r5 |
| **Job 全生命周期** | job 提交 → 起节点 → Helm 部署 → 监控异常 → job 结束立刻销毁 |
| **孤儿节点清理** | Monitor 每 5 min 检查无 running job 的节点，立刻回收 |
| **多云抽象** | AWS / GKE / On-premise 统一 API |

一句话：CA 是基础设施层（"我的 pod 放不下了"），dcluster 是工作负载层（"帮我端到端跑完一个 Spark job"）。

---

### 二、多租户与数据模型

## D9：多租户表结构——共享表 vs 独立 namespace

`[设计决策]`

**结论：用独立 namespace（per-tenant），已验证。**

| 维度 | 方案 A：共享表 | 方案 B：独立 namespace ★ |
|--|--|--|
| 隔离方式 | `tenant` 列 + partition pruning | Iceberg namespace 物理隔离 |
| MV DDL | 需要 `WHERE tenant = 'xxx'` | 无需过滤，FROM 本身已隔离 |
| S3 物理路径 | `s3://bucket/raw_events/tenant=sofi/` | `s3://bucket/{tenant}/event_result/` |
| Schema 演进 | ALTER 一次，所有 tenant 共享 | 每个 namespace 独立演进，互不影响 |
| 排查范围 | 需加 `WHERE tenant` 过滤 | S3 路径天然隔离到 tenant 级别 |

核心理由：MV DDL 更干净，不同 tenant 的 feature 列集合完全独立，S3 天然隔离。

---

## D10：Iceberg event_result 列名从哪里来？

`[概念理解]`

**不是 Cube 生成的，是 Feature 定义决定的。**

```
Feature 发布 → FP MySQL feature 表（id=7, name="country"）
  → EventResult.featureMap = {7: "US"}
  → Kafka velocity-al
  → SMT 查 MySQL：id=7 → name="country"
  → Iceberg ALTER TABLE ADD COLUMN country STRING
```

Cube 的作用是"在 Iceberg 原始表上建预聚合视角"，不决定原始表的列。

---

### 三、MV 刷新与查询

## D11：MV 刷新频率（Daily vs Hourly）

`[设计决策]`

### MV 刷新频率 vs K8s 资源调度——两个不同层次

| 层次 | 控制者 | 配置位置 | 决定了什么 |
|--|--|--|--|
| MV 刷新频率 | StarRocks FE 调度器 | `CREATE MV ... REFRESH ASYNC EVERY(...)` | 数据新鲜度（业务 SLA） |
| CN 资源何时在线 | K8s（当前方案：常驻 1 CN） | Deployment replicas | 算力可用时间 |

> **注**：早期设计曾考虑独立的 CN Refresh CronJob（凌晨拉起 / 白天 scale to zero）。基于 D5 的结论（不拆 CN），当前方案改为 1 个常驻 CN 同时承担查询和 refresh，无需 CronJob scaler。

### Daily vs Hourly 取舍

| 维度 | Daily（推荐起步） | Hourly |
|--|--|--|
| 数据延迟 SLA | 最长 ~24h | 最长 ~1h |
| CN 资源 | 凌晨 refresh 2-3h，白天只跑查询 | refresh 持续运行，和查询争资源 |
| 成本 | 低 | 高（refresh 占更多 CPU 时间） |
| 适用场景 | 离线分析、T+1 报表 | 近实时趋势查询 |

**结论**：先从 Daily 起步。MV 刷新频率是业务决策，由需求方根据可接受的数据延迟 SLA 决定。

---

## D12：target_features 校验时机

`[设计决策]`

**结论：两层校验，分工明确。**

**创建时（轻校验）**
- `target_features` 字段格式是否合法
- 引用的 feature ID 是否存在于当前 tenant

**MV 刷新前（深校验）**
- Feature 数据类型是否与聚合函数兼容
- 校验失败 → `Cube.status = ERROR`，写入 `error_message`

> 关键点：运行时校验放在**刷新开始前**而不是**失败后**——主动校验比被动捕异常更可控。

**Infra 排查路径（三类原因）：**

```
Cube.status == ERROR → 看 error_message
  ├── "feature xxx not found"        → 创建时校验漏过（不应发生）
  ├── "type incompatible: ..."       → 刷新前校验失败（配置问题）
  └── "SQL execution error: ..."     → 基础设施问题
```

责任边界：Dev 写校验逻辑，Infra 按三类路径排查。

---

## D13：MV fallback 场景（MV 未就绪时降级扫原始 Parquet）

`[设计决策]`

**触发条件：**
1. 新 tenant 刚 onboard，历史 MV 还没建好（**最常见**）
2. MV refresh 失败
3. 凌晨 refresh 未完成就有查询（ToB 一般不会）

**Latency 差距——数量级级别：**

```
MV 查询:         几十毫秒~1 秒（读几百行预聚合数据）
Raw Parquet scan: 几分钟~十几分钟（扫 90天 × 1440文件/天 ≈ 13万个 S3 文件）
```

**推荐设计（优先治本，不靠 scaling）：**

| 方案 | 说明 | 推荐度 |
|------|------|--------|
| Onboard 时立刻触发历史 MV 回填 | MV ready 前前端显示"数据处理中"，ready 后开放查询 | ★★★ 根本解法 |
| Fallback 时限制查询时间范围 | 自动把 90d 降级到 7d，扫描量减少 13x | ★★ 兜底保护 |
| 临时扩容加速回填 | 仅当单 tenant 数据量极大时才需要 | ★ 按需 |

**核心原则**：scaling 不是 fallback 的解法，确保 MV 始终 ready 才是。

---

### 四、数据写入管线配置

## D14：Kafka Connect commit interval（60s 是否合适）

`[设计决策]`

**结论：推荐改为 10min（600000ms）。**

60s 在日级 MV 场景下偏激进，主要副作用：
- 一天 ~2,880 批小文件，CN 降级扫描时 open 大量文件，性能差
- Iceberg catalog manifest 条目膨胀快
- 60s vs 10min 对 MV 结果完全无影响

| 提交间隔 | 每天文件批次 | 数据可见延迟 | 评估 |
|----------|------------|------------|------|
| 60s | ~2,880 | ≤60s | 偏激进 |
| **10min** | **~288** | **≤10min** | **推荐** ✓ |
| 30min | ~96 | ≤30min | 纯批处理 |

唯一保留 60s 的理由：有"最近 1 分钟数据必须可见"的强 SLA——本系统不存在此需求。

---

## D15：SMT metadata.refresh.interval.ms（60s 是否合理）

`[设计决策]`

**结论：60s 合理。**

SMT 每 60s 全量查询 FP MySQL `SELECT id, name, return_type FROM feature WHERE status='PUBLISHED'`。Feature publish 是低频操作，≤60s 的可见延迟完全可接受。查询极轻，对 FP MySQL 无感知压力。

---

### 五、基础设施与职责边界

## D16：哪些设计点需要和 Dev 同步？

`[设计决策]`

### 必须和 Dev sync

| 设计点 | 需要确认的问题 |
|--|--|
| Stats API request/response schema | window 参数格式？response 里 coverage/actual_days 怎么传？ |
| event_result 表名 vs raw_events | 统一用哪个名字？ |
| MV 刷新频率（daily vs hourly） | 业务可接受的数据延迟 SLA？ |
| target_features 校验时机 | Dev 写校验逻辑，Infra 排查路径依赖这个决定 |

### Infra 自己决定

- Kafka Connect commit interval → 已定 10min（D14）
- CN 节点规格和 Spot/HPA 策略 → 已定（D2-D4）
- S3 路径命名规则
- Iceberg catalog MySQL 独立实例 → 已定（D17）
- per-tenant namespace → 已定（D9）

---

## D17：Iceberg Catalog MySQL——独立实例还是共用 FP MySQL？

`[设计决策]`

**结论：独立实例。**

核心风险：Iceberg Vacuum 的长事务会让 FP 的小事务排队等锁，直接拉高实时检测的 p99 延迟。

| 维度 | FP MySQL | Iceberg Catalog MySQL |
|------|----------|-----------------------|
| 延迟敏感 | 极高（在线检测路径） | 中等（批处理可容忍） |
| 锁特征 | 短暂行锁 | Vacuum 时可能长事务 |
| 峰值来源 | 实时请求流量 | 批处理调度周期 |

Iceberg JDBC catalog 对 MySQL 规格要求低（元数据量有限），小规格实例足够，成本不高。

---

### 六、概念理解（Iceberg / StarRocks / FP 业务概念）

> 以下内容是理解性问答，不涉及需要做的设计选择。

## C1：raw_events 和 event_result 是什么关系？

**是同一个概念，命名不一致。**

| | 系统设计文档 | 实际实现 |
|--|--|--|
| 表名 | `raw_events` | `event_result` |
| 数据内容 | 原始事件 + 全量 feature 列 | 原始事件 + 全量 feature 列 |

实现里选 `event_result`：与 ClickHouse 已有的 `event_result` 表对齐——**velocity-al → Iceberg 的 ClickHouse 镜像**。

---

## C2：per-tenant namespace 在 Iceberg catalog 物理层面是什么？

**是不同的表，不是同一张表的不同分区。**

Iceberg REST Catalog 底层 MySQL `iceberg_tables` 中 `table_namespace` 字段对应 tenant：

```
catalog_name | table_namespace | table_name   | metadata_location
─────────────────────────────────────────────────────────────────
rest         | qaautotest      | event_result | s3://.../qaautotest/event_result/metadata/v3.metadata.json
rest         | yysecurity      | event_result | s3://.../yysecurity/event_result/metadata/v1.metadata.json
```

各 namespace 拥有完全独立的 S3 metadata.json 和 Parquet 数据文件。

---

## C3：StarRocks FE 和 CN 各自的职责

```
FE（Frontend，常驻 StatefulSet）:
  - SQL 解析 + 查询规划
  - 把 fp-async 的 SQL 翻译成执行计划，分发给 CN
  - 管理元数据（表 schema、MV 定义、CN 注册状态）
  - 轻量，不做实际数据计算

CN（Compute Node，无状态 Deployment）:
  ├── 凌晨: MV refresh——扫 S3 原始 Parquet，GROUP BY，写聚合结果
  └── 白天: 查询执行——读 MV 预聚合数据，合并 daily partition，返回
```

CN 是唯一做实际计算的组件。

---

## C4：Iceberg 数据写入顺序——先写 S3 还是先更新 Catalog？

**先写 S3，最后原子更新 Catalog。**

```
① 写 Parquet 数据文件 → S3
② 写 manifest file（avro）→ S3
③ 写 snapshot（avro）→ S3
④ 写新版 metadata.json → S3
⑤ Catalog 原子更新指针：旧 metadata.json → 新 metadata.json  ← 提交完成
```

第 ⑤ 步是提交的原子边界。之前任何阶段挂掉，S3 上有孤儿文件但对外不可见，Vacuum 后续清理。

---

## C5：Iceberg 各组件分别存了什么？

### S3 上的文件

| 文件类型 | 存了什么 | 作用 |
|----------|---------|------|
| Parquet（data） | 实际事件数据，列存压缩 | 数据真相，CN 最终读这里 |
| metadata.json | schema、partition spec、snapshot 列表、current 指针 | 表的"总目录"，每次 commit 新版 |
| snapshot（avro） | 提交时间、类型、指向 manifest 的指针 | MVCC 基础 |
| manifest file（avro） | 每个 parquet 的路径 + min/max/null 统计 | 查询加速：跳过不满足条件的文件 |

### Catalog（REST + MySQL）

只存一件事：当前 metadata.json 的 S3 路径。核心作用：并发控制（CAS 原子更新）+ 服务发现。

---

## C6：StarRocks 查询 Iceberg 的完整链路

```
StarRocks FE 收到 SQL
  → 问 REST Catalog：metadata 在哪？
  → 读 S3 metadata.json → 找 current snapshot
  → 读 snapshot → 找 manifest 列表
  → 读 manifest：检查每个 parquet 的 min/max → 跳过不匹配的文件
  → 只拉相关 parquet → CN 执行计算 → 返回
```

关键点：Iceberg 不持有数据也不传递数据。FE 拿到 metadata 路径后，所有后续操作直接访问 S3。

---

## C7：Iceberg 三个 K8s Pod 各自的职责

```
iceberg-catalog-mysql   = Catalog 后端（MySQL），存 table→metadata 路径映射
iceberg-rest-catalog    = REST API，把 MySQL 指针暴露成 HTTP 接口
iceberg-kafka-connect   = 写入端，Kafka → Parquet（S3）+ 提交 Iceberg snapshot
```

StarRocks FE 从 REST Catalog 拿到路径后 → 直接读 S3，不再经过 Catalog。

---

## C8：Feature / Dimension / Measure / Cube 的业务概念

```
Feature  = 原材料（事件上的字段：country, amount, device_risk...）
Dimension = 切片方式（按什么分组：按国家、按事件类型）
Measure  = 统计量（分组后算什么：count, sum, p95, distinct_count）
Cube     = 一个分析需求 = Dimension + Measure + 哪些 Feature
MV       = Cube 的预计算结果，按天分区，存在 StarRocks S3 路径里
```

---

## C9：StarRocks MV 数据存在哪？

**也在 S3，但格式和 Iceberg 不同，由 StarRocks 自己管理（Shared-Data 架构）。**

```
s3://warehouse/
├── {tenant}/event_result/        ← Iceberg 管（Parquet 格式，原始全量）
└── starrocks-segments/           ← StarRocks 管（Segment 格式，预聚合结果）
    └── mv_{tenant}_{dim}_{feat}/
        ├── stat_date=2026-03-28/ ← 每天一个 partition，极小（~几KB）
        └── stat_date=2026-03-29/
```

CN 无状态：数据在 S3，本地 cache 只是加速层，丢掉可重建。

---

## C10：Iceberg 只有 event_result 一张主表吗？

**是的。**

```
① Iceberg: event_result（per-tenant namespace）
   = 全量原始事件 + 所有 feature 列，持续写入

② StarRocks MV: mv_{tenant}_{dimension}_{feature}
   = 每个 Cube × Feature 自动生成，按天分区
```

早期设计曾考虑 4 套 Iceberg 存储（raw + daily + agg + cache），最终简化为 **1 张 Iceberg 原始表 + StarRocks 负责所有预聚合**。
