#!/usr/bin/env python3
"""
Column-batched migration: ClickHouse → Iceberg with FINAL dedup.

Designed for wide tables (2000+ columns) where SELECT * is too slow.
Exports columns in batches of 200, each with FINAL for dedup accuracy,
then merges batches in Python and appends to Iceberg.

Prerequisites:
  1. Iceberg table must be registered first via FP API:
     curl -X POST http://<fp-api>/iceberg/connector/{tenant}
  2. ClickHouse HTTP API must be reachable (port 8123)
  3. Iceberg REST catalog must be reachable (port 8181)
  4. AWS credentials available (IAM role or env vars) for S3 access

Usage (full speed — dedicated CH instance):
    python migrate_column_batch.py \\
        --tenant rippling \\
        --clickhouse-url http://clickhouse-migration:8123 \\
        --iceberg-catalog-url http://iceberg-rest-catalog:8181 \\
        --start-date 2026-03-01 \\
        --end-date 2026-04-01 \\
        --column-batch-size 200

Usage (throttled — shared production CH):
    python migrate_column_batch.py \\
        --tenant rippling \\
        --clickhouse-url http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123 \\
        --iceberg-catalog-url http://iceberg-rest-catalog:8181 \\
        --start-date 2026-03-01 \\
        --end-date 2026-04-01 \\
        --column-batch-size 200 \\
        --ch-max-threads 2 \\
        --ch-priority 10 \\
        --sleep-between-days 30

State file: migration_state_{tenant}.json
  Tracks completed days. Re-runs skip already-done days.
  Delete an entry to force re-import of a specific day.

See docs/migration-guide.md for the full exploration and benchmarks.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import NoSuchTableError
import pyiceberg.types as T
from pyiceberg.schema import Schema

# ---------------------------------------------------------------------------
# Column renames: CH name → Iceberg name
# Matches the live Kafka Connect + FeatureResolverTransform SMT pipeline.
# ---------------------------------------------------------------------------
BASE_RENAMES = {
    "time":                 "eventTime",
    "timeInserted":         "processingTime",
    "dv_reevaluate_entity": "reEvaluateEntity",
    "origin_id":            "originId",
    "origin_category":      "originCategory",
}


# ---------------------------------------------------------------------------
# ClickHouse helpers
# ---------------------------------------------------------------------------

def ch_query_parquet(ch_url, ch_user, ch_password, sql):
    """Execute CH query, return Parquet bytes via temp file."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.parquet')
    tmp.close()
    cmd = ['curl', '-s', '-o', tmp.name, '-X', 'POST', ch_url, '--data-binary', '@-']
    if ch_user:
        cmd += ['-u', f'{ch_user}:{ch_password}']
    subprocess.run(cmd, input=sql.encode(), capture_output=True, timeout=600)
    with open(tmp.name, 'rb') as f:
        data = f.read()
    os.unlink(tmp.name)
    return data


def ch_query_text(ch_url, ch_user, ch_password, sql):
    """Execute CH query, return text result."""
    cmd = ['curl', '-s', '-X', 'POST', ch_url, '--data-binary', '@-']
    if ch_user:
        cmd += ['-u', f'{ch_user}:{ch_password}']
    r = subprocess.run(cmd, input=sql.encode(), capture_output=True, timeout=300)
    return r.stdout.decode().strip()


def ch_type_to_iceberg(ch_type):
    """Map ClickHouse type string to Iceberg type."""
    t = ch_type.strip()
    while True:
        if t.startswith("Nullable(") and t.endswith(")"): t = t[9:-1]
        elif t.startswith("LowCardinality(") and t.endswith(")"): t = t[15:-1]
        else: break
    if t.startswith("Array("):
        inner = t[6:-1]
        return T.ListType(element_id=0, element_type=ch_type_to_iceberg(inner), element_required=False)
    if t in ("String",) or t.startswith("FixedString("): return T.StringType()
    if t in ("Int8", "Int16", "Int32", "UInt8", "UInt16"): return T.IntegerType()
    if t in ("Int64", "UInt32", "UInt64"): return T.LongType()
    if t == "Float32": return T.FloatType()
    if t == "Float64": return T.DoubleType()
    return T.StringType()


def quote_col(name):
    """Backtick-quote column names with special characters."""
    if '(' in name or ')' in name or ' ' in name or '-' in name:
        return f'`{name}`'
    return name


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def state_path(tenant):
    return f"migration_state_{tenant}.json"


def load_state(tenant):
    path = state_path(tenant)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(tenant, state):
    with open(state_path(tenant), "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------

def convert_timestamps(table_pa):
    """Convert time/timeInserted from int64 ms/s → timestamptz (µs).
    Auto-detects: values > 1_000_000_000_000 are milliseconds, otherwise seconds.
    """
    for ts_col in ("time", "timeInserted"):
        if ts_col in table_pa.column_names:
            idx = table_pa.column_names.index(ts_col)
            arr = table_pa.column(idx).cast(pa.int64())
            ms_mask = pc.greater(arr, pa.scalar(1_000_000_000_000, pa.int64()))
            us = pc.if_else(
                ms_mask,
                pc.multiply(arr, pa.scalar(1_000, pa.int64())),
                pc.multiply(arr, pa.scalar(1_000_000, pa.int64())),
            )
            table_pa = table_pa.set_column(
                idx,
                pa.field(ts_col, pa.timestamp("us", tz="UTC")),
                us.cast(pa.timestamp("us", tz="UTC")),
            )
    return table_pa


def rename_columns(table_pa, renames):
    """Rename CH columns to Iceberg names."""
    new_names = [renames.get(n, n) for n in table_pa.column_names]
    return table_pa.rename_columns(new_names)


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

def run(args):
    ch_url = args.clickhouse_url
    ch_user = args.clickhouse_user
    ch_password = args.clickhouse_password
    ch_db = args.clickhouse_db or args.tenant
    ch_table = args.clickhouse_table

    # Build SETTINGS clause — only add throttling if explicitly configured
    settings_parts = []
    if args.ch_max_threads:
        settings_parts.append(f"max_threads = {args.ch_max_threads}")
    if args.ch_priority:
        settings_parts.append(f"priority = {args.ch_priority}")
    settings_parts.append(f"max_execution_time = {args.ch_max_execution_time}")
    settings = ", ".join(settings_parts)

    # ── Discover schema ────────────────────────────────────────────────
    print("Discovering ClickHouse schema...", flush=True)
    cols_text = ch_query_text(ch_url, ch_user, ch_password,
        f"SELECT name, type FROM system.columns "
        f"WHERE database='{ch_db}' AND table='{ch_table}' "
        f"ORDER BY position FORMAT TSV "
        f"SETTINGS {settings}")

    all_columns = []
    for line in cols_text.split("\n"):
        parts = line.split("\t")
        if len(parts) == 2:
            all_columns.append((parts[0], parts[1]))
    all_col_names = [name for name, _ in all_columns]
    print(f"  {len(all_columns)} columns discovered", flush=True)

    # ── Discover ORDER BY key ──────────────────────────────────────────
    create_text = ch_query_text(ch_url, ch_user, ch_password,
        f"SHOW CREATE TABLE {ch_db}.{ch_table} FORMAT TSV "
        f"SETTINGS {settings}")

    order_by_cols = []
    create_normalized = create_text.replace("\\n", "\n")
    m = re.search(r'ORDER BY\s*\(([^)]+)\)', create_normalized)
    if m:
        order_by_cols = [c.strip().strip('`') for c in m.group(1).split(",")]
    print(f"  ORDER BY key: {order_by_cols}", flush=True)

    if not order_by_cols:
        print("ERROR: Could not determine ORDER BY key", file=sys.stderr)
        sys.exit(1)

    # ── Check if FINAL is needed ───────────────────────────────────────
    use_final = "ReplacingMergeTree" in create_normalized or "CollapsingMergeTree" in create_normalized
    if use_final:
        print(f"  Engine: ReplacingMergeTree — will use FINAL for dedup", flush=True)
    else:
        print(f"  Engine: MergeTree — no FINAL needed", flush=True)

    # ── Build column renames ───────────────────────────────────────────
    # Only rename the fixed columns defined in BASE_RENAMES.
    # ORDER BY key columns keep their original names — they are composite
    # dedup keys, not user identifiers.
    renames = dict(BASE_RENAMES)

    # ── Discover partition expression for pruning ──────────────────────
    print(f"  Date range: {args.start_date} to {args.end_date}", flush=True)

    has_time_partition = "toYYYYMM" in create_normalized and "time" in create_normalized

    partition_filter = None
    if has_time_partition:
        partition_months_text = ch_query_text(ch_url, ch_user, ch_password,
            f"SELECT DISTINCT toYYYYMM(toDateTime(time / 1000)) AS m "
            f"FROM {ch_db}.{ch_table} "
            f"WHERE time >= toUInt64(toUnixTimestamp(toDateTime('{args.start_date}') - INTERVAL 7 DAY)) * 1000 "
            f"AND time < toUInt64(toUnixTimestamp(toDateTime('{args.end_date}') + INTERVAL 7 DAY)) * 1000 "
            f"GROUP BY m ORDER BY m FORMAT TSV "
            f"SETTINGS {settings}")
        partition_months = [m.strip() for m in partition_months_text.split("\n") if m.strip()]
        partition_filter = f"toYYYYMM(toDateTime(time / 1000)) IN ({','.join(partition_months)})"
        print(f"  Partition months for pruning: {partition_months}", flush=True)
    else:
        print(f"  No time-based partition — filtering by timeInserted directly", flush=True)

    # ── Build Iceberg catalog ──────────────────────────────────────────
    # S3 location is managed by the Iceberg REST catalog — no S3 config needed here.
    # PyIceberg uses the default AWS credential chain (IAM role / env vars).
    catalog_kwargs = {"uri": args.iceberg_catalog_url}
    if args.aws_region:
        catalog_kwargs["s3.region"] = args.aws_region
    catalog = load_catalog("rest", **catalog_kwargs)

    full_name = f"{args.tenant}.event_result"

    # ── Validate: table must exist (created by FP API) ─────────────────
    print(f"\n=== Step 1: Validate Iceberg table ===", flush=True)
    step1_start = time.time()

    try:
        table = catalog.load_table(full_name)
        existing_cols = len(table.schema().fields)
        print(f"  Table {full_name} exists ({existing_cols} columns)", flush=True)
    except NoSuchTableError:
        print(f"ERROR: Table {full_name} does not exist.", file=sys.stderr)
        print(f"  Register it first: curl -X POST http://<fp-api>/iceberg/connector/{args.tenant}",
              file=sys.stderr)
        sys.exit(1)

    print(f"  Step 1: {time.time() - step1_start:.2f}s", flush=True)

    # ── Step 2: Sync full schema ───────────────────────────────────────
    print(f"\n=== Step 2: Sync schema ({len(all_columns)} CH columns → Iceberg) ===", flush=True)
    step2_start = time.time()

    base_names = {"eventId", "eventType", "userId", "eventTime", "processingTime"}
    new_fields = []
    fid = 100
    for name, ch_type in all_columns:
        iceberg_name = renames.get(name, name)
        if iceberg_name in base_names:
            continue
        iceberg_type = ch_type_to_iceberg(ch_type)
        new_fields.append(T.NestedField(fid, iceberg_name, iceberg_type, required=False))
        fid += 1

    table = catalog.load_table(full_name)
    with table.update_schema() as upd:
        upd.union_by_name(Schema(*new_fields))
    final_cols = len(table.schema().fields)
    print(f"  Iceberg table now has {final_cols} columns", flush=True)
    print(f"  Step 2: {time.time() - step2_start:.2f}s", flush=True)

    # ── Step 3: Column-batched export ──────────────────────────────────
    print(f"\n=== Step 3: Export with {'FINAL + ' if use_final else ''}column batching ===", flush=True)
    step3_start = time.time()

    key_cols = order_by_cols
    other_cols = [c for c in all_col_names if c not in key_cols]
    batch_size = args.column_batch_size
    col_batches = []
    for i in range(0, len(other_cols), batch_size):
        batch = key_cols + other_cols[i:i + batch_size]
        col_batches.append(batch)

    print(f"  {len(col_batches)} column batches of ~{batch_size} + {len(key_cols)} key columns", flush=True)

    from datetime import datetime, timedelta
    start = datetime.strptime(args.start_date, "%Y-%m-%d")
    end = datetime.strptime(args.end_date, "%Y-%m-%d")
    days = []
    d = start
    while d < end:
        days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    print(f"  {len(days)} days to migrate", flush=True)

    state = load_state(args.tenant)
    ok = skipped = failed = 0
    table = catalog.load_table(full_name)

    for day_idx, day in enumerate(days):
        next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

        if state.get(day) == "DONE":
            print(f"  [{day}] SKIP (already done)")
            skipped += 1
            continue

        day_start = time.time()
        batch_tables = []
        export_total = 0

        time_filter = (
            f"timeInserted >= toUInt64(toUnixTimestamp(toDateTime('{day} 00:00:00'))) * 1000 "
            f"AND timeInserted < toUInt64(toUnixTimestamp(toDateTime('{next_day} 00:00:00'))) * 1000"
        )
        if partition_filter:
            where = f"WHERE {partition_filter} AND {time_filter}"
        else:
            where = f"WHERE {time_filter}"

        final_kw = "FINAL" if use_final else ""

        for batch_idx, cols in enumerate(col_batches):
            cols_sql = ", ".join(quote_col(c) for c in cols)
            sql = (
                f"SELECT {cols_sql} FROM {ch_db}.{ch_table} {final_kw} "
                f"{where} "
                f"FORMAT Parquet "
                f"SETTINGS {settings}"
            )

            batch_start = time.time()
            parquet_bytes = ch_query_parquet(ch_url, ch_user, ch_password, sql)
            export_total += time.time() - batch_start

            try:
                batch_pa = pq.read_table(io.BytesIO(parquet_bytes))
                batch_tables.append(batch_pa)
            except Exception as e:
                print(f"  [{day}] batch {batch_idx+1}/{len(col_batches)}: ERROR ({e})")
                break

            if not args.dry_run:
                print(f"  [{day}] batch {batch_idx+1}/{len(col_batches)}: "
                      f"{batch_pa.num_rows} rows, {len(cols)} cols, "
                      f"{time.time() - batch_start:.1f}s", flush=True)

        if len(batch_tables) != len(col_batches):
            state[day] = "FAILED"
            save_state(args.tenant, state)
            failed += 1
            continue

        if batch_tables[0].num_rows == 0:
            print(f"  [{day}] SKIP (no rows)")
            state[day] = "DONE"
            save_state(args.tenant, state)
            skipped += 1
            continue

        # Merge column batches
        merge_start = time.time()
        merged = batch_tables[0]
        for t in batch_tables[1:]:
            for col_name in t.column_names:
                if col_name not in key_cols:
                    merged = merged.append_column(col_name, t.column(col_name))
        merge_time = time.time() - merge_start

        # Convert timestamps + rename
        merged = convert_timestamps(merged)
        merged = rename_columns(merged, renames)

        if args.dry_run:
            print(f"  [{day}] DRY-RUN: {merged.num_rows} rows, {merged.num_columns} cols, "
                  f"export={export_total:.1f}s merge={merge_time:.1f}s")
            state[day] = "DONE"
            save_state(args.tenant, state)
            ok += 1
            continue

        # Write to Iceberg
        write_start = time.time()
        try:
            table.append(merged)
            write_time = time.time() - write_start
            state[day] = "DONE"
            save_state(args.tenant, state)
            ok += 1

            total_day = time.time() - day_start
            print(f"  [{day}] OK: {merged.num_rows} rows, "
                  f"export={export_total:.1f}s merge={merge_time:.1f}s "
                  f"write={write_time:.1f}s total={total_day:.1f}s", flush=True)
        except Exception as e:
            state[day] = "FAILED"
            save_state(args.tenant, state)
            failed += 1
            print(f"  [{day}] FAILED: {e}", file=sys.stderr)
            import traceback; traceback.print_exc()

        # Sleep between days (only when throttling for shared production CH)
        if day_idx < len(days) - 1 and args.sleep_between_days > 0:
            time.sleep(args.sleep_between_days)

    step3_time = time.time() - step3_start
    total = time.time() - step1_start
    print(f"\n=== Migration complete ===", flush=True)
    print(f"  ok={ok}  skipped={skipped}  failed={failed}", flush=True)
    print(f"  Step 3: {step3_time:.1f}s  Total: {total:.1f}s", flush=True)

    if failed:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Column-batched migration: ClickHouse → Iceberg with FINAL dedup. "
                    "See docs/migration-guide.md for benchmarks and details."
    )

    # Required
    parser.add_argument("--tenant", required=True,
                        help="Iceberg namespace (e.g. rippling)")
    parser.add_argument("--clickhouse-url", required=True,
                        help="ClickHouse HTTP URL (port 8123)")
    parser.add_argument("--iceberg-catalog-url", required=True,
                        help="Iceberg REST catalog URL (e.g. http://iceberg-rest-catalog:8181)")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD exclusive")

    # ClickHouse connection
    parser.add_argument("--clickhouse-user", default="default")
    parser.add_argument("--clickhouse-password", default="")
    parser.add_argument("--clickhouse-db", default=None,
                        help="ClickHouse database (defaults to --tenant)")
    parser.add_argument("--clickhouse-table", default="event_result")

    # Migration tuning
    parser.add_argument("--column-batch-size", type=int, default=200,
                        help="Columns per batch (default 200 — tested: 109s/batch in production)")
    parser.add_argument("--aws-region", default="us-west-2")

    # Throttling — disabled by default (full speed for dedicated CH instance).
    # Enable these when running against a shared production CH.
    parser.add_argument("--ch-max-threads", type=int, default=None,
                        help="ClickHouse max_threads (omit for full speed, set to 2 for shared CH)")
    parser.add_argument("--ch-priority", type=int, default=None,
                        help="ClickHouse query priority (omit for default, set to 10 for shared CH)")
    parser.add_argument("--ch-max-execution-time", type=int, default=600,
                        help="ClickHouse max_execution_time in seconds (default 600)")
    parser.add_argument("--sleep-between-days", type=int, default=0,
                        help="Seconds to sleep between days (default 0, set to 30 for shared CH)")

    parser.add_argument("--dry-run", action="store_true",
                        help="Export and merge but skip Iceberg writes")

    args = parser.parse_args()
    if not args.clickhouse_db:
        args.clickhouse_db = args.tenant
    run(args)


if __name__ == "__main__":
    main()
