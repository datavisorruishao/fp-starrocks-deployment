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

```bash
pip install -r requirements.txt
```

AWS credentials must be available via environment variables or instance profile (boto3 credential chain).

The Iceberg table is pre-created by `POST /iceberg/connector/{tenant}` (FP API). If the table
does not yet exist, the script creates it automatically from the first batch's schema.

## Usage

### With ClickHouse connection (recommended)

Dynamically discovers array columns and primary key from CH `system.columns` / `system.tables`:

```bash
python import_ch_export.py \
    --tenant rippling \
    --s3-source s3://datavisor-rippling/DATASET/raw__/ \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --clickhouse-url http://clickhouse-rippling:8123 \
    --aws-region us-west-2
```

`--clickhouse-db` defaults to `--tenant` if omitted.

### Without ClickHouse connection

Array columns are stored as raw strings. `--primary-key` must be provided explicitly:

```bash
python import_ch_export.py \
    --tenant rippling \
    --s3-source s3://datavisor-rippling/DATASET/raw__/ \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --primary-key customer_id \
    --aws-region us-west-2
```

### Local file (testing)

```bash
python import_ch_export.py \
    --tenant rippling \
    --local-file ./raw__ch-exporter-event_result-*.csv.gz \
    --iceberg-catalog-url http://iceberg-rest-catalog:8181 \
    --clickhouse-url http://clickhouse-rippling:8123 \
    --dry-run
```

## Options

| Flag | Required | Description |
|---|---|---|
| `--tenant` | yes | Iceberg namespace (e.g. `rippling`) |
| `--s3-source` | yes* | S3 prefix containing export files |
| `--local-file` | yes* | Local CSV.gz file(s) — for testing only |
| `--iceberg-catalog-url` | yes | Iceberg REST catalog URL |
| `--clickhouse-url` | no | CH HTTP endpoint — enables dynamic schema discovery |
| `--clickhouse-user` | no | CH username (default: `default`) |
| `--clickhouse-password` | no | CH password |
| `--clickhouse-db` | no | CH database (defaults to `--tenant`) |
| `--primary-key` | no | CH column to rename to `userId` (auto-discovered if CH provided) |
| `--aws-region` | no | AWS region (default: `us-west-2`) |
| `--dry-run` | no | Parse and count rows, skip Iceberg writes |

\* `--s3-source` and `--local-file` are mutually exclusive; one is required.

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
  "raw__ch-exporter-event_result-1775257218377-42D2AFBF.csv.gz": "DONE",
  "raw__ch-exporter-event_result-1775862018377-8F3C1A2B.csv.gz": "DONE"
}
```

Re-runs skip files already marked `DONE`. To force re-import of a specific file, delete its
entry from the state file (or delete the whole file to restart from scratch).

## Verification

```bash
# Iceberg row count (via StarRocks)
SET CATALOG iceberg_catalog;
SELECT count(*) FROM rippling.event_result;

# ClickHouse ground truth
SELECT count() FROM rippling.event_result FINAL;
```

Counts should match within a small drift (live events that arrived during the import window).
