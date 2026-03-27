# CRE-6630 StarRocks 生产部署 — Brainstorm 总结

## 1. 需求本质

客户在 FP 控制台配置"统计视角"（Cube = Dimension + Measures + Target Features），系统**每天凌晨**自动预聚合（MV），之后客户随时查 30d/90d/180d 统计结果，**毫秒级返回**。

### 数据从重到轻的变化

| 阶段 | 数据量 (Medium tenant) | 谁处理 | 何时 |
|------|----------------------|--------|------|
| 原始事件 (S3 Parquet) | 43 GB/天, 1000万行 | Kafka Connect 写入 | 实时 |
| MV 预聚合结果 | ~200行/天, 几 KB | CN Refresh 扫原始数据 GROUP BY | 凌晨批处理 |
| 查询返回 | ~几千行, 几 KB JSON | CN Query 合并 daily 分区 | 在线请求 |

**在线查询极轻** — MV 把 TB 级数据压成了 KB 级。重活全在凌晨 batch。

---

## 2. 你 (Infra) 的职责边界

### 你做的 (Module B: 基础设施)

- 部署/运维: Kafka Connect, Iceberg REST Catalog, StarRocks (FE + CN)
- S3 Bucket + IAM + Lifecycle
- Compaction CronJob
- CN Refresh 弹性 (dcluster spot)
- 监控 (Prometheus + Grafana)
- 新 tenant onboarding: 注册 connector

### 后端做的 (fp-async Java)

- Cube/Dimension CRUD API
- 生成 `CREATE MATERIALIZED VIEW` DDL → 通过 JDBC 发给 StarRocks FE
- Stats 查询 SQL 拼装 + 结果格式化
- Partial Window 逻辑

---

## 3. StarRocks 核心概念

| 角色 | 比喻 | 职责 | 资源需求 |
|------|------|------|---------|
| **FE** (Frontend) | 餐厅领班 | 查询规划, MV 调度, 元数据管理 | 低 (4C/16G), 固定 3 节点 HA |
| **CN** (Compute Node) | 后厨厨师 | 扫数据, 聚合, 排序 — 真正干活 | 高, 是 scaling 主要对象 |
| **S3** | 大冷库 | 存所有数据 (Iceberg Parquet + MV segment) | 按量付费, 无限容量 |

Shared-Data 模式: CN 无状态, 数据全在 S3, 本地 SSD 只做 cache。CN 可随时加减。

---

## 4. 生产架构: Model C — Shared FE + CN Pool 隔离

```
                         ┌──────────────────────────────┐
                         │        fp-async (Java)        │
                         └──────────┬───────────────────┘
                                    │ JDBC :9030
                         ┌──────────▼───────────────────┐
                         │    StarRocks FE × 3 (HA)     │
                         │    常驻, On-Demand            │
                         └──┬────────────────────┬──────┘
                            │                    │
              ┌─────────────▼──────┐  ┌──────────▼─────────────┐
              │ CN Query Pool      │  │ CN Refresh Pool        │
              │ 常驻 2-3 节点       │  │ 凌晨 dcluster spot     │
              │ On-Demand / RI     │  │ 跑完即释放              │
              │ 在线查询 (10-50ms) │  │ MV 刷新 (分钟~小时级)   │
              └────────────────────┘  └────────────────────────┘
                      │                         │
                      ▼                         ▼
              ┌─────────────────────────────────────────┐
              │                   S3                     │
              │  /raw_events/ (Iceberg Parquet)          │
              │  /starrocks/  (MV segment cache)         │
              └─────────────────────────────────────────┘
                      ▲
              ┌───────┴──────────────────┐
              │ Kafka Connect Workers    │
              │ 2-3 个 Pod (共享)         │
              │ 内跑 N 个 connector/task  │
              └───────┬──────────────────┘
                      │
              ┌───────┴──────────────────┐
              │ Kafka: velocity-al-{t}   │
              └──────────────────────────┘
```

---

## 5. 跨 Cluster 部署拓扑: Per-cluster (Option A)

### 背景

当前 cloud 架构维护多个 region 和 cluster, Cluster A/B 一个 active 一个 standby。每个 cluster namespace 下有完整一套 FP-async、Kafka、ClickHouse、YugabyteDB 等。

### 决策: StarRocks 跟随每个 cluster 部署一套 (Per-cluster)

**不做中心化部署**, 理由:

1. **一致性** — 跟 ClickHouse、YugabyteDB、Kafka 的部署模式完全一致, 不引入新的运维模式, 团队熟悉
2. **数据路径局部性** — Kafka Connect 必须消费本地 Kafka, fp-async JDBC 查询 StarRocks 需要低延迟, 都要求 cluster 内通信
3. **A/B failover 零额外工作** — 切 cluster 时 StarRocks 跟着切, 不需要改 JDBC endpoint 或做跨 cluster 路由
4. **故障隔离** — 一个 cluster 的 StarRocks 挂了不影响其他 cluster
5. **Standby 成本极低** — StarRocks Shared-Data 模式下 CN 无状态, 数据全在 S3, standby cluster 只需最小配置

### Per-cluster 资源分配

| 组件 | Active Cluster | Standby Cluster |
|------|---------------|-----------------|
| Kafka Connect | 2-3 workers (正常运行) | 2-3 workers (正常运行, 持续写 S3 保持数据同步) |
| StarRocks FE | ×3 (HA) | ×1 (最小, 无需 HA) |
| StarRocks CN Query | ×2-3 (常驻) | ×1 (warm standby) |
| StarRocks CN Refresh | ×0-4 (凌晨 spot) | ×0 (不跑 batch) |
| Standby 月成本增量 | — | ~$200-300 |

### 关键: Standby 的 Kafka Connect 持续运行

Standby cluster 的 Kafka Connect 持续消费 Kafka 写 S3, 这样:
- S3 上的 Iceberg 数据始终是完整的
- Failover 时 StarRocks 只需要 scale up CN 即可立即服务
- 不需要在 failover 后追数据

### Pod 层面数据流 (单 cluster 内)

```
┌─ Cluster (namespace) ───────────────────────────────────────────┐
│                                                                   │
│  ┌──────────┐   velocity-al    ┌────────────────┐               │
│  │ fp-async  │ ────topic────→  │ Kafka Cluster   │               │
│  │ (Java)    │                 └───────┬─────────┘               │
│  └────┬──────┘                         │ consume                  │
│       │                                ▼                          │
│       │ JDBC :9030            ┌──────────────────┐               │
│       │ (Stats query)        │ Kafka Connect     │               │
│       │                      │ Workers (2-3 Pod) │               │
│       │                      └───────┬───────────┘               │
│       │                              │ write Parquet              │
│       │                              ▼                            │
│  ┌────▼─────────────────────────────────────────┐               │
│  │             StarRocks                         │               │
│  │  FE ×1~3  ← 查询规划 / MV 调度                │               │
│  │    ├── CN Query Pool (常驻, serve Stats API)  │               │
│  │    └── CN Refresh Pool (凌晨 spot, MV 刷新)   │               │
│  └──────────────────┬────────────────────────────┘               │
│                     │ read/write                                  │
└─────────────────────┼─────────────────────────────────────────────┘
                      ▼
             ┌─────────────────┐
             │       S3        │
             │ /raw_events/    │ ← Kafka Connect 写入
             │ /starrocks/     │ ← MV segment
             └─────────────────┘
```

### 被否决的方案

**中心化 StarRocks (Option B)** — fp-async 需跨 cluster JDBC 查询, 网络延迟 + 可靠性风险; StarRocks 成为所有 cluster 的 SPOF; 不 follow 现有架构模式。

**Per-region 共享 (Option C)** — 只在同一 region 内有大量小 cluster 且成本压力大时才考虑, 当前规模下属于 over-engineering。

---

## 6. 多租户隔离模型

### 共享 (部署一套, 所有 tenant 用)

- StarRocks FE × 3
- StarRocks CN Pools
- Kafka Connect Workers (2-3 个 Pod)
- Iceberg REST Catalog (1 个)
- S3 Bucket (1 个)

### Per-tenant (每个 client 独立)

- Kafka Connect **connector** (每 tenant 一个 JSON 配置, 跑在共享 worker 上)
- Iceberg **database** (每 tenant 一个, 如 `yysecurity.raw_events`)
- StarRocks **MV** (每 tenant × 每 Cube 自动创建, fp-async 管理)
- S3 **路径前缀** (如 `s3://bucket/raw_events/tenant=yysecurity/`)

### Kafka Connect 三层结构

| 层 | 是什么 | 数量 |
|----|--------|------|
| **Worker** | JVM 进程 / K8s Pod (你部署的) | 2-3 个, 固定 |
| **Connector** | 逻辑配置 JSON (curl 注册的) | 每 tenant 一个 |
| **Task** | 实际干活的线程 (框架自动分配到 worker) | 由 `tasks.max` 决定 |

Worker 挂了 → 框架自动把 task 重新分配到存活 worker (rebalance)。

### 新 tenant onboarding

```bash
# 只需注册一个 connector, 其余自动
curl -X POST http://kafka-connect:8083/connectors -d '{
  "name": "iceberg-sink-{tenant}",
  "config": { "topics": "velocity-al-{tenant}", "iceberg.tables": "{tenant}.raw_events", ... }
}'
```

---

## 7. Scaling 策略

### 在哪个层面做 scaling

| 组件 | 方式 | 说明 |
|------|------|------|
| FE (3 nodes) | 固定不动 | 查询规划, 不需要弹缩 |
| Kafka Connect | 固定不动 | 吞吐稳定, 极少需要调整 |
| CN Query | HPA (reactive) | 在线查询, 负载不可预测 |
| CN Refresh | dcluster + spot (proactive) | 定时批任务, 跑完释放 |

### 为什么 batch 用 dcluster 而非 HPA

| 维度 | HPA | dcluster + spot |
|------|-----|-----------------|
| 成本 | on-demand 全价 | spot 省 60-70% |
| 时机 | 被动响应 | 主动定时, 任务前已 ready |
| Spot 中断 | 不感知 | 可配 fallback on-demand |
| 适用 | 在线查询 (不知何时来) | 批任务 (知道何时跑) |

### CN Refresh dcluster 流程

```
00:00                                          06:00
  │  01:00 dcluster 申请 spot (4x r6i.2xlarge)   │
  │  01:10 机器 ready, join K8s, CN pods 启动      │
  │  01:20 StarRocks FE 触发 MV refresh            │
  │  ~04:30 刷新完成                                │
  │  04:40 CN 优雅下线, dcluster 释放 spot          │
```

### 在线查询实际上需要多少 scaling?

如果 MV 设计合理: **几乎不需要**。在线查询只是读 MV 的几千行 (KB 级), 常驻 2-3 个小 CN 就够了。

---

## 8. 资源配置 (Medium tenant 起步)

| 组件 | 规格 | 数量 | 月成本 (1yr RI) |
|------|------|------|----------------|
| StarRocks FE | m6i.xlarge (4C/16G) | 1 | $87 |
| StarRocks CN Query | r6i.xlarge (4C/32G) | 2-3 | $229-$344 |
| StarRocks CN Refresh | r6i.2xlarge spot (4hr/day) | 2 | ~$15 |
| Kafka Connect Worker | m6i.xlarge (4C/16G) | 2 | $174 |
| Iceberg REST Catalog | m6i.large (2C/8G) | 1 | ~$70 |
| S3 (180d retention) | 7.7 TB | - | $177 |
| **合计** | | | **~$1,100-1,400/月** |

详细 sizing 见 [sizing-sheet.md](sizing-sheet.md)。

---

## 9. QA → Prod 差异

| 维度 | QA (当前) | 生产 |
|------|----------|------|
| StarRocks | `allin1` standalone 1 pod | FE×3 + CN Query×2-3 + CN Refresh×0-4 |
| 部署方式 | 手动 YAML | StarRocks Operator + Helm |
| CN Refresh | 不存在 | dcluster spot, 凌晨起/释放 |
| Kafka Connect | 1 worker, 借用 duckdb Kafka | 2-3 workers, 生产 Kafka |
| Iceberg Catalog | Pod 内 MySQL | RDS MySQL |
| Compaction | 没配 | CronJob 每天凌晨 |
| 监控 | 手动 kubectl | Prometheus + Grafana |
| 多租户 | 单 tenant | 每 tenant connector + Iceberg DB |

---

## 10. 行动优先级

| 优先级 | 事项 | 说明 |
|--------|------|------|
| **P0** | StarRocks Operator 部署 | 替换 standalone, 支持 FE/CN 分离 |
| **P0** | CN 分 Query/Refresh 两个 pool | 在线查询 vs 批处理隔离 |
| **P1** | Compaction CronJob | 防 S3 小文件爆炸 |
| **P1** | S3 Lifecycle Rule | 热 180d → 温 IA → 冷 Glacier → 删 |
| **P1** | 监控 | FE/CN Prometheus → Grafana |
| **P2** | 多租户 connector 模板化 | 新 tenant = 一条命令 |
| **P2** | dcluster 集成 | spot CN 申请/注册/释放自动化 |

---

## 11. 产出文件

| 文件 | 内容 |
|------|------|
| [sizing-sheet.md](sizing-sheet.md) | 4 个 tenant tier 的详细资源配置、成本估算、scaling 触发条件 |
| [brainstorm-summary.md](brainstorm-summary.md) | 本文件 — 完整 brainstorm 总结 |
| Excalidraw 架构图 | Model C 多租户部署拓扑 (在 Excalidraw canvas 上) |
