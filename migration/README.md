# ClickHouse Export → Iceberg Import

Imports historical `event_result` data from the ClickHouse exporter job's S3 files into Iceberg.

The CH exporter job runs weekly and writes snapshots to S3 as:
```
s3://{bucket}/{prefix}/raw__ch-exporter-event_result-{timeInserted_ms}-{hash}.csv.gz
```

This script reads those files and appends them into the tenant's Iceberg table, applying the
same column renames and transformations as the live Kafka Connect + SMT pipeline.

```
S3: raw__ch-exporter-event_result-*.csv.gz
    │  import_ch_export.py
    │  discovers array columns from ClickHouse system.columns
    │  applies column renames + dv_isDetection inversion
    │  PyIceberg: table.append() per batch
    ▼
Iceberg table: {tenant}.event_result (via REST catalog)
```

## Prerequisites

AWS credentials must be available via environment variables, instance profile, or IRSA (boto3 credential chain).

The Iceberg table is pre-created by `POST /iceberg/connector/{tenant}` (FP API). If the table
does not yet exist, the script creates it automatically from the first batch's schema.

## Quick start (Kubernetes)

```bash
# 1. Build image
bash migration/build.sh

# 2. Push to ECR (or your registry)
REGISTRY=123456789.dkr.ecr.us-west-2.amazonaws.com bash migration/build.sh --push

# 3. Edit job.yaml — fill in CONFIGURE sections (tenant, S3 files, CH url, image)
#    Then apply:
kubectl apply -f migration/job.yaml -n duckdb

# 4. Watch logs
kubectl logs -f job/ch-iceberg-migration-rippling -n duckdb

# 5. Cleanup
kubectl delete job ch-iceberg-migration-rippling -n duckdb
```

Or use `envsubst` for scripted deploys:
```bash
export TENANT=rippling
export S3_FILES="s3://bucket/path/file1.csv.gz s3://bucket/path/file2.csv.gz"
envsubst < migration/job.yaml | kubectl apply -n duckdb -f -
```

## Usage (local / bare Python)

```bash
pip install -r requirements.txt
```

### Recommended: explicit S3 files

Pick the specific weekly files you want to import (e.g. last month) from `aws s3 ls`:

```bash
python import_ch_export.py \
    --tenant rippling \
    --s3-file \
        s3://datavisor-rippling/DATASET/raw__/raw__ch-exporter-event_result-1772838000460-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz \
        s3://datavisor-rippling/DATASET/raw__/raw__ch-exporter-event_result-1773442800327-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz \
        s3://datavisor-rippling/DATASET/raw__/raw__ch-exporter-event_result-1774047602243-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz \
        s3://datavisor-rippling/DATASET/raw__/raw__ch-exporter-event_result-1774652415054-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --clickhouse-url http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123 \
    --aws-region us-west-2
```

`--clickhouse-db` defaults to `--tenant` if omitted.

### Auto-discover all files under a prefix

Scans the entire prefix and imports every matching file. Use when you want to import all
available history:

```bash
python import_ch_export.py \
    --tenant rippling \
    --s3-source s3://datavisor-rippling/DATASET/raw__/ \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --clickhouse-url http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123 \
    --aws-region us-west-2
```

### Local file (testing only)

```bash
python import_ch_export.py \
    --tenant rippling \
    --local-file ./raw__ch-exporter-event_result-*.csv.gz \
    --iceberg-catalog-url http://localhost:8181 \
    --clickhouse-url http://localhost:8123 \
    --dry-run
```

## Options

| Flag | Required | Description |
|---|---|---|
| `--tenant` | yes | Iceberg namespace (e.g. `rippling`) |
| `--s3-file` | yes* | **Recommended.** Explicit S3 file path(s) — one or more full `s3://` URIs |
| `--s3-source` | yes* | S3 prefix — auto-discovers all matching files under the prefix |
| `--local-file` | yes* | Local CSV.gz file(s) — for testing only |
| `--iceberg-catalog-url` | yes | Iceberg REST catalog URL (see note below) |
| `--clickhouse-url` | no | CH HTTP interface URL, port 8123 (see note below) |
| `--clickhouse-user` | no | CH username (default: `default`) |
| `--clickhouse-password` | no | CH password |
| `--clickhouse-db` | no | CH database (defaults to `--tenant`) |
| `--primary-key` | no | CH column to rename to `userId` (auto-discovered if CH provided) |
| `--aws-region` | no | AWS region (default: `us-west-2`) |
| `--dry-run` | no | Parse and count rows, skip Iceberg writes |

\* `--s3-file`, `--s3-source`, and `--local-file` are mutually exclusive; one is required.

### URL notes

**`--iceberg-catalog-url`**: the K8s service deployed by `charts-iceberg`.
- Inside the cluster: `http://iceberg-rest-catalog:8181`
- From outside, use port-forward: `kubectl port-forward svc/iceberg-rest-catalog 8181:8181`
  then use `http://localhost:8181`

**`--clickhouse-url`**: the CH HTTP interface (port **8123**, not the native port 9000).
Find the correct host in FP's `application.yaml` under `clickhouseUrl`.
Example: `http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123`

## Column mapping

Applied to every file regardless of tenant, matching the live Kafka Connect + SMT output:

| ClickHouse | Iceberg | Notes |
|---|---|---|
| `eventId` | `eventId` | |
| `eventType` | `eventType` | |
| `time` | `eventTime` | |
| `timeInserted` | `processingTime` | |
| `{primary_key}` | `userId` | dynamic per tenant |
| `rules` | `rules` | parsed from Python list repr |
| `actions` | `actions` | parsed from Python list repr |
| `trialRules` | `trialRules` | parsed from Python list repr (if present) |
| `trialActions` | `trialActions` | parsed from Python list repr (if present) |
| `dv_reevaluate_entity` | `reEvaluateEntity` | |
| `origin_id` | `originId` | |
| `origin_category` | `originCategory` | |
| `dv_isDetection` | `fromUpdateAPI` | boolean NOT applied |
| `dv_cluster_ids` | `dv_cluster_ids` | CH-only historical data |
| `rule_property` | `rule_property` | CH-only historical data |
| `trial_rule_property` | `trial_rule_property` | CH-only historical data |
| `<feature columns>` | same name | auto-discovered from CH |

Array columns (e.g. `rules`, `actions`, feature list columns) are stored in the CSV as Python
list repr strings (`"[1237, 5432]"`) and parsed to real Iceberg list types using
`ast.literal_eval`. Element types are discovered dynamically from ClickHouse `system.columns`.

## Schema evolution

If the Iceberg table was pre-created by connector registration (5 base columns), the script
automatically evolves the schema to include all CH columns before appending. Extra CH columns
not produced by live traffic will have `NULL` for new rows — this is harmless.

## Resumability

The script writes `import_ch_export_state_{tenant}.json` after each file:

```json
{
  "raw__ch-exporter-event_result-1764370800204-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz": "DONE",
  "raw__ch-exporter-event_result-1764975600842-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz": "DONE",
  "raw__ch-exporter-event_result-1765580418847-42D2AFBF804A14FC266D9DC9026BC6D5.csv.gz": "FAILED"
}
```

Re-runs skip files already marked `DONE`. `FAILED` files are retried automatically.
To force re-import of a specific file, delete its entry (or delete the whole file to restart).

## Verification

```bash
# Iceberg row count (via StarRocks)
SET CATALOG iceberg_catalog;
SELECT count(*) FROM rippling.event_result;

# ClickHouse ground truth
SELECT count() FROM rippling.event_result FINAL;
```

Counts should match within a small drift (live events that arrived during the import window).

---

# Column-Batched Migration (for wide tables)

For tenants with 2000+ columns where `import_ch_export.py` is too slow (SELECT * on 2400 columns
takes 2.33s per row even with LIMIT 1), use `migrate_column_batch.py` instead.

This script exports columns in batches of 200, each with `FINAL` for dedup accuracy on
`ReplacingMergeTree`, then merges batches in Python and appends to Iceberg.

See [docs/migration-guide.md](../docs/migration-guide.md) for the full exploration, production
benchmarks, and cost estimation.

## Usage

```bash
# Step 0: Register the table via FP API first
curl -X POST http://<fp-api>/iceberg/connector/{tenant}

# Step 1: Run migration (dry-run first)
python migrate_column_batch.py \
    --tenant rippling \
    --clickhouse-url http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123 \
    --clickhouse-db rippling \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --start-date 2026-03-01 \
    --end-date 2026-04-01 \
    --primary-key SSN \
    --column-batch-size 200 \
    --aws-region us-west-2 \
    --dry-run

# Step 2: Run for real (remove --dry-run)
python migrate_column_batch.py \
    --tenant rippling \
    --clickhouse-url http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123 \
    --clickhouse-db rippling \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --start-date 2026-03-01 \
    --end-date 2026-04-01 \
    --primary-key SSN \
    --column-batch-size 200 \
    --aws-region us-west-2 \
    --sleep-between-days 30
```

## Production benchmarks (column batch size)

| Batch size | Time per batch | Memory | Recommendation |
|---|---|---|---|
| 100 cols | 39s | 301 MB | Conservative |
| **200 cols** | **109s** | **676 MB** | **Recommended** |
| 500 cols | >300s (timeout) | 2 GB | Too large |

## Estimated timeline (200 cols/batch, 2400 total columns, 31 days)

```
Per batch:  ~109s
Per day:    12 batches × 109s = 22 min + sleep = ~25 min
31 days:    ~13 hours — run overnight
```

## Which script to use?

| Scenario | Script |
|---|---|
| Tenant has CH exporter CSV.gz files on S3 | `import_ch_export.py` |
| Tenant has 2000+ columns, no CH exporter | `migrate_column_batch.py` |
| Both available | `import_ch_export.py` (zero CH load) |
