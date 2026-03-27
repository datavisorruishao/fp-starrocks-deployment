# StarRocks Shared-Data Mode -- Production Sizing Sheet

**Context**: StarRocks as OLAP query engine over Iceberg tables (S3), consuming FP event data via Kafka Connect.
**Architecture**: FE (query planning) + CN (compute + local cache) -- no BE nodes, no local persistent storage.

---

## 1. Tenant Size Tiers

| Attribute | Small | Medium | Large | XL |
|-----------|-------|--------|-------|----|
| Events per second (QPS) | < 200 | 200 - 1,000 | 1,000 - 5,000 | 5,000 - 20,000 |
| Features per event | 20 - 50 | 50 - 150 | 100 - 300 | 200 - 500 |
| Avg event payload (JSON, pre-Parquet) | ~1 KB | ~3 KB | ~6 KB | ~10 KB |
| Avg row size (Parquet, columnar) | ~200 B | ~500 B | ~900 B | ~1.5 KB |
| Data retention (hot, S3 Standard) | 90 days | 180 days | 180 days | 365 days |
| Concurrent analytic queries | 2 - 5 | 5 - 15 | 10 - 30 | 20 - 50 |
| MV count (Cubes x target features) | 5 - 10 | 10 - 30 | 30 - 80 | 50 - 150 |

**Notes**:
- QPS = events arriving on `velocity-al-{tenant}` Kafka topic.
- Parquet row size is typically 5-7x smaller than raw JSON due to columnar compression + dictionary encoding.
- Concurrent queries include both Stats API queries from fp-async and ad-hoc exploration.

---

## 2. Per-Tier Data Volume Estimates

### 2.1 Daily Ingestion Volume

| Metric | Small | Medium | Large | XL |
|--------|-------|--------|-------|----|
| Events/day | 17M | 86M | 432M | 1.7B |
| Raw JSON/day | ~17 GB | ~258 GB | ~2.6 TB | ~17 TB |
| Parquet on S3/day | ~3.4 GB | ~43 GB | ~389 GB | ~2.6 TB |
| Parquet files/day (before compaction) | ~600 | ~3,000 | ~15,000 | ~60,000 |

**Calculation basis**: events/day = QPS x 86,400. Parquet size = rows x avg Parquet row size. File count assumes Kafka Connect flushes every 60s with N tasks.

### 2.2 Cumulative S3 Storage (Parquet only, hot tier)

| Period | Small | Medium | Large | XL |
|--------|-------|--------|-------|----|
| 30 days | 102 GB | 1.3 TB | 11.7 TB | 78 TB |
| 90 days | 306 GB | 3.9 TB | 35 TB | 234 TB |
| 180 days | 612 GB | 7.7 TB | 70 TB | -- |
| 365 days | -- | -- | -- | 949 TB |

### 2.3 Kafka Connect Sizing

| Parameter | Small | Medium | Large | XL |
|-----------|-------|--------|-------|----|
| Kafka partitions (velocity-al topic) | 6 | 12 | 24 | 48 |
| Connector tasks (= partitions) | 6 | 12 | 24 | 48 |
| Connect workers | 1 | 2 | 3 - 4 | 6 - 8 |
| Worker instance type | m6i.large | m6i.xlarge | m6i.xlarge | m6i.2xlarge |
| Commit interval | 60s | 60s | 60s | 30s |
| Memory per worker | 4 GB | 8 GB | 8 GB | 16 GB |

### 2.4 Iceberg Compaction

| Aspect | Recommendation |
|--------|---------------|
| Trigger | Daily cron, partition-level |
| Target file size | 256 MB - 512 MB |
| Method | StarRocks `OPTIMIZE TABLE` (v3.2+) or Iceberg `rewrite_data_files` via Spark |
| Pre-compaction file count/day (Medium) | ~3,000 x 60s flush = many small files |
| Post-compaction file count/day (Medium) | ~85 files (43 GB / 512 MB) |
| Compaction window | 02:00 - 05:00 UTC (off-peak) |
| Stale snapshot cleanup | `expire_snapshots` older than 3 days |

---

## 3. StarRocks Cluster Sizing

### 3.1 FE Nodes (Frontend -- Query Planning)

FE nodes handle SQL parsing, query planning, metadata management, and MV scheduling. With external Iceberg catalog, FE does not manage tablet metadata, so memory pressure is much lower than shared-nothing mode.

| Tier | FE Count | CPU | Memory | Disk | Instance Type |
|------|----------|-----|--------|------|---------------|
| Small | 1 | 4 vCPU | 8 GB | 50 GB SSD (logs + metadata) | m6i.xlarge |
| Medium | 1 | 4 vCPU | 16 GB | 100 GB SSD | m6i.xlarge |
| Large | 3 (1 leader + 2 follower) | 8 vCPU | 32 GB | 100 GB SSD | m6i.2xlarge |
| XL | 3 (1 leader + 2 follower) | 16 vCPU | 64 GB | 200 GB SSD | m6i.4xlarge |

**Why 3 FE for Large/XL**: Leader election requires odd quorum. Single FE is acceptable for Small/Medium since FE failure only causes brief query downtime (Iceberg data in S3 is safe).

**Key FE configs for shared-data**:
```properties
run_mode = shared_data
cloud_native_storage_type = S3
aws_s3_path = s3://bucket/starrocks/
```

### 3.2 CN Nodes (Compute Nodes -- Query Execution)

CN nodes perform all scan, filter, aggregate, and join operations. Local NVMe/SSD serves as a block cache for hot Parquet segments.

#### CN Query Pool (serves online Stats API queries)

| Tier | CN Count | CPU | Memory | Local Cache (SSD) | Instance Type |
|------|----------|-----|--------|-------------------|---------------|
| Small | 2 | 8 vCPU | 32 GB | 100 GB NVMe | r6i.xlarge |
| Medium | 3 | 16 vCPU | 64 GB | 200 GB NVMe | r6i.2xlarge |
| Large | 4 - 6 | 16 vCPU | 64 GB | 500 GB NVMe | r6i.2xlarge |
| XL | 8 - 12 | 32 vCPU | 128 GB | 1 TB NVMe | r6i.4xlarge |

#### CN Refresh Pool (MV daily refresh -- can scale to zero when idle)

| Tier | CN Count (during refresh) | CPU | Memory | Local Cache | Instance Type |
|------|--------------------------|-----|--------|-------------|---------------|
| Small | 1 | 8 vCPU | 32 GB | 100 GB | r6i.xlarge |
| Medium | 2 | 16 vCPU | 64 GB | 200 GB | r6i.2xlarge |
| Large | 3 - 4 | 16 vCPU | 64 GB | 500 GB | r6i.2xlarge |
| XL | 6 - 8 | 32 vCPU | 128 GB | 500 GB | r6i.4xlarge |

**Refresh pool lifecycle**: Scale up via CronJob before MV refresh window (e.g., 01:50 UTC), scale down after completion (e.g., 05:00 UTC). Use StarRocks Warehouse API or K8s HPA with custom metrics.

#### CN Memory Allocation Guidelines

| Component | % of Total Memory | Notes |
|-----------|-------------------|-------|
| Query execution (sort, agg, join) | 60% | `mem_limit` per query |
| Block cache (Parquet segments) | 30% | `block_cache_mem_size` |
| OS + JVM overhead | 10% | Reserved |

**Key CN configs**:
```properties
starlet_cache_dir = /data/block_cache
block_cache_disk_size = 80%  # of local SSD
block_cache_mem_size = 20%   # of CN memory
pipeline_dop = 0             # auto-detect parallelism
```

### 3.3 Resource Groups / Warehouses for Tenant Isolation

StarRocks 3.x supports Warehouses (shared-data) and Resource Groups for workload isolation.

```sql
-- Create warehouses for workload separation
CREATE WAREHOUSE query_wh WITH (
    min_cluster_count = 1,
    max_cluster_count = 3
);

CREATE WAREHOUSE refresh_wh WITH (
    min_cluster_count = 0,  -- scale to zero when idle
    max_cluster_count = 2
);

-- Per-tenant resource groups within a warehouse
CREATE RESOURCE GROUP rg_tenant_large
    TO (user = 'fp_async_large_tenant')
    WITH (
        cpu_weight = 10,
        mem_limit = '60%',
        concurrency_limit = 20,
        max_cpu_cores = 12
    );

CREATE RESOURCE GROUP rg_tenant_shared
    TO (user = 'fp_async_shared')
    WITH (
        cpu_weight = 5,
        mem_limit = '30%',
        concurrency_limit = 10,
        max_cpu_cores = 6
    );
```

---

## 4. Deployment Models

### 4.1 Comparison Table

| Aspect | Model A: Single Shared Cluster | Model B: Per-Group Clusters | Model C: Shared FE + CN Pool Segregation |
|--------|-------------------------------|----------------------------|------------------------------------------|
| **Description** | One FE + one CN pool for all tenants. Isolation via Resource Groups. | Large tenants get dedicated clusters. Small tenants share one cluster. | Shared FE layer, but CN nodes are tagged per tenant group. |
| **Tenant isolation** | Soft (Resource Groups, query quotas) | Hard (separate clusters) | Medium (CN pool affinity, Resource Groups) |
| **Cost efficiency** | Best -- full resource sharing | Worst -- idle capacity per cluster | Good -- shared FE, flexible CN pools |
| **Ops complexity** | Low -- single cluster to manage | High -- N clusters, N sets of monitoring | Medium -- single FE, but CN pool management |
| **Scaling flexibility** | Good -- add CN nodes to shared pool | Per-cluster scaling | Good -- scale CN pools independently |
| **Noisy neighbor risk** | High -- one bad query affects all | None | Low -- CN pools are isolated |
| **FE node count** | 1 - 3 | 1 - 3 per cluster | 3 shared |
| **CN node count** | Sum of all tiers | Duplicated across clusters | Sum of all tiers (but tagged) |
| **Best for** | < 10 tenants, similar sizes | Mix of very large + small tenants | 10-50 tenants, varied sizes |

### 4.2 Model Details

#### Model A: Single Shared Cluster

```
                 ┌────────────────────────────────┐
                 │          FE (1-3 nodes)         │
                 └───────────────┬────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
     ┌────────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐
     │  CN-1 (query)  │  │  CN-2 (query)│  │ CN-3 (refresh)│
     │  RG: large 60% │  │  RG: small   │  │  MV refresh   │
     │  RG: small 40% │  │    100%      │  │  (scale-to-0) │
     └────────────────┘  └──────────────┘  └───────────────┘
```

- Pros: Simple, cost-effective, easy to start.
- Cons: Resource Group limits are soft -- a runaway query can still impact others.
- Recommended for: Initial deployment, fewer than 10 tenants.

#### Model B: Per-Group Clusters

```
     ┌──────────────────────┐     ┌──────────────────────┐
     │  Cluster: Large-1    │     │  Cluster: Shared     │
     │  FE x1, CN x4       │     │  FE x1, CN x3       │
     │  Tenant: bank_a     │     │  Tenants: fintech_x, │
     │                      │     │   fintech_y, ...     │
     └──────────────────────┘     └──────────────────────┘
```

- Pros: Complete isolation, predictable performance.
- Cons: FE overhead per cluster, underutilized CN in small clusters.
- Recommended for: Tenants with strict SLA or regulatory isolation requirements.

#### Model C: Shared FE + CN Pool Segregation (Recommended)

```
                 ┌────────────────────────────────┐
                 │      Shared FE (3 nodes)       │
                 └───────────────┬────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
  ┌─────┴──────────┐    ┌───────┴────────┐    ┌─────────┴──────┐
  │ CN Pool: large │    │ CN Pool: shared│    │ CN Pool: refresh│
  │ Warehouse: wh1 │    │ Warehouse: wh2 │    │ Warehouse: wh_r │
  │ 4-6 CN nodes   │    │ 3-4 CN nodes   │    │ 0-4 CN nodes    │
  │ bank_a only    │    │ all small      │    │ MV refresh only │
  └────────────────┘    └────────────────┘    └────────────────┘
```

- Pros: Single FE management, CN pools can scale independently, refresh pool scales to zero.
- Cons: Slightly more complex warehouse/resource group configuration.
- Recommended for: Production multi-tenant deployment.

---

## 5. Scaling Triggers

### 5.1 CN Scale-Out Triggers

| Metric | Warning Threshold | Scale-Out Threshold | Action |
|--------|-------------------|---------------------|--------|
| CN CPU utilization (avg 5min) | > 60% | > 75% sustained 10min | Add 1 CN to pool |
| CN memory utilization | > 70% | > 85% | Add 1 CN to pool |
| Query P99 latency | > 2s | > 5s sustained 5min | Add 1 CN to pool |
| Query queue depth | > 5 | > 10 sustained 5min | Add 1 CN to pool |
| Block cache hit ratio | < 80% | < 60% | Increase local SSD or add CN |
| MV refresh duration | > 3 hours | > 5 hours | Add CN to refresh pool |

### 5.2 CN Scale-In Triggers

| Metric | Threshold | Action |
|--------|-----------|--------|
| CN CPU utilization (avg 30min) | < 25% | Remove 1 CN (min = tier minimum) |
| Refresh pool idle | No active MV refresh task | Scale refresh CN to 0 |

### 5.3 Tenant Graduation (Shared to Dedicated)

A tenant should move from shared CN pool to dedicated CN pool when:

| Signal | Threshold |
|--------|-----------|
| QPS exceeds | > 2,000 events/s sustained |
| Concurrent queries | > 15 regular queries |
| Query P99 in shared pool | > 3s (caused by this tenant) |
| MV count | > 50 MVs |
| S3 scan volume per query | > 50 GB regularly |
| Contractual SLA requirement | Any latency SLA < 500ms P99 |

### 5.4 HPA Configuration (Kubernetes)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: starrocks-cn-query-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: StatefulSet
    name: starrocks-cn-query
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
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
```

**Refresh pool CronJob scaler** (alternative to HPA for batch workloads):

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: starrocks-cn-refresh-scaleup
spec:
  schedule: "50 1 * * *"    # 01:50 UTC, before MV refresh at 02:00
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: scaler
              image: bitnami/kubectl:latest
              command:
                - kubectl
                - scale
                - statefulset/starrocks-cn-refresh
                - --replicas=3
```

---

## 6. Cost Estimation Framework

### 6.1 AWS Instance Pricing Reference (us-west-2, on-demand, Linux)

| Instance | vCPU | Memory | Network | On-Demand $/hr | 1yr RI $/hr | 3yr RI $/hr |
|----------|------|--------|---------|----------------|-------------|-------------|
| m6i.xlarge | 4 | 16 GB | Up to 12.5 Gbps | $0.192 | $0.121 | $0.086 |
| m6i.2xlarge | 8 | 32 GB | Up to 12.5 Gbps | $0.384 | $0.242 | $0.173 |
| r6i.xlarge | 4 | 32 GB | Up to 12.5 Gbps | $0.252 | $0.159 | $0.113 |
| r6i.2xlarge | 8 | 64 GB | Up to 12.5 Gbps | $0.504 | $0.318 | $0.226 |
| r6i.4xlarge | 16 | 128 GB | Up to 12.5 Gbps | $1.008 | $0.636 | $0.452 |
| i3en.xlarge | 4 | 32 GB + 2.5 TB NVMe | Up to 25 Gbps | $0.452 | $0.285 | $0.203 |

**Note**: CN nodes benefit from NVMe for block cache. Consider `i3en` instances for cache-heavy workloads, or use EBS gp3 volumes attached to `r6i` instances.

### 6.2 S3 Storage Costs

| Component | Unit Cost | Notes |
|-----------|-----------|-------|
| S3 Standard storage | $0.023/GB/month | Hot data |
| S3 Standard-IA | $0.0125/GB/month | Data > 180 days |
| S3 Glacier | $0.004/GB/month | Data > 365 days |
| S3 GET requests | $0.0004/1000 requests | CN reads during query |
| S3 PUT requests | $0.005/1000 requests | Kafka Connect writes |
| S3 data transfer (same region) | $0.00 | Free within region |

### 6.3 Monthly Cost per Tenant Tier (StarRocks + S3, Model C, 1yr RI)

| Component | Small | Medium | Large | XL |
|-----------|-------|--------|-------|----|
| **FE nodes** | 1x m6i.xlarge = $87 | 1x m6i.xlarge = $87 | shared 3x m6i.2xlarge = $58* | shared 3x m6i.4xlarge = $115* |
| **CN query** | 2x r6i.xlarge = $229 | 3x r6i.2xlarge = $688 | 5x r6i.2xlarge = $1,145 | 10x r6i.4xlarge = $4,579 |
| **CN refresh** | 1x r6i.xlarge x 4hr/day = $4 | 2x r6i.2xlarge x 4hr/day = $15 | 3x r6i.2xlarge x 4hr/day = $23 | 6x r6i.4xlarge x 4hr/day = $92 |
| **S3 storage (hot, 90d/180d)** | 306 GB = $7 | 7.7 TB = $177 | 70 TB = $1,610 | 949 TB = $21,827 |
| **S3 requests** | ~$2 | ~$15 | ~$50 | ~$200 |
| **Kafka Connect** | 1x m6i.large = $69 | 2x m6i.xlarge = $174 | 3x m6i.xlarge = $261 | 6x m6i.2xlarge = $1,045 |
| **Iceberg REST Catalog** | shared = $10 | shared = $10 | shared = $10 | shared = $10 |
| **Total (monthly)** | **~$408** | **~$1,166** | **~$3,157** | **~$27,868** |

*FE cost is shared across tenants in Model C; per-tenant share shown.

### 6.4 Comparison: StarRocks (Shared-Data) vs ClickHouse Cluster

| Dimension | StarRocks Shared-Data + Iceberg | ClickHouse (Shared-Nothing, replicated) |
|-----------|--------------------------------|----------------------------------------|
| **Architecture** | Compute-storage separation. CN stateless. | Each node stores data locally. Replicated. |
| **Storage cost (Medium tier, 180d)** | S3: $177/mo (cheap, tiered) | EBS gp3: ~$800/mo (3x replication) |
| **Compute scaling** | Add/remove CN in minutes. Scale to zero for batch. | Add node = rebalance shards (hours). |
| **Operational overhead** | Low -- CN is disposable, S3 is durable | Medium -- shard management, replication monitoring |
| **Query latency (point lookup)** | ~50-200ms (with MV + cache) | ~10-50ms (local disk) |
| **Query latency (full scan, 1 day)** | ~200ms-2s (depends on cache) | ~100ms-1s (local SSD) |
| **Iceberg native support** | Yes (external catalog) | Limited (requires ClickHouse-Iceberg connector, less mature) |
| **Multi-tenant isolation** | Warehouses + Resource Groups | Separate databases, no resource isolation |
| **Cost for Medium tier (monthly)** | ~$1,166 | ~$1,500 - $2,000 (3-node r6i.2xlarge + EBS) |
| **Cost at XL scale (monthly)** | ~$27,868 (S3 dominates) | ~$15,000 - $20,000 (less storage cost if shorter retention) |

**Key takeaway**: StarRocks shared-data wins on operational simplicity and elastic scaling. ClickHouse wins on raw query latency for local-disk workloads. For this use case (Iceberg-first, multi-tenant, variable load), StarRocks shared-data is the better fit.

---

## 7. FP Async Impact Analysis

### 7.1 Current FP Async Data Flow

```
Kafka: velocity-al-{tenant}
    ├── ConsumerForCH (existing) ──> ClickHouse (EventResult)
    └── Kafka Connect Iceberg Sink (NEW) ──> S3 Parquet ──> StarRocks
```

### 7.2 What Changes and What Does Not

| Aspect | Impact | Details |
|--------|--------|---------|
| FP Async code changes | **None** | Kafka Connect is an independent consumer group. No FP code modification. |
| FP Async configuration | **None** | `EventResultKafkaProducer` already writes to `velocity-al-{tenant}`. |
| Kafka topic changes | **None** | Same topic, new consumer group (`iceberg-connect`). |
| ConsumerForCH behavior | **No change** | Independent consumer group, independent offsets. |
| Message format | **No change** | Kafka Connect consumes the same EventResult JSON (LZ4). |

### 7.3 Additional Load from Iceberg Path

| Resource | Impact | Mitigation |
|----------|--------|------------|
| **Kafka broker -- consumer load** | +1 consumer group reading every message. Increases broker fetch throughput by ~100%. | Kafka brokers sized for 2x read is standard practice. Monitor `BytesOutPerSec`. |
| **Kafka broker -- retention** | No change -- retention policy stays the same, messages are consumed by both groups. | N/A |
| **Kafka partition count** | May need increase if Iceberg sink tasks are limited by partition count. | Set partitions = max(ConsumerForCH parallelism, Iceberg sink tasks). |
| **S3 write throughput** | New write path. Medium tenant = ~43 GB/day = ~500 KB/s sustained. Large = ~4.5 MB/s. | S3 has effectively unlimited write throughput. Cost is per-PUT. |
| **Network egress (Kafka to Connect)** | Same data volume as ConsumerForCH reads. Medium = ~258 GB/day raw JSON. | Ensure Kafka Connect workers are in same AZ as Kafka brokers. |
| **Iceberg REST Catalog -- metadata ops** | ~1 commit per flush interval (60s) per task. Medium = 12 tasks x 1440 commits/day = ~17K metadata ops/day. | Lightweight MySQL writes. Negligible load. |

### 7.4 Kafka Broker Sizing Adjustment

For existing deployments adding the Iceberg path:

| Current Kafka Sizing | Adjustment Needed |
|----------------------|-------------------|
| Brokers at < 50% fetch throughput | No change -- existing headroom covers the new consumer group |
| Brokers at 50-70% fetch throughput | Monitor closely; plan to add 1 broker |
| Brokers at > 70% fetch throughput | Add 1-2 brokers before enabling Iceberg sink |

### 7.5 Rollout Recommendation

1. **Deploy Kafka Connect + Iceberg sink** with `consumer.override.auto.offset.reset=latest` -- starts consuming from current offset, no backfill pressure.
2. **Monitor Kafka broker metrics** for 48 hours: `BytesOutPerSec`, `FetchRequestsPerSec`, consumer lag for both `ConsumerForCH` and `iceberg-connect` groups.
3. **If ConsumerForCH lag increases**: Add Kafka broker capacity or reduce Iceberg sink task count temporarily.
4. **Backfill historical data** separately via Spark/Flink job reading from S3 ClickHouse export, not by replaying Kafka.

---

## Appendix A: Quick Reference -- Minimum Viable Production Deployment

For a single Medium-tier tenant getting started:

| Component | Spec | Count |
|-----------|------|-------|
| StarRocks FE | m6i.xlarge (4 vCPU, 16 GB) | 1 |
| StarRocks CN (query) | r6i.2xlarge (8 vCPU, 64 GB, 200 GB gp3 cache) | 3 |
| StarRocks CN (refresh) | r6i.2xlarge (on-demand, scale-to-0) | 0 - 2 |
| Kafka Connect worker | m6i.xlarge (4 vCPU, 16 GB) | 2 |
| Iceberg REST Catalog | m6i.large (2 vCPU, 8 GB) | 1 |
| S3 bucket | Standard, lifecycle rules configured | 1 |
| MySQL (catalog metadata) | db.r6g.large or shared with existing FP MySQL | 1 |

**Estimated monthly cost**: ~$1,200 (1yr RI) for compute + ~$180 for S3 = **~$1,380/month**.

## Appendix B: Parquet Row Size Estimation

Typical FP EventResult with 100 features:

| Field | Type | Avg Bytes (Parquet) |
|-------|------|---------------------|
| event_id (string) | VARCHAR | 20 B |
| tenant (string) | VARCHAR | 10 B |
| event_type (string) | VARCHAR | 15 B |
| user_id (string) | VARCHAR | 20 B |
| event_time (timestamp) | BIGINT | 8 B |
| 100 feature columns (mixed types) | DOUBLE/INT/VARCHAR | ~400 B |
| Parquet overhead (row group, page headers) | -- | ~30 B |
| **Total per row** | | **~500 B** |

With Parquet dictionary encoding + Snappy compression, typical compression ratio is 5-7x vs raw JSON, resulting in approximately 15-20% of raw JSON size on disk.
