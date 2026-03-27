# CRE-6630 Production Deployment Record — duckdb namespace on dev_a

**日期:** 2026-03-27
**集群:** dev_a (us-west-2)
**命名空间:** duckdb
**验证租户:** qaautotest

---

## 1. 部署流程

### 1.1 组件清单

| 组件 | K8s 类型 | Service | 镜像 |
|------|---------|---------|------|
| Iceberg Catalog MySQL | StatefulSet (1) | iceberg-catalog-mysql:3306 | mysql:8.0 |
| Iceberg REST Catalog | Deployment (1) | iceberg-rest-catalog:8181 | tabulario/iceberg-rest:1.6.0 |
| Kafka Connect + SMT | Deployment (1) | iceberg-kafka-connect:8083 | confluentinc/cp-kafka-connect-base:7.7.1 |
| StarRocks FE | StatefulSet (1) | starrocks-fe:9030 | starrocks/fe-ubuntu:3.3-latest (v3.3.22) |
| StarRocks CN | Deployment (1) | starrocks-cn:9050 | starrocks/cn-ubuntu:3.3-latest (v3.3.22) |

### 1.2 部署顺序

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

### 1.3 手动部署

```bash
cp 00-secrets.example.yaml 00-secrets.yaml
# 编辑密码
bash deploy-duckdb.sh
```

### 1.4 Helm 部署

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

fp-async 写到 `velocity-al` topic 的消息格式（真实示例，来自 QA topic `cre6630_fp_velocity.yysecurity`）:

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

`featureMap` 的 key 是 feature 的整数 ID（如 `"1"`, `"2"`），不是人类可读的名称。
**FeatureResolverTransform (SMT)** 查询 FP MySQL 的 `feature` 表做映射：

```sql
-- SMT 执行的查询 (fp-mysql.duckdb:3306/qaautotest, 用户 dv.ro, 只读)
SELECT id, name, return_type FROM feature WHERE status = 'PUBLISHED';
-- qaautotest 有 705 个 PUBLISHED features
```

转换过程：
```
输入 (Kafka JSON):
  {"featureMap": {"8": 299.99, "7": "US", "15": "merchant_xyz"}}
                     ↓ SMT 查 MySQL: id=8 → name=amount, type=Double
                     ↓              id=7 → name=country, type=String
                     ↓              id=15 → name=merchant_id, type=String
输出 (Iceberg Parquet):
  amount=299.99 (DOUBLE), country="US" (STRING), merchant_id="merchant_xyz" (STRING)
```

**结果：** Iceberg 表有 5 个固定列 + N 个 feature 列（qaautotest 约 705 列）。

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

### 3.7 完整验证 Checklist

- [ ] `kubectl get pods -nduckdb | grep -E 'starrocks|iceberg'` → 全部 Running
- [ ] StarRocks FE 接受 JDBC 连接 (port 9030)
- [ ] `SHOW COMPUTE NODES;` → CN Alive=true
- [ ] `SHOW CATALOGS;` → iceberg_catalog 存在
- [ ] Connector status → RUNNING, 2 tasks RUNNING
- [ ] 写入测试消息 → 等 60-120s (commit interval + 初始化)
- [ ] Kafka Connect 日志: `Committed snapshot` + `addedRecords > 0`
- [ ] `SELECT COUNT(*)` → 行数匹配写入数
- [ ] `SELECT *` → 固定字段正确 (event_id, event_type, user_id, event_time, processing_time)
kubectl -n duckdb exec starrocks-fe-0 -- \
  mysql -h 127.0.0.1 -P 9030 -u root -e "
    SHOW CATALOGS;
    SHOW COMPUTE NODES;
    SELECT COUNT(*) FROM iceberg_catalog.qaautotest.event_result;
    SELECT * FROM iceberg_catalog.qaautotest.event_result LIMIT 5;
  "


### 3.8 完整验证 checklist

- [ ] `kubectl get pods -nduckdb | grep -E 'starrocks|iceberg'` → 全部 Running
- [ ] StarRocks FE 接受 JDBC 连接 (port 9030)
- [ ] `SHOW COMPUTE NODES;` → CN Alive=true
- [ ] `SHOW CATALOGS;` → iceberg_catalog 存在
- [ ] Connector status → RUNNING, 2 tasks RUNNING
- [ ] 写入测试消息 → 等 60s（commit interval）
- [ ] `SELECT COUNT(*)` → 行数匹配
- [ ] `SELECT *` → 字段正确 (event_id, event_type, user_id, event_time, processing_time)

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

## 6. 清理

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
