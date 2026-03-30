# CRE-6630 Production Deployment Record — duckdb namespace on dev_a

**日期:** 2026-03-27
**集群:** dev_a (us-west-2)
**命名空间:** duckdb
**验证租户:** qaautotest

---

## 1. 部署流程

### 1.1 组件清单

| 组件 | 功能 | K8s 类型 | Service | 镜像 |
|------|------|---------|---------|------|
| Iceberg Catalog MySQL | 存储 Iceberg 表的元数据 (schema, partition, manifest 路径) | StatefulSet (1) | iceberg-catalog-mysql:3306 | mysql:8.0 |
| Iceberg REST Catalog | Iceberg 元数据 REST API, 供 Kafka Connect 和 StarRocks 访问 | Deployment (1) | iceberg-rest-catalog:8181 | tabulario/iceberg-rest:1.6.0 |
| Kafka Connect + SMT | 消费 velocity-al topic → SMT 将 featureMap 整数 ID 解析为列名 → 写 S3 Parquet (Iceberg 格式) | Deployment (1) | iceberg-kafka-connect:8083 | confluentinc/cp-kafka-connect-base:7.7.1 |
| StarRocks FE | 查询规划、MV 调度、元数据管理。接受 fp-async 的 JDBC 连接 | StatefulSet (1) | starrocks-fe:9030 | starrocks/fe-ubuntu:3.3-latest (v3.3.22) |
| StarRocks CN | 无状态计算节点。扫 S3 Parquet 做聚合/排序, 真正执行查询 | Deployment (1) | starrocks-cn:9050 | starrocks/cn-ubuntu:3.3-latest (v3.3.22) |

### 1.2 外部依赖 vs 我们部署的

**我们部署的 (5 个 pod):**
上面 1.1 的 5 个组件，全部是新增到 duckdb namespace。

**外部依赖 (已存在，不动):**

| 依赖 | 地址 | 用途 | 谁提供 |
|------|------|------|--------|
| Kafka | kafka3.duckdb:9092 (3 broker) | Kafka Connect 消费 velocity-al topic | 现有基础设施 |
| FP MySQL | fp-mysql.duckdb:3306 | SMT 查 feature 表做 ID→name 映射 (dv.ro 只读) | 现有基础设施 |
| S3 Bucket | datavisor-dev-us-west-2-iceberg | Kafka Connect 写 Parquet, StarRocks 读 Parquet | 现有基础设施 |
| fp-async | fp-async.duckdb:8080 | 产生 velocity-al 消息 (我们的数据源) | 现有基础设施 |

**velocity-al topic 命名规则**

```
{cluster_name}_fp_velocity-al-.{tenant}
```

| 环境 | 示例 |
|------|------|
| 生产 (prod cluster) | `prod_fp_velocity-al-.admin` |
| 生产 (prod cluster) | `prod_fp_velocity-al-.baselane` |
| Dev (duckdb cluster) | `duckdb_fp_velocity-al-.qaautotest` |

新 tenant onboarding 时，Kafka Connect connector 配置里的 `topics` 字段就按此规则填。

**数据来源: velocity-al topic 是怎么来的？**

```
客户请求
  │
  ▼
FP API (Detection / Accumulate)
  │  把事件写到 Kafka
  ▼
Kafka: {cluster}_fp_velocity.{tenant}        ← velocity topic (原始事件)
  │
  ▼
fp-async Consumer
  ├──→ 写 YugabyteDB (velocity 聚合数据，供实时查询)
  │
  └──→ 产生到 Kafka: {cluster}_fp_velocity-al-.{tenant}   ← 我们消费的 topic
              │
       ┌──────┴─────────────────────────┐
       ▼                                ▼
  ConsumerForCH                   Kafka Connect (我们新加的)
  (已有，写 ClickHouse)            独立 consumer group，互不干扰
                                        │ SMT 转换 featureMap
                                        ▼
                               S3 Parquet (Iceberg 格式)
                                        │
                                        ▼
                               StarRocks FE → CN
                               SELECT * FROM iceberg_catalog.{tenant}.event_result
```

**完整数据链路 (含 MV):**

```
                    [写入路径 - 实时]
Kafka Connect → S3 Parquet (原始事件，每 60s 一批)

                    [MV 刷新路径 - 凌晨批处理]
CN Refresh 扫 S3 原始 Parquet
→ GROUP BY (Dimension × Measure)
→ 写 MV 日聚合分区 (几百行/天)

                    [查询路径 - 在线毫秒级]
fp-async Stats API → StarRocks FE → CN Query 读 MV
→ 合并多个 daily 分区 → 返回 30d/90d/180d 聚合结果

                    [降级路径 - MV 未就绪时]
FE 自动 CBO rewrite → CN Query 扫原始 Parquet (慢但数据最新)
```

**本次验证中的真实数据 vs 手动数据:**

| 数据 | 来源 | 说明 |
|------|------|------|
| velocity-al topic 结构 | fp-async 产生 | JSON: `{eventId, eventType, userId, eventTime, processingTime, featureMap}` |
| 本次测试数据 | **手动** kafka-console-producer | 因为 FP 无法加载 qaautotest 租户，手动写入 |
| 生产环境数据 | **自动** fp-async 持续产生 | 不需要手动 trigger |

> **生产环境不需要手动 trigger。** fp-async 持续消费 velocity topic，每处理一个事件就产生一条 velocity-al 消息。Kafka Connect 实时消费，每 60s 做一次 Iceberg commit 写入 S3。

### 1.3 Tenant 说明

当前部署验证租户：**qaautotest** (duckdb namespace)

| 配置项 | 值 |
|--------|-----|
| 命名空间 | duckdb |
| 租户 | qaautotest |
| velocity-al topic | `duckdb_fp_velocity-al-.qaautotest` |
| topic 命名规则 | `{namespace}_fp_velocity-al-.{tenant}` — fp-async 标准格式 |
| FP MySQL | fp-mysql.duckdb:3306/qaautotest (dv.ro 只读) |



### 1.5 部署顺序

必须按顺序部署，每步等前一步 Ready：

```
00-secrets.yaml          → Secrets (MySQL 密码、SMT 密码)
19-smt-source.yaml       → ConfigMap (FeatureResolverTransform.java + pom.xml)
10-iceberg-catalog-mysql  → 等 StatefulSet Ready
11-iceberg-rest-catalog   → 等 Deployment Ready (依赖上面的 MySQL)
20-kafka-connect          → 等 Deployment Ready (依赖 REST Catalog + Kafka)
21-kafka-connect-register → Job: 注册 connector (依赖 Connect Ready)
30-starrocks-fe           → 等 StatefulSet Ready
31-starrocks-cn           → 等 Deployment Ready
32-starrocks-init         → Job: 注册 CN + 创建 Iceberg catalog (依赖 FE Ready)
```

### 1.6 手动部署

```bash
cp 00-secrets.example.yaml 00-secrets.yaml
# 编辑密码
bash deploy-duckdb.sh
```

### 1.7 Helm 部署

```bash
helm install starrocks-iceberg ./charts-production/ -n duckdb \
  --kubeconfig=...dev_a.config \
  --set iceberg.catalogMysql.rootPassword="xxx" \
  --set iceberg.catalogMysql.password="xxx"
```

---

## 2. 数据流

```
┌─────────────┐     velocity-al topic      ┌───────────────────┐
│  fp-async    │ ──────────────────────────→│  Kafka (3 broker) │
│  (或手动     │  duckdb_fp_velocity-al-    │  kafka3.duckdb    │
│   produce)   │  .qaautotest               │  :9092            │
└─────────────┘                             └────────┬──────────┘
                                                     │ consume
                                                     ▼
                                            ┌───────────────────┐
                                            │ Kafka Connect     │
                                            │ iceberg-sink-     │
                                            │ qaautotest (2task)│
                                            │ + SMT transform   │
                                            └────────┬──────────┘
                                                     │ write Parquet
                                                     ▼
                            ┌────────────────────────────────────────┐
                            │  S3: datavisor-dev-us-west-2-iceberg   │
                            │  /cre-6630/duckdb/qaautotest/          │
                            │  event_result/data/*.parquet           │
                            └────────────────┬───────────────────────┘
                                             │ metadata
                                             ▼
                            ┌────────────────────────────────────────┐
                            │  Iceberg REST Catalog (:8181)          │
                            │  → Iceberg Catalog MySQL (metadata)    │
                            └────────────────┬───────────────────────┘
                                             │ external catalog
                                             ▼
                            ┌────────────────────────────────────────┐
                            │  StarRocks FE (:9030)                  │
                            │    └── CN (stateless, 读 S3 Parquet)   │
                            │                                        │
                            │  SELECT * FROM                         │
                            │    iceberg_catalog.qaautotest           │
                            │    .event_result;                      │
                            └────────────────────────────────────────┘
```

### 关键配置

| 配置项 | 值 |
|--------|-----|
| Kafka topic | `duckdb_fp_velocity-al-.qaautotest` (3 分区, RF=3) |
| Connect group | `iceberg-connect-qaautotest` |
| Connect replication factor | 3 |
| Connect commit interval | 60s |
| Iceberg table | `qaautotest.event_result` |
| S3 warehouse | `s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/` |
| StarRocks S3 data | `cre-6630/duckdb/starrocks-data/` (shared-data 内部数据) |
| SMT 读取 | `fp-mysql.duckdb:3306/qaautotest` (dv.ro, 只读) |

---

## 3. 如何 Trigger 和验证

### 3.1 数据来源：fp-async 产生的 velocity-al 消息

在生产环境中，数据流是：

```
客户请求 → FP API (Detection) → Kafka velocity topic
    → fp-async (Consumer) → 写 YugabyteDB + 产生 velocity-al 消息
    → Kafka velocity-al topic ← 这是我们消费的数据源
```

fp-async 写到 `velocity-al` topic 的消息格式（示例来自 QA Kafka，消息结构与标准生产 topic `{cluster}_fp_velocity-al-.{tenant}` 相同）:

```json
{
  "eventId": "cre6630-e1",
  "eventType": "login",
  "userId": "u-cre6630-1",
  "eventTime": 1774420000000,
  "processingTime": 1774420001000,
  "featureMap": {
    "1": "foo",        ← feature ID=1 的值（String 类型）
    "2": 123,          ← feature ID=2 的值（Integer 类型）
    "3": true           ← feature ID=3 的值（Boolean 类型）
  }
}
```

**关键字段说明：**

| 字段 | 含义 | 来源 |
|------|------|------|
| `eventId` | 事件唯一 ID | FP 分配 |
| `eventType` | 事件类型 (transaction, login 等) | 客户传入 |
| `userId` | 用户/实体 ID | 客户传入 |
| `eventTime` | 事件发生时间 (epoch ms) | 客户传入 |
| `processingTime` | FP 处理时间 (epoch ms) | FP 分配 |
| `featureMap` | **整数 ID → 值** 的 map | FP 计算后输出 |

### 3.2 SMT 转换：featureMap 整数 ID → 列名

**为什么 featureMap 是整数 ID？**

fp-async 产生的 velocity-al 消息里，featureMap 的 key 是整数，不是名字——这是为了节省 Kafka 消息体积（高频场景，每个事件都有几百个 feature）：

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

**SMT 做的事：**

`FeatureResolverTransform` 在 Kafka Connect 处理每条消息时实时翻译：

```
启动时 + 每 60s 刷新缓存:
  SELECT id, name, return_type
  FROM qaautotest.feature
  WHERE status = 'PUBLISHED'
  → qaautotest 有 705 个 PUBLISHED features

  建立内存缓存:
  { "8":  ["amount",      "Double"],
    "7":  ["country",     "String"],
    "15": ["merchant_id", "String"],
    ...  (705 条) }

处理每条消息:
  featureMap {"8": 299.99, "7": "US", "15": "merchant_xyz"}
       ↓ 查缓存，按 return_type 做类型转换
  amount      = 299.99  → DOUBLE
  country     = "US"    → STRING
  merchant_id = "merchant_xyz" → STRING

输出完整 Schema 的 Kafka Connect Record:
  event_id | event_type    | user_id  | event_time    | amount | country | merchant_id | ...
  abc-123  | transaction   | user_42  | 1774597200000 | 299.99 | US      | merchant_xyz| ...
       ↓
  写入 Iceberg Parquet (列式, 类型正确)
       ↓
  StarRocks 可以直接:
  SELECT amount, country FROM iceberg_catalog.qaautotest.event_result
```

**Iceberg 表结构：**

| 列 | 来源 | 类型 |
|----|------|------|
| event_id | 固定字段 | STRING |
| event_type | 固定字段 | STRING |
| user_id | 固定字段 | STRING |
| event_time | 固定字段 | LONG |
| processing_time | 固定字段 | LONG |
| amount | featureMap 解析 | DOUBLE |
| country | featureMap 解析 | STRING |
| merchant_id | featureMap 解析 | STRING |
| ... (约 700 列) | featureMap 解析 | 按 return_type |

Schema 是**动态的**：新增 feature 时 SMT 刷新缓存，Iceberg 自动 evolve schema（`iceberg.tables.evolve-schema-enabled: true`）。

**本次测试的 featureMap 是空的 `{}`**，所以 Parquet 里只有 5 个固定列有值，其余约 700 列全为 NULL。

### 3.3 本次测试用的数据

由于 `duckdb_fp_velocity-al-.qaautotest` topic 保留期只有 24h（`retention.ms=86400000`），
且 FP 服务无法加载 `qaautotest` 租户，无法通过正常数据流产生消息。

**用 kafka-console-producer 手动写入测试消息：**

```bash
kubectl -n duckdb exec kafka3-0 -- bash -c '
for i in $(seq 1 20); do
  echo "{\"eventId\":\"test-$i\",\"eventType\":\"transaction\",\"userId\":\"user_$i\",\"eventTime\":1774593600000,\"processingTime\":1774593601000,\"featureMap\":{}}"
done | kafka-console-producer.sh --bootstrap-server localhost:9092 \
  --topic duckdb_fp_velocity-al-.qaautotest 2>/dev/null
'
```

**注意：** 测试消息的 `featureMap` 为空 `{}`，所以经过 SMT 后所有 feature 列都是 NULL。
只有 5 个固定列有值：`event_id`, `event_type`, `user_id`, `event_time`, `processing_time`。

实际写入的消息内容：
```json
{"eventId":"helm-test-1","eventType":"payment","userId":"helm_user_1","eventTime":1774597200000,"processingTime":1774597201000,"featureMap":{}}
```

如果想测试 feature 列有值，需要模拟真实 featureMap（用 qaautotest 租户的 feature ID）：
```json
{"eventId":"rich-test-1","eventType":"transaction","userId":"user_1","eventTime":1774597200000,"processingTime":1774597201000,"featureMap":{"123":299.99,"456":"US","789":true}}
```
其中 `123`, `456`, `789` 需要替换为 `qaautotest.feature` 表中实际的 PUBLISHED feature ID。

### 3.4 验证时间线

本次实际验证过程（手动部署）：

```
06:27  部署 Iceberg Catalog MySQL, REST Catalog, Kafka Connect, StarRocks FE+CN
06:43  StarRocks init job 完成 (CN 注册 + Iceberg catalog 创建)
06:44  iceberg-kafka-connect-register job 完成 (connector 注册)
06:44  写入 10 条测试消息 (batch 1: e2e-test-1 ~ e2e-test-10)
06:45  Kafka Connect 消费消息 (consumer group lag → 0)
06:46  等待 commit interval (60s)
06:47  第一次 commit: 0 tables (协调机制初始化中)
06:48  写入 20 条测试消息 (batch 2: e2e-batch2-1 ~ e2e-batch2-20)
06:50  Commit: 30 records → qaautotest.event_result ✅
06:50  StarRocks 查询: SELECT COUNT(*) = 30 ✅
```

Helm 部署验证：

```
06:58  helm install (清理手动部署后重新安装)
07:01  所有 pod Running
07:01  写入 15 条 (helm-test-1~15) + 20 条 (helm-v2-1~20) 测试消息
07:05  Commit: 35 records → qaautotest.event_result ✅
07:05  StarRocks 查询: SELECT COUNT(*) = 35 ✅
```

### 3.5 逐环节 Check 命令

**① 检查 Kafka topic 有数据**
```bash
# 看 earliest vs latest offset，差值 > 0 就有数据
kubectl -n duckdb exec kafka3-0 -- bash -c "
  echo '=== earliest ===' && \
  kafka-get-offsets.sh --bootstrap-server localhost:9092 --topic duckdb_fp_velocity-al-.qaautotest --time -2 && \
  echo '=== latest ===' && \
  kafka-get-offsets.sh --bootstrap-server localhost:9092 --topic duckdb_fp_velocity-al-.qaautotest
"
```

**② 检查 Connector 状态**
```bash
kubectl -n duckdb exec <connect-pod> -- \
  curl -s http://localhost:8083/connectors/iceberg-sink-qaautotest/status
# 期望: connector.state=RUNNING, tasks[*].state=RUNNING
```

**③ 检查 Consumer Group 消费进度**
```bash
# 先找 group 名 (不是你配的 CONNECT_GROUP_ID!)
kubectl -n duckdb exec kafka3-0 -- bash -c \
  "kafka-consumer-groups.sh --bootstrap-server localhost:9092 --list | grep iceberg"
# 输出: connect-iceberg-sink-qaautotest       ← 用这个
#        connect-iceberg-sink-qaautotest-coord

kubectl -n duckdb exec kafka3-0 -- bash -c \
  "kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
   --describe --group connect-iceberg-sink-qaautotest"
# 期望: CURRENT-OFFSET = LOG-END-OFFSET (LAG=0)
```

> **坑：** Kafka Connect 的 consumer group 名不是你在 `CONNECT_GROUP_ID` 里配的值，
> 而是 `connect-<connector-name>` 格式，即 `connect-iceberg-sink-qaautotest`。

**④ 检查 Kafka Connect 日志中的 commit**
```bash
kubectl -n duckdb logs <connect-pod> --tail=50 | grep -E 'Commit|Committed|table|record'
# 期望: "Committed snapshot XXX (MergeAppend)"
#        "addedRecords=CounterResult{unit=COUNT, value=20}"
# 失败: "committed to 0 table(s)" → 等下一个 60s cycle
```

**⑤ 检查 S3 数据文件**
```bash
aws s3 ls s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/qaautotest/ --recursive
# 期望: .../event_result/data/00000-xxx.parquet 文件
```

**⑥ 检查 StarRocks 查询**
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

### 3.6 最终查询结果

**手动部署（30 rows）：**
```
mysql> SELECT COUNT(*) as total_rows FROM iceberg_catalog.qaautotest.event_result;
+------------+
| total_rows |
+------------+
|         30 |
+------------+

mysql> SELECT event_id, event_type, user_id FROM iceberg_catalog.qaautotest.event_result LIMIT 5;
+----------------+-------------+---------+
| event_id       | event_type  | user_id |
+----------------+-------------+---------+
| e2e-batch2-1   | transaction | user_1  |
| e2e-batch2-2   | transaction | user_2  |
| e2e-batch2-3   | transaction | user_3  |
| e2e-batch2-4   | transaction | user_4  |
| e2e-batch2-5   | transaction | user_5  |
+----------------+-------------+---------+
```

**Helm 部署（35 rows）：**
```
mysql> SELECT COUNT(*) as total_rows FROM iceberg_catalog.qaautotest.event_result;
+------------+
| total_rows |
+------------+
|         35 |
+------------+

mysql> SELECT event_id, event_type, user_id FROM iceberg_catalog.qaautotest.event_result LIMIT 5;
+-------------+----------+-------------+
| event_id    | event_type | user_id    |
+-------------+----------+-------------+
| helm-test-1 | payment  | helm_user_1 |
| helm-test-2 | payment  | helm_user_2 |
| helm-test-3 | payment  | helm_user_3 |
| helm-test-4 | payment  | helm_user_4 |
| helm-test-5 | payment  | helm_user_5 |
+-------------+----------+-------------+
```

**完整行结构（约 705 列，因 featureMap 为空所以 feature 列全为 NULL）：**
```
mysql> SELECT * FROM iceberg_catalog.qaautotest.event_result LIMIT 1\G
*************************** 1. row ***************************
         event_id: helm-test-1
       event_type: payment
          user_id: helm_user_1
       event_time: 1774597200000
  processing_time: 1774597201000
           amount: NULL          ← featureMap 为空，feature 列全 NULL
          country: NULL
      merchant_id: NULL
            ... (约 700 个 feature 列，全 NULL)
```

**对比：QA 环境有真实 fp-async 数据时的完整行：**
```
mysql> SELECT * FROM iceberg_catalog.yysecurity.event_result LIMIT 1\G
         event_id: test_event_cre6630_001
       event_type: trans
          user_id: test_user_cre6630
          country: US            ← SMT 从 featureMap 解析出来
           amount: 299.99        ← Double 类型
      merchant_id: merchant_xyz  ← String 类型
        direction: credit
    max_sum_card: 3000
  sum_card_amount: 1500
            rules: [1]
          actions: [REJECT]
```

### 3.7 S3 实际文件结构（已验证）

运行 `aws s3 ls s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/ --recursive` 得到：

```
data/
  00001-...parquet   (手动部署第1批, 06:49, ~188KB)
  00001-...parquet   (手动部署第2批, 06:50, ~188KB)
  00001-...parquet   (Helm 部署,     07:05, ~188KB)

metadata/
  00000-...metadata.json   ← v0 表定义 (初始 schema)
  00000-...metadata.json   ← v0 表定义 (Helm 重建后)
  00001-...metadata.json   ← v1 表定义 (第1次 commit 后更新)
  00002-...metadata.json   ← v2 表定义 (第2次 commit 后更新)
  snap-XXX.avro            ← snapshot 文件 (每次 commit 一个)
  XXX-m0.avro              ← manifest 文件 (记录本次 commit 包含哪些 parquet)
```

**各类文件的作用：**

| 文件类型 | 例子 | 作用 |
|---------|------|------|
| `*.parquet` | `data/00001-xxx.parquet` | 实际行数据，列式存储，~188KB/批 |
| `*.metadata.json` | `metadata/00002-xxx.metadata.json` | 表的当前状态：schema、分区定义、当前 snapshot 指针 |
| `snap-xxx.avro` | `metadata/snap-8768xxx.avro` | Snapshot 描述：本次 commit 的时间戳、操作类型 (append)、指向哪个 manifest |
| `xxx-m0.avro` | `metadata/9b5769xx-m0.avro` | Manifest 文件：列出本 snapshot 包含的所有 parquet 文件路径 + 统计信息 |

**查一条数据时的读取链路：**

```
SELECT * FROM iceberg_catalog.qaautotest.event_result
    ↓
StarRocks FE 查 Iceberg REST Catalog
    ↓ 找到最新的 metadata.json
metadata.json → 指向当前 snapshot (snap-xxx.avro)
    ↓
snap-xxx.avro → 指向 manifest (xxx-m0.avro)
    ↓
xxx-m0.avro  → 列出所有 parquet 文件路径
    ↓
StarRocks CN 并行读取 S3 上的 .parquet 文件
    ↓
返回结果
```

每次 Kafka Connect commit (60s) = 1 个新 parquet 文件 + 更新一次 metadata 链。
这就是为什么需要 **Compaction CronJob** — 文件会越积越多，需要定期合并。

### 3.8 完整验证 Checklist

- [ ] `kubectl get pods -nduckdb | grep -E 'starrocks|iceberg'` → 全部 Running
- [ ] StarRocks FE 接受 JDBC 连接 (port 9030)
- [ ] `SHOW COMPUTE NODES;` → CN Alive=true
- [ ] `SHOW CATALOGS;` → iceberg_catalog 存在
- [ ] Connector status → RUNNING, 2 tasks RUNNING
- [ ] 写入测试消息 → 等 60-120s (commit interval + 初始化)
- [ ] Kafka Connect 日志: `Committed snapshot` + `addedRecords > 0`
- [ ] `SELECT COUNT(*)` → 行数匹配写入数
- [ ] `SELECT *` → 固定字段正确 (event_id, event_type, user_id, event_time, processing_time)

---

## 4. 踩过的坑

### 坑 1: Iceberg REST Catalog — `java.ext.dirs` 不支持 (Java 17+)

**现象：** Pod CrashLoopBackOff，日志：
```
Error: -Djava.ext.dirs=/drivers is not supported. Use -classpath instead.
```

**原因：** `tabulario/iceberg-rest:1.6.0` 使用 Java 17，`java.ext.dirs` 在 Java 9+ 已移除。

**解决：** 不用 `JAVA_TOOL_OPTIONS`。改为：
1. init container 下载 MySQL JDBC JAR 到 emptyDir
2. main container 用 subPath 挂载到 `/usr/lib/iceberg-rest/mysql-connector-j-8.0.33.jar`
3. **必须** override container command，用 `-cp` 代替 `-jar`：
   ```yaml
   command:
     - java
     - -cp
     - /usr/lib/iceberg-rest/iceberg-rest-image-all.jar:/usr/lib/iceberg-rest/mysql-connector-j-8.0.33.jar
     - org.apache.iceberg.rest.RESTCatalogServer
   ```

**参考：** qa-security 的部署用了完全相同的方式（检查 running pod 的 command 字段确认）。

---

### 坑 2: StarRocks FE/CN — 容器启动即退出

**现象：** CrashLoopBackOff，stdout 无日志输出。

**原因：** `starrocks/fe-ubuntu:3.3-latest` 和 `starrocks/cn-ubuntu:3.3-latest` 镜像**没有 Docker ENTRYPOINT**。K8s 启动容器后没有前台进程，容器立即退出。

**解决：** 必须显式指定 command：
```yaml
# FE
command: ["/opt/starrocks/fe/bin/start_fe.sh"]

# CN
command: ["/opt/starrocks/cn/bin/start_cn.sh"]
```

`start_fe.sh` 不加 `--daemon` 参数时，使用 `exec java ...`（前台模式）。加了 `--daemon` 会 `nohup ... &` 后台运行然后退出。

**调试技巧：** FE 日志不输出到 stdout，而是写到 `/opt/starrocks/fe/log/fe.log` 和 `fe.out`。如果容器反复 crash：
1. 临时 patch command 为 `["sleep", "3600"]`
2. exec 进去手动运行 `start_fe.sh --daemon`
3. 查看 `fe.log` 和 `fe.warn.log`

---

### 坑 3: MySQL 客户端 — `--skip-ssl` 不识别

**现象：** StarRocks init Job 报 `mysql: [ERROR] unknown option '--skip-ssl'`

**原因：** `mysql:8.0` 镜像的客户端不支持 `--skip-ssl`（旧语法）。

**解决：** 改用 `--ssl-mode=DISABLED`。

---

### 坑 4: Kafka Connect — 消费了消息但 commit 0 tables

**现象：** Consumer group lag=0（消息已消费），但日志显示 `Commit ... complete, committed to 0 table(s)`。

**原因：** Iceberg sink connector 使用内部 `control-iceberg` topic 做 commit 协调。首次启动时协调机制需要一个完整的 commit cycle 来初始化。前几个 cycle 可能显示 0 tables。

**解决：** 等待 1-2 个 commit interval（60s × 2 = 2分钟）。如果持续为 0：
1. 确认 connector status 是 RUNNING（不是 FAILED）
2. 确认 consumer group 有 offset（不是全部 `-`）
3. 检查是否有 SMT 报错（`kubectl logs <connect-pod> | grep -i error`）
4. 检查 `control-iceberg` topic 是否被其他 connector 实例占用

**本次经验：** 手动部署首次 commit 就成功了。Helm 部署需要等 ~2 分钟后数据才出现。

---

### 坑 5: FP API 无法加载 qaautotest 租户

**现象：** `Tenant qaautotest can not be loaded!`

**原因：** duckdb namespace 的 FP 实例可能只加载了部分租户。`qaautotest` 虽然在 fp-mysql 中有数据库，但 FP 运行时未加载它。

**影响：** 无法通过 FP detection API 自然产生 velocity-al 消息。

**解决：** 用 `kafka-console-producer` 直接向 topic 写测试消息来验证。生产环境中 fp-async 会自然产生这些消息。

---

### 坑 6: Kafka Connect consumer group 名不是你配的

**现象：** 用 `kafka-consumer-groups.sh --describe --group iceberg-connect-qaautotest` 报 `GroupId is not a consumer group (connect)`。

**原因：** Kafka Connect 框架会自动生成 consumer group 名，格式是 `connect-<connector-name>`，而不是 `CONNECT_GROUP_ID` 的值。`CONNECT_GROUP_ID` 是 Connect 集群的 coordination group，不是数据消费的 group。

**解决：** 先 `--list` 再 `--describe`：
```bash
kafka-consumer-groups.sh --list | grep iceberg
# 输出: connect-iceberg-sink-qaautotest
#        connect-iceberg-sink-qaautotest-coord
```

---

## 5. 验证结果

### 手动部署 (manual-production/)

| 步骤 | 结果 |
|------|------|
| 写入 30 条测试消息 | ✅ |
| Kafka Connect 消费并写入 S3 | ✅ 2 个 data files, ~377KB |
| StarRocks 查询 | ✅ `SELECT COUNT(*) = 30` |
| 字段正确 | ✅ event_id, event_type, user_id 全部匹配 |

### Helm 部署 (charts-production/)

| 步骤 | 结果 |
|------|------|
| `helm lint` | ✅ 0 chart(s) failed |
| `helm install` | ✅ 21 resources created |
| 全部 Pod Running | ✅ |
| 写入 35 条测试消息 | ✅ |
| StarRocks 查询 | ✅ `SELECT COUNT(*) = 35` |

---

## 7. 生产架构目标 vs 当前部署

> 详细设计见 [brainstorm-summary.md](../brainstorm-summary.md) 和 [sizing-sheet.md](../sizing-sheet.md)

### 7.1 当前 (dev_a 验证) vs 生产目标

| 维度 | 当前 dev_a 部署 | 生产目标 |
|------|----------------|----------|
| StarRocks FE | ×1, 无 HA | ×3 HA (m6i.xlarge 4C/16G) |
| StarRocks CN | ×1 通用 | CN Query ×2-3 (常驻) + CN Refresh ×0-4 (凌晨 spot) |
| CN Pool 隔离 | 无 | Query Pool + Refresh Pool 分开 |
| Kafka Connect | ×1 worker | ×2-3 workers |
| Iceberg Catalog MySQL | Pod 内 MySQL | RDS MySQL |
| Compaction | 未配置 | CronJob 每天凌晨 |
| 监控 | 无 | Prometheus + Grafana |
| 多租户 | 单 tenant | 每 tenant connector + Iceberg DB |

### 7.2 Scaling 策略

> **当前 manifests 和 Helm chart 不包含 HPA 和 dcluster 配置，这些是 TODO。**
> 下面是生产目标设计。

**哪个层面做 scaling:**

| 组件 | 方式 | 当前状态 | 说明 |
|------|------|---------|------|
| FE (3 nodes) | **固定不动** | ✅ 已实现 (×1) | 查询规划, 不需要弹缩 |
| Kafka Connect | **固定不动** | ✅ 已实现 (×1) | 吞吐稳定, 极少需要调整 |
| CN Query | **HPA (reactive)** | ❌ TODO | 在线查询, 负载不可预测 |
| CN Refresh | **dcluster + spot (proactive)** | ❌ TODO | 定时批任务, 跑完释放 |

**为什么 batch 用 dcluster 而非 HPA:**

| 维度 | HPA | dcluster + spot |
|------|-----|-----------------|
| 成本 | on-demand 全价 | spot 省 60-70% |
| 时机 | 被动响应 | 主动定时, 任务前已 ready |
| Spot 中断 | 不感知 | 可配 fallback on-demand |
| 适用 | 在线查询 (不知何时来) | 批任务 (知道何时跑) |

**CN 的两种角色及部署方式:**

| | CN Query Pool | CN Refresh Pool |
|--|---|---|
| 用途 | 服务 fp-async Stats API 实时查询 | 凌晨 MV 刷新（扫原始数据做 GROUP BY）|
| 常驻 | 是，replicas=2-3 | 否，平时 replicas=0 |
| K8s 类型 | Deployment (无状态, 数据在 S3) | Deployment (平时 scale=0，CronJob 控制) |
| 机器类型 | On-Demand / RI | Spot (通过 dcluster 申请) |
| 节点选择 | 普通节点 | dcluster 申请的 spot 节点 (nodeSelector) |

> **CN 不需要 StatefulSet。** Shared-Data 模式下 CN 完全无状态，数据全在 S3，本地只有 cache。随时可以起/停。

**dcluster 是谁的工作？**

- **完全是 infra 的工作**，不进 fp-async 代码，不在 FP 业务逻辑里
- dcluster 只负责 EC2 资源申请和释放，CN pod 的起停和向 FE 注册是 infra 的 CronJob 负责

**CN Refresh 完整流程 (生产目标, 需要 infra 实现):**

```
01:00  K8s CronJob 触发 (infra 写的脚本/Job)
  ├─ ① 调用 dcluster API: 申请 N 台 spot EC2 (r6i.2xlarge)
  │     dcluster 负责: 起 EC2 → 配置 → join K8s 集群
  ├─ ② 等待 spot 节点出现在 K8s:
  │     kubectl wait node --selector=dcluster-pool=cn-refresh --for=condition=Ready
  ├─ ③ Scale up CN Refresh Deployment:
  │     kubectl scale deployment/starrocks-cn-refresh --replicas=3
  │     (pod 自动调度到 spot 节点 via nodeSelector)
  ├─ ④ 等 CN pod Ready 后，向 FE 注册:
  │     mysql -h starrocks-fe -P 9030 -u root -e
  │       "ALTER SYSTEM ADD COMPUTE NODE 'cn-refresh-pod-1:9050';
  │        ALTER SYSTEM ADD COMPUTE NODE 'cn-refresh-pod-2:9050';
  │        ALTER SYSTEM ADD COMPUTE NODE 'cn-refresh-pod-3:9050';"
  ├─ ⑤ 等待 StarRocks MV refresh 完成
  │     (FE 凌晨自动触发，或 mysql 执行 REFRESH MATERIALIZED VIEW)
  ├─ ⑥ 从 FE 注销 CN Refresh:
  │     "ALTER SYSTEM DROP COMPUTE NODE 'cn-refresh-pod-1:9050'; ..."
  ├─ ⑦ Scale down:
  │     kubectl scale deployment/starrocks-cn-refresh --replicas=0
  └─ ⑧ 调用 dcluster API: 释放 spot 机器

~04:30 以后: fp-async 查询 Stats API
  → StarRocks FE → CN Query Pool (常驻, 独立 Deployment)
  → 读 MV (KB 级) → 毫秒返回
```

**在线查询需要多少 scaling？**

如果 MV 设计合理: **几乎不需要**。在线查询只是读 MV 的几千行 (KB 级), 常驻 2-3 个小 CN 就够了。

**HPA / dcluster 实现 TODO:**

| 项 | 需要做的事 | 谁做 |
|----|-----------|------|
| 拆分 CN Query / Refresh 两个 Deployment | 现在合并在一个 Deployment，需要拆开 + nodeSelector | infra |
| CN Query HPA | HPA 资源 (基于 CPU/内存 trigger) | infra |
| CN Refresh CronJob | shell 脚本封装 dcluster 申请 + CN 起停 + FE 注册 | infra |
| StarRocks Resource Group | `CREATE RESOURCE GROUP` 隔离 Query/Refresh 资源 | infra |
| dcluster API 对接 | 了解 dcluster 申请接口的具体参数 | infra (需查 dcluster 文档) |

### 7.3 资源配置参考 (Medium tenant 起步)

| 组件 | 规格 | 数量 | 月成本 (1yr RI) |
|------|------|------|----------------|
| StarRocks FE | m6i.xlarge (4C/16G) | 1 | $87 |
| StarRocks CN Query | r6i.xlarge (4C/32G) | 2-3 | $229-$344 |
| StarRocks CN Refresh | r6i.2xlarge spot (4hr/day) | 2 | ~$15 |
| Kafka Connect Worker | m6i.xlarge (4C/16G) | 2 | $174 |
| Iceberg REST Catalog | m6i.large (2C/8G) | 1 | ~$70 |
| S3 (180d retention) | 7.7 TB | - | $177 |
| **合计** | | | **~$1,100-1,400/月** |

详细 4 tier sizing → [sizing-sheet.md](../sizing-sheet.md)

### 7.4 跨 Cluster 部署 (Per-cluster)

决策：每个 cluster (A/B) 各部署一套，不做中心化。理由：

1. 跟 ClickHouse、YugabyteDB、Kafka 模式一致
2. fp-async JDBC 查询 StarRocks 需要 cluster 内低延迟
3. A/B failover 零额外工作
4. 故障隔离

**Active vs Standby 资源:**

| 组件 | Active Cluster | Standby Cluster |
|------|---------------|-----------------|
| Kafka Connect | 2-3 workers (正常运行) | 2-3 workers (**持续运行**, 保持 S3 数据同步) |
| StarRocks FE | ×3 (HA) | ×1 (最小) |
| StarRocks CN Query | ×2-3 | ×1 (warm standby) |
| StarRocks CN Refresh | ×0-4 (凌晨 spot) | ×0 |
| Standby 月成本增量 | — | ~$200-300 |

**关键：** Standby 的 Kafka Connect 持续运行写 S3，failover 时 StarRocks scale up CN 即可立即服务。

### 7.5 多租户 Onboarding

共享基础设施 (部署一次)：FE、CN Pools、Connect Workers、REST Catalog、S3 Bucket

Per-tenant (每个 client)：
- Kafka Connect **connector** (JSON 配置, curl 注册到共享 worker)
- Iceberg **database** (如 `yysecurity.raw_events`)
- StarRocks **MV** (fp-async 自动创建)
- S3 **路径前缀** (如 `s3://bucket/raw_events/tenant=yysecurity/`)

**新 tenant 注册:**
```bash
curl -X POST http://iceberg-kafka-connect:8083/connectors -d '{
  "name": "iceberg-sink-{tenant}",
  "config": {
    "topics": "duckdb_fp_velocity-al-.{tenant}",
    "iceberg.tables": "{tenant}.event_result",
    "transforms.featureResolver.metadata.jdbc.url": "jdbc:mysql://fp-mysql:3306/{tenant}",
    ... (其余同模板)
  }
}'
```

### 7.6 剩余 TODO (行动优先级)

| 优先级 | 事项 | 状态 | 说明 |
|--------|------|------|------|
| **P0** | StarRocks FE+CN 分离部署 | ✅ 完成 | dev_a 验证通过 |
| **P0** | Helm Chart | ✅ 完成 | charts-production/ |
| **P0** | CN 分 Query/Refresh 两个 pool | ❌ 待做 | 需要 StarRocks Resource Group + 两组 CN Deployment |
| **P1** | Compaction CronJob | ❌ 待做 | 防 S3 小文件爆炸 |
| **P1** | S3 Lifecycle Rule | ❌ 待做 | 热 180d → 温 IA → 冷 Glacier → 删 |
| **P1** | 监控 | ❌ 待做 | FE/CN `:8030/metrics` → Prometheus → Grafana |
| **P2** | 多租户 connector 模板化 | ❌ 待做 | 新 tenant = 一条命令 |
| **P2** | dcluster 集成 | ❌ 待做 | spot CN 申请/注册/释放自动化 |
| **P2** | Iceberg Catalog MySQL → RDS | ❌ 待做 | 生产不用 Pod 内 MySQL |

---

## 8. 清理

```bash
# Helm 方式
helm uninstall starrocks-iceberg -n duckdb --kubeconfig=...dev_a.config
kubectl -n duckdb delete pvc data-iceberg-catalog-mysql-0 fe-meta-starrocks-fe-0

# 手动方式
kubectl -n duckdb delete job starrocks-init iceberg-kafka-connect-register
kubectl -n duckdb delete deployment starrocks-cn iceberg-kafka-connect iceberg-rest-catalog
kubectl -n duckdb delete statefulset starrocks-fe iceberg-catalog-mysql
kubectl -n duckdb delete svc starrocks-fe starrocks-cn iceberg-kafka-connect iceberg-rest-catalog iceberg-catalog-mysql
kubectl -n duckdb delete configmap starrocks-fe-config starrocks-cn-config iceberg-kafka-connect-connector fp-feature-smt-source
kubectl -n duckdb delete secret iceberg-catalog-mysql iceberg-rest-catalog-secret iceberg-smt-mysql
kubectl -n duckdb delete serviceaccount iceberg-rest-catalog iceberg-kafka-connect
kubectl -n duckdb delete pvc data-iceberg-catalog-mysql-0 fe-meta-starrocks-fe-0
```
