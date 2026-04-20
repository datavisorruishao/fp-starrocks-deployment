# ClickHouse → Iceberg Migration Guide

A comprehensive guide for migrating `event_result` data from ClickHouse to Iceberg, based on production exploration and benchmarking.

## Problem Statement

We need to migrate historical `event_result` data from ClickHouse into Iceberg so it's queryable via StarRocks. The challenge:

- **2400+ columns** per table (15 fixed columns + ~2400 feature columns)
- **15 million rows** for a 30-day window
- **~342K rows per day**, with peaks of 80K rows in a single hour
- **Production ClickHouse** — migration must not impact live traffic
- **100% data accuracy** required — no missing or duplicate rows
- **ReplacingMergeTree** engine — duplicates exist until background merge runs

## Exploration Journey

### 1. Table structure (production evidence)

```sql
clickhouse :) SHOW CREATE TABLE event_result;

ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(toDateTime(time / 1000))
PRIMARY KEY SSN
ORDER BY (SSN, eventId, time, eventType)
TTL toDateTime(timeInserted / 1000, 'Europe/London') + toIntervalWeek(80)
SETTINGS index_granularity = 8192

-- 2439 columns total
```

### 2. Why not just SELECT *?

```sql
clickhouse :) SELECT * FROM event_result LIMIT 1
    SETTINGS max_threads = 2, priority = 10;

1 row in set. Elapsed: 2.330 sec.
```

**2.33 seconds for a single row.** ClickHouse stores each column as a separate file — `SELECT *` on 2400 columns means 2400 file opens.

### 3. The partition pruning discovery

Initial queries filtered by `timeInserted` (insertion time):

```sql
clickhouse :) SELECT eventId, time, eventType, timeInserted
    FROM event_result
    WHERE toDate(toDateTime(intDiv(timeInserted, 1000))) = yesterday()
    LIMIT 1
    SETTINGS max_threads = 2, priority = 10;

-- 1 row in set. Elapsed: 3.563 sec. Processed 281.58 million rows, 2.25 GB
```

**3.5 seconds, scanned ALL 281 million rows** — full table scan because `timeInserted` doesn't match the partition key.

But the table is partitioned by `time` (event time). Switching the filter:

```sql
clickhouse :) SELECT eventId, time, eventType, timeInserted
    FROM event_result
    WHERE toYYYYMM(toDateTime(time / 1000)) = toYYYYMM(yesterday())
      AND toDate(toDateTime(time / 1000)) = yesterday()
    LIMIT 1
    SETTINGS max_threads = 2, priority = 10, max_execution_time = 30;

-- 1 row in set. Elapsed: 0.017 sec. Processed 18.61 thousand rows, 1.66 MB
```

**200x faster** — from 3.5s to 0.017s, from 281M rows to 18K rows.

### 4. Time gap between `time` and `timeInserted`

We need data by `timeInserted` (last 30 days inserted), but partition pruning needs `time`. Measured the gap:

```sql
clickhouse :) SELECT
    avg(timeInserted - time) / 1000 AS avg_gap_seconds,
    max(timeInserted - time) / 1000 AS max_gap_seconds,
    min(timeInserted - time) / 1000 AS min_gap_seconds
FROM event_result
WHERE toYYYYMM(toDateTime(time / 1000)) = toYYYYMM(yesterday())
  AND toDate(toDateTime(time / 1000)) = yesterday()
SETTINGS max_threads = 2, priority = 10, max_execution_time = 30;

-- ┌────avg_gap_seconds─┬─max_gap_seconds─┬─min_gap_seconds─┐
-- │ 45862.869556093625 │        129576.8 │      -60033.086 │
-- └────────────────────┴─────────────────┴─────────────────┘
-- Elapsed: 0.046 sec.
```

```
avg gap:  45,862s = ~12.7 hours
max gap:  129,576s = ~36 hours (1.5 days!)
min gap:  -60,033s = negative (events with future timestamps)
```

Solution: use `time` for partition pruning with extra month buffer, `timeInserted` for the actual filter.

### 5. Verifying partition pruning captures all rows (March 2026)

```sql
-- WITH partition pruning (fast)
clickhouse :) SELECT count() AS with_pruning
FROM event_result
WHERE toYYYYMM(toDateTime(time / 1000)) IN (202602, 202603, 202604)
  AND timeInserted >= toUInt64(toUnixTimestamp(toDateTime('2026-03-01 00:00:00'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('2026-04-01 00:00:00'))) * 1000
SETTINGS max_threads = 2, priority = 10, max_execution_time = 60;

-- ┌─with_pruning─┐
-- │     14996165 │ -- 15.00 million
-- └──────────────┘
-- Elapsed: 0.438 sec. Processed 36.90 million rows

-- WITHOUT partition pruning (ground truth, full scan)
clickhouse :) SELECT count() AS ground_truth
FROM event_result
WHERE timeInserted >= toUInt64(toUnixTimestamp(toDateTime('2026-03-01 00:00:00'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('2026-04-01 00:00:00'))) * 1000
SETTINGS max_threads = 2, priority = 10, max_execution_time = 120;

-- ┌─ground_truth─┐
-- │     14996165 │ -- 15.00 million
-- └──────────────┘
-- Elapsed: 1.294 sec. Processed 282.17 million rows
```

**14,996,165 = 14,996,165. Exact match. 100% accurate.**
Partition pruning: 0.44s (36.9M rows) vs full scan: 1.29s (282M rows).

### 6. Row distribution per hour

```sql
clickhouse :) SELECT
    toHour(toDateTime(intDiv(timeInserted, 1000))) AS hour,
    count() AS rows
FROM event_result
WHERE toYYYYMM(toDateTime(time / 1000)) = toYYYYMM(yesterday())
  AND toDate(toDateTime(time / 1000)) = yesterday()
GROUP BY hour ORDER BY hour
SETTINGS max_threads = 2, priority = 10, max_execution_time = 30;

--  ┌─hour─┬──rows─┐
--  │    0 │ 14975 │
--  │    1 │  9924 │
--  │    2 │ 12687 │
--  │    3 │ 14224 │
--  │    4 │  5038 │
--  │    7 │ 16207 │
--  │    8 │  2515 │
--  │    9 │  2252 │
--  │   11 │ 79696 │  ← peak hour
--  │   12 │  5060 │
--  │   13 │  3869 │
--  │   14 │  4694 │
--  │   15 │ 12611 │
--  │   16 │ 21016 │
--  │   17 │ 32986 │
--  │   18 │ 20470 │
--  │   19 │ 21469 │
--  │   20 │ 11749 │
--  │   21 │ 15612 │
--  │   22 │ 13242 │
--  │   23 │  9199 │
--  └──────┴───────┘
-- Elapsed: 1.732 sec.
```

**~342K rows/day, peak hour 80K rows.** Daily total: ~330K rows.

### 7. Duplicate rate (ReplacingMergeTree dedup check)

```sql
clickhouse :) SELECT
    count() AS total_rows,
    uniq(SSN, eventId, time, eventType) AS unique_keys,
    count() - uniq(SSN, eventId, time, eventType) AS duplicates
FROM event_result
WHERE toYYYYMM(toDateTime(time / 1000)) IN (202603)
  AND timeInserted >= toUInt64(toUnixTimestamp(toDateTime('2026-03-15 00:00:00'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('2026-03-16 00:00:00'))) * 1000
SETTINGS max_threads = 2, priority = 10, max_execution_time = 60;

-- ┌─total_rows─┬─unique_keys─┬─duplicates─┐
-- │     342274 │      341606 │        668 │
-- └────────────┴─────────────┴────────────┘
-- Elapsed: 0.654 sec.
```

**668 duplicates out of 342,274 rows (0.2%).** FINAL is required for 100% accuracy.

### 8. FINAL cost benchmarks (production)

```sql
-- 4 key columns WITH FINAL
clickhouse :) SELECT SSN, eventId, time, eventType
FROM event_result FINAL
WHERE toYYYYMM(toDateTime(time / 1000)) IN (202602, 202603, 202604)
  AND timeInserted >= toUInt64(toUnixTimestamp(toDateTime('2026-03-15 00:00:00'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('2026-03-16 00:00:00'))) * 1000
FORMAT Null
SETTINGS max_threads = 2, priority = 10, max_execution_time = 30;

-- Elapsed: 3.058 sec. Processed 36.92 million rows, 3.89 GB
-- Peak memory usage: 86.27 MiB.

-- 10 columns WITH FINAL
-- Elapsed: 4.977 sec. Processed 36.91 million rows, 5.51 GB
-- Peak memory usage: 129.81 MiB.

-- 100 columns WITH FINAL
clickhouse :) SELECT eventId, time, rules, actions, eventType, timeInserted,
    CQ_IDSCORERESULTCODE2, CUSTOM_SCORE_2, ... (100 columns total)
FROM event_result FINAL
WHERE toYYYYMM(toDateTime(time / 1000)) IN (202602, 202603, 202604)
  AND timeInserted >= toUInt64(toUnixTimestamp(toDateTime('2026-03-15 00:00:00'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('2026-03-16 00:00:00'))) * 1000
FORMAT Null
SETTINGS max_threads = 2, priority = 10, max_execution_time = 60;

-- Elapsed: 39.067 sec. Processed 36.91 million rows, 17.81 GB
-- Peak memory usage: 301.47 MiB.

-- 100 columns WITHOUT FINAL (for comparison)
-- Elapsed: 4.123 sec. Processed 36.91 million rows, 3.74 GB
-- Peak memory usage: 24.49 MiB.
```

| Columns | With FINAL | Memory | Without FINAL | Status |
|---|---|---|---|---|
| 4 (key only) | **3.1s** | 86 MB | 0.4s | ✓ |
| 10 | **5.0s** | 130 MB | 1.2s | ✓ |
| 100 | **39s** | 301 MB | 4.1s | ✓ |
| 200 | **109s** | 676 MB | — | ✓ recommended batch size |
| 500 | **>300s (timeout)** | 2 GB | — | ✗ timed out at 48% |

```sql
-- 200-column FINAL evidence:
-- Elapsed: 108.613 sec. Processed 36.96 million rows, 35.43 GB
-- (340.30 thousand rows/s., 326.18 MB/s.)
-- Peak memory usage: 675.81 MiB.

-- 500-column FINAL evidence (timed out):
-- Elapsed: 299.989 sec. Processed 17.96 million rows, 45.44 GB
-- (59.86 thousand rows/s., 151.49 MB/s.) (2.1 CPU, 2.13 GB RAM) 48%
-- Code: 159. DB::Exception: Timeout exceeded: elapsed 300.005426888 seconds, maximum: 300.
```

`FINAL` reads ALL rows in the partition (36.9M) to deduplicate. Cost scales with column count because all selected columns must be read into memory for dedup. **200 columns per batch is the sweet spot** — fewer batches than 100, manageable memory, completes within 2 minutes.

### 9. ClickHouse system resources (production)

```sql
clickhouse :) SELECT
    count() AS active_queries,
    formatReadableSize(sum(memory_usage)) AS total_memory_used,
    sum(length(thread_ids)) AS total_threads_used,
    max(elapsed) AS longest_query_sec
FROM system.processes WHERE is_initial_query = 1;

-- ┌─active_queries─┬─total_memory_used─┬─total_threads_used─┬─longest_query_sec─┐
-- │              2 │ 22.68 MiB         │                  4 │          0.049838 │
-- └────────────────┴───────────────────┴────────────────────┴───────────────────┘

clickhouse :) SELECT metric, value FROM system.asynchronous_metrics
WHERE metric IN ('OSMemoryTotal', 'OSMemoryAvailable', 'LoadAverage1', 'LoadAverage5');

-- ┌─metric────────────┬────────value─┐
-- │ LoadAverage1      │        14.22 │
-- │ LoadAverage5      │        14.89 │
-- │ OSMemoryAvailable │ 187313037312 │  -- 187 GB free
-- │ OSMemoryTotal     │ 264579244032 │  -- 264 GB total
-- └───────────────────┴──────────────┘

clickhouse :) SELECT value FROM system.settings WHERE name = 'max_threads';
-- 'auto(30)' → 30 CPU cores
```

**System status:**
- 30 CPU cores, load average 14.9 (~50% utilized)
- 264 GB RAM, 187 GB free (29% used)
- Migration with `max_threads=2` uses 7% of CPU at lowest priority

### 10. Settings verification

```sql
clickhouse :) SELECT 1 SETTINGS max_threads = 2, priority = 10;
-- 1 row in set. Elapsed: 0.002 sec.
```

Confirmed: `max_threads` and `priority` settings are supported.

### 11. Why Python dedup won't work

Without `FINAL`, ClickHouse returns all copies of duplicate rows in arbitrary order. Python `drop_duplicates` keeps an arbitrary row — but `ReplacingMergeTree` keeps the **last inserted** row. If duplicate rows have different values in non-key columns (e.g. a feature was updated), Python picks the wrong one.

Only ClickHouse knows the insertion order.

### 12. OPTIMIZE TABLE — too dangerous

```sql
OPTIMIZE TABLE event_result PARTITION 202603 FINAL
```

This forces dedup on disk, allowing fast export without `FINAL`. But it rewrites the entire partition — heavy I/O, locks the partition, competes with production. **Not safe for a production database serving live traffic.**

## Chosen Approach

### FINAL + column batching

Split 2400 columns into batches of 100-500. Each batch query uses `FINAL` for correctness:

```sql
-- Batch 1: key cols + cols 1-496
SELECT SSN, eventId, time, eventType, col1, ..., col496
FROM event_result FINAL
WHERE toYYYYMM(toDateTime(time / 1000)) IN ({partition_months})
  AND timeInserted >= toUInt64(toUnixTimestamp(toDateTime('{start}'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('{end}'))) * 1000
FORMAT Parquet
SETTINGS max_threads = 2, priority = 10, max_execution_time = 300

-- Batch 2: key cols + cols 497-992
-- ... same WHERE, same FINAL
```

Merge batches in Python by appending columns (same ORDER BY + FINAL = same row order across batches).

### Three-step migration

**Step 1 — Register table via FP API (instant)**
```bash
curl -X POST http://<fp-api>/iceberg/connector/{tenant}
```
Creates Iceberg table with 5 base columns + partition spec + sort order + bloom filters.

**Step 2 — Sync full schema from CH → Iceberg (instant, metadata only)**

Query `system.columns` for all 2400 column names/types, map to Iceberg types, `update_schema().union_by_name()`. No data moved. All 2400 columns become queryable in StarRocks (return NULL until data is written).

**Step 3 — Export per day with FINAL + column batching**

For each day in the migration window:
1. Split 2400 columns into batches (+ 4 key columns per batch)
2. Export each batch as Parquet with `FINAL` and partition pruning
3. Merge batches in Python on the key columns
4. Convert timestamps, rename columns
5. `iceberg_table.append()`
6. Sleep between days to reduce CH pressure

### ClickHouse resource usage per migration query

```
CPU:    2 of 30 cores (7%)
Memory: ~676 MB peak (with FINAL on 200 cols, production measured)
Priority: 10 (lowest, yields to production)
Duration: ~109s per batch
Sustained average: ~1% CPU (active during query, sleeping between)
```

## Cost Estimation

### Pre-migration checks (all safe, read-only)

| # | Check | Query | Result |
|---|---|---|---|
| 1 | Column count | `SELECT count() FROM system.columns WHERE table='event_result'` | 2439 |
| 2 | Total rows | `SELECT count() FROM event_result` | 282M |
| 3 | March rows | `SELECT count() ... WHERE timeInserted IN March` | 14,996,165 |
| 4 | Rows per day | `GROUP BY hour WHERE yesterday()` | ~342K/day, peak 80K/hour |
| 5 | Duplicate rate | `count() vs uniq(SSN,eventId,time,eventType)` | 0.2% (668/342K) |
| 6 | Partition pruning accuracy | Pruned count vs full-scan count | 14,996,165 = 14,996,165 ✓ |
| 7 | Time gap | `avg/max/min(timeInserted - time)` | avg 12.7h, max 36h, min -60Ks |
| 8 | CH load | `system.processes` | 50% CPU, 29% RAM |
| 9 | CH resources | `system.asynchronous_metrics` | 30 cores, 264 GB RAM |
| 10 | FINAL cost (200 cols) | `SELECT 200 cols FINAL FORMAT Null` | 109s, 676 MB |

### Migration timeline (batch size = 200 columns)

```
Columns: 2400
Batches per day: 12 (200 cols per batch)
Days to migrate: 31 (March 2026)

Per batch:    ~109s (with FINAL on 200 cols, production measured)
Per day:      12 batches × 109s = 22 min + merge/write + sleep = ~25 min
31 days:      31 × 25 min = ~13 hours

Schedule: run overnight Friday → done by Saturday morning.
```

| Batch size | Batches/day | Time/day | Total 31 days |
|---|---|---|---|
| 100 cols | 24 | ~28 min | ~14 hours |
| **200 cols** | **12** | **~25 min** | **~13 hours** ← recommended |
| 500 cols | 5 | timeout | ✗ |

**Do NOT use 500 columns per batch** — timed out at 300s in production (only 48% complete, 2 GB RAM).

### Safety settings

```sql
SETTINGS
  max_threads = 2,             -- 7% of 30 cores
  priority = 10,               -- lowest priority, yields to production
  max_execution_time = 300     -- auto-kill if stuck (5 min safety net)
```

## Post-migration verification

```sql
-- StarRocks count
SET new_planner_optimize_timeout = 60000;
SELECT COUNT(*) FROM iceberg_catalog.{tenant}.event_result;

-- ClickHouse ground truth (with FINAL for accurate deduped count)
SELECT count()
FROM event_result FINAL
WHERE toYYYYMM(toDateTime(time / 1000)) IN (202602, 202603, 202604)
  AND timeInserted >= toUInt64(toUnixTimestamp(toDateTime('2026-03-01 00:00:00'))) * 1000
  AND timeInserted <  toUInt64(toUnixTimestamp(toDateTime('2026-04-01 00:00:00'))) * 1000
SETTINGS max_threads = 2, priority = 10, max_execution_time = 300;

-- Counts must match exactly.
```

## Key Findings Summary

| # | Finding | Evidence | Impact |
|---|---|---|---|
| 1 | `SELECT *` on 2400 cols = 2.33s for 1 row | Production `LIMIT 1` test | Must batch columns |
| 2 | `timeInserted` filter = full table scan (281M rows) | `Processed 281.58 million rows` | Must use `time` for partition pruning |
| 3 | Partition pruning = 200x faster | 0.017s vs 3.5s | Always include `toYYYYMM(toDateTime(time / 1000))` |
| 4 | Pruned count = full-scan count | 14,996,165 = 14,996,165 | Partition buffer is sufficient |
| 5 | Time gap up to 36 hours, negative possible | avg 12.7h, max 36h, min -60Ks | Need 3-month partition buffer |
| 6 | `FINAL` on 100 cols = 39s, 301 MB | Production benchmark | FINAL cost scales with column count |
| 7 | Without FINAL = 4.1s, 24 MB (same 100 cols) | Production benchmark | 10x faster without FINAL |
| 8 | `FINAL` is required for 100% accuracy | 668 duplicates in 342K rows | Can't use Python dedup |
| 9 | `OPTIMIZE TABLE FINAL` too dangerous | Rewrites entire partition | Must use query-level FINAL |
| 10 | CH has 30 cores, 264 GB RAM, 50% utilized | `system.asynchronous_metrics` | 2 threads + priority 10 is safe |
| 11 | ClickHouse timezone != UTC | `min_readable: 2026-03-01 00:00:18` | Use `toUnixTimestamp(toDateTime('...'))` |

## Data flow

```
ClickHouse (production, throttled reads)
    │
    │ SELECT {200 cols} FROM event_result FINAL
    │ FORMAT Parquet
    │ SETTINGS max_threads=2, priority=10
    │
    ▼
Python script (bastion host or K8s Job)
    │
    │ 12 column batches → merge on (SSN, eventId, time, eventType)
    │ Convert timestamps (ms → µs for timestamptz)
    │ Rename columns (time→eventTime, SSN→userId, etc.)
    │
    ▼
PyIceberg table.append() → S3 (Parquet files)
    │
    ▼
StarRocks queries via Iceberg REST catalog
```

## Script usage

```bash
python migration/migrate_column_batch.py \
  --tenant {tenant_name} \
  --clickhouse-url http://{ch-host}:8123 \
  --clickhouse-db {db_name} \
  --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
  --start-date 2026-03-01 \
  --end-date 2026-04-01 \
  --primary-key SSN \
  --column-batch-size 200 \
  --ch-max-threads 2 \
  --ch-priority 10 \
  --sleep-between-days 30
```

Resumable: re-run the same command after interruption — state file tracks completed days.
Dry-run: add `--dry-run` to preview without writing.
