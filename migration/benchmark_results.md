# StarRocks Ad-hoc vs MV Benchmark Results

## Environment

- Iceberg table: `iceberg_catalog.rippling.event_result`
- StarRocks: 4.0.8, CN: 2 × 4 cores / 16GiB, data cache 4GB mem + 20GB disk

### Before compaction
- 2,689,038 rows, ~1,900 columns
- 619 data files, ~3.3MB / ~4,300 rows each (small file problem)
- Total data: ~2GB compressed Parquet on S3

### After compaction (re-import with BATCH_SIZE=500K)
- 2,292,473 rows, ~1,674 columns
- 49 data files, ~20.9MB avg each
- Total data: 1.02 GB compressed Parquet on S3

## Optimization Journey

### Phase 1: Version Upgrade (3.3.22 → 4.0.8)

StarRocks 4.0 改进了 Iceberg connector 的 scan 并行度和 metadata cache。

| 配置 | Q1 耗时 |
|---|---|
| 3.3 默认 (1×2C/8G, dop=auto=1, io=4) | 12.2s |
| 4.0 默认 (1×2C/8G, dop=auto=1, io=16) | 8.9s |

**效果**: 12.2s → 8.9s（+37%），4.0 默认的 `connector_io_tasks` 从 4 升到 16。

### Phase 2: Session 变量调优

手动测试不同 pipeline_dop 和 io_tasks 组合：

| 配置 | Q1 耗时 |
|---|---|
| 4.0 dop=8, io=64 | 21s (过度并行，2核争抢) |
| 4.0 dop=2, io=64 (冷) | 4.7s |
| 4.0 dop=2, io=64 (热) | 4.3s |

**发现**: dop 不能超过 CPU 核数，否则线程争抢反而更慢。dop=2 + io=64 是 2 核的最优组合。

### Phase 3: 三层优化（资源 + 配置 + 全局变量）

**Level 1 — 资源扩容 (values.yaml)**:
- CN replicas: 1 → 2（文件扫描分摊到两个节点）
- CN CPU: 2 → 4 cores（pipeline_dop auto 从 1 → 2）
- CN Memory: 8Gi → 16Gi
- cacheSizeLimit: 10Gi → 30Gi

**Level 2 — cn.conf 调优**:
- `datacache_disk_path` + `datacache_mem_size=4GB` + `datacache_disk_size=20GB`（S3 数据缓存到 CN 本地）
- `connector_io_tasks_per_scan_operator=32`（每个 scan operator 32 个并发 S3 请求）
- `--add-opens=java.base/java.util=ALL-UNNAMED`（修复 Java 17 模块系统 + Kryo 序列化兼容性 bug）

**Level 3 — 全局 session 变量 (FE)**:
- `SET GLOBAL connector_io_tasks_per_scan_operator = 32;`

**CN 自注册改进**:
- 从 Helm init job 注册 service name（只支持 1 个 CN）→ 每个 CN pod 用 initContainer 注册自己的 pod IP（支持多副本）

| 配置 | Q1 冷 | Q1 热 |
|---|---|---|
| 4.0 L1 only (1×4C/16G) | 17.5s | 2.1s |
| 4.0 L1+L2+L3 (2×4C/16G) | 7.5s | 1.4s |

### Phase 4: Compaction（重新导入，减少文件数）

修改 `import_ch_export.py`：
- `BATCH_SIZE`: 50K → 500K（减少 append 次数）
- 合并 CSV reader 的小 batch 后再 append（避免每个 32MB CSV block 生成一个文件）
- `--no-partition`: 建表不带 partition spec（避免每个 append 按分区拆成多个文件）
- `write.target-file-size-bytes`: 512MB → 1GB（避免单次 append 内部拆分）

**结果**: 619 files → 49 files（avg 20.9MB）

| 配置 | Q1 冷 | Q1 热 |
|---|---|---|
| 4.0 全部优化 (619 files) | 7.5s | 1.4s |
| **4.0 全部优化 (49 files)** | **744ms** | **247ms** |

### Phase 5: MV 预计算

| MV | Q1 耗时 |
|---|---|
| mv_action_daily | 257ms |

## Final Benchmark (Q1: 各 eventType 拦截率)

| 配置 | Q1 冷 | Q1 热 | vs baseline |
|---|---|---|---|
| 3.3 默认 (619 files, 1×2C/8G) | 12.2s | - | baseline |
| 4.0 默认 (619 files, 1×2C/8G) | 8.9s | - | 1.4x |
| 4.0 L1+L2+L3 (619 files, 2×4C/16G) | 7.5s | 1.4s | 8.7x 热 |
| **4.0 全部 + compaction (49 files)** | **744ms** | **247ms** | **冷 16x, 热 49x** |
| **MV (mv_action_daily)** | - | **257ms** | **47x** |

Ad-hoc 热缓存（247ms）≈ MV（257ms），compaction 后 ad-hoc 已经接近预计算的速度。

### Phase 6: pipeline_dop 对比测试 (dop=2 vs dop=4, 2026-04-15)

CN 已扩容到 4 cores，auto 默认 dop=2 (cores/2)。测试 dop=4 是否更优。

| 配置 | dop=2 | dop=4 | 结论 |
|---|---|---|---|
| Q1 冷 (cache disabled) | 1,175ms | **542ms** | dop=4 快 2.2x |
| Q1 热 (cache enabled) | **196ms** | 220ms | dop=2 略快 |

**分析**:
- 冷查询瓶颈在 S3 IO，更多 pipeline 线程 = 更多并行 scan，4 核扛得住，dop=4 大幅领先。
- 热查询数据已在本地缓存，瓶颈变成 CPU 计算和线程调度，4 个 pipeline 在 4 核上与 IO/brpc 等线程争抢，开销 > 收益。

**决策**: 保持 dop=auto (=2) 不变。热缓存是主要场景（data cache 常开），dop=2 更优。
冷查询 542ms vs 1.1s 均可接受，且仅在缓存未命中时发生。

## Bottleneck Analysis (EXPLAIN ANALYZE)

### Before compaction (619 files)
```
TotalTime: 16.6s (3.3 默认, 未优化)
├── ICEBERG_SCAN: 8.5s (99.46%)    ← 99% 时间在扫描
│   ├── OpenFile: 6.6s             ← 619 文件逐个打开 S3 连接
│   ├── ReaderInitFooterRead: 6.5s ← 1900 列的 Parquet footer 很大
│   ├── FSIOTime: 7.9s             ← S3 网络 I/O
│   └── CPU: 137ms                 ← CPU 几乎没用（I/O bound）
├── AGGREGATION: 34ms
└── PROJECT: 11ms
```

瓶颈: 619 个小文件 × S3 round trip。CPU/内存利用率极低。
Compaction 后 49 个文件，S3 round trip 减少 12.6 倍，冷缓存从 7.5s → 744ms。

## Pitfalls

### Java 17 + Kryo 兼容性
升级 CN 资源到 4C/16G 后触发 `NoClassDefFoundError: UnmodifiableCollectionsSerializer`。
根因: Java 17 模块系统禁止 Kryo 反射访问 `java.util.Collections` 内部字段。
修复: cn.conf JAVA_OPTS 加 `--add-opens=java.base/java.util=ALL-UNNAMED`。

### PyIceberg pyarrow_to_schema field-ids
`pyarrow_to_schema()` 要求 PyArrow schema 有 Iceberg field-ids，但 CSV 读入的 schema 没有。
修复: 用手动类型映射 `_pa_to_iceberg_type()` 构建 Iceberg schema，不依赖 `pyarrow_to_schema`。

### DayTransform + LongType 不兼容
Partition spec `day(processingTime)` 要求 timestamp 类型，但 processingTime 存为 long（毫秒）。
状态: 建表暂不加 partition spec，未来需将 processingTime 转为 TimestampType 或改用兼容的 transform。
