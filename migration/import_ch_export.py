#!/usr/bin/env python3
"""
Import historical event_result data from ClickHouse export job CSV.gz files into Iceberg.

The CH export job writes weekly snapshots to S3 as:
  s3://{bucket}/{prefix}/raw__ch-exporter-event_result-{timeInserted_ms}-{hash}.csv.gz

Column renames applied (CH → Iceberg), matching the SMT output:
  time         → eventTime       (event timestamp, milliseconds)
  timeInserted → processingTime  (insert timestamp, milliseconds)
  {primary-key} → userId         (entity identifier)

Array columns are auto-discovered from ClickHouse system.columns (requires --clickhouse-url).
Without a ClickHouse connection, array columns remain as raw strings in Iceberg.

Usage:
    python import_ch_export.py \\
        --s3-source s3://datavisor-rippling/DATASET/raw__/ \\
        --tenant rippling \\
        --iceberg-catalog-url http://iceberg-rest-catalog:8181 \\
        --aws-region us-west-2 \\
        --clickhouse-url http://clickhouse-host:8123 \\
        --clickhouse-db rippling \\
        [--clickhouse-user default] \\
        [--clickhouse-password ''] \\
        [--primary-key customer_id]  # auto-discovered from CH if omitted
        [--dry-run]

State file: import_ch_export_state_{tenant}.json
  Tracks completed files by filename. Re-runs skip already-done files.
  Delete a filename entry (or the whole file) to force re-import.
"""

import argparse
import ast
import gzip
import json
import os
import sys

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pv
import requests
import s3fs
from pyiceberg.catalog import load_catalog
from pyiceberg.exceptions import NoSuchTableError

BATCH_SIZE = 50_000   # rows per Iceberg append (wide schema → smaller batches)

# Fixed column renames: CH name → Iceberg name (applied to every tenant).
# Matches the live Kafka Connect + FeatureResolverTransform SMT pipeline output.
BASE_RENAMES = {
    "time":                 "eventTime",
    "timeInserted":         "processingTime",
    "dv_reevaluate_entity": "reEvaluateEntity",
    "origin_id":            "originId",
    "origin_category":      "originCategory",
}


# ---------------------------------------------------------------------------
# ClickHouse schema discovery
# ---------------------------------------------------------------------------

def _ch_post(ch_url: str, ch_user: str, ch_password: str, query: str) -> str:
    """Execute a ClickHouse query and return the plain-text result."""
    resp = requests.post(
        ch_url,
        auth=(ch_user, ch_password) if ch_user else None,
        data=query.encode(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text.strip()


def _ch_inner_type_to_pa(ch_type: str) -> pa.DataType:
    """
    Map a ClickHouse scalar type string to a PyArrow DataType.

    Handles: Nullable(...), LowCardinality(...), Int*, UInt*, Float*, String,
    FixedString, Bool.  Falls back to pa.string() for unknown types.
    """
    t = ch_type.strip()
    # Strip Nullable / LowCardinality wrappers (may be nested)
    while True:
        if t.startswith("Nullable(") and t.endswith(")"):
            t = t[9:-1]
        elif t.startswith("LowCardinality(") and t.endswith(")"):
            t = t[15:-1]
        else:
            break
    if t in ("String",) or t.startswith("FixedString("):
        return pa.string()
    if t in ("Int8", "Int16", "Int32", "UInt8", "UInt16"):
        return pa.int32()
    if t in ("Int64", "UInt32", "UInt64"):
        return pa.int64()
    if t == "Float32":
        return pa.float32()
    if t == "Float64":
        return pa.float64()
    if t == "Bool":
        return pa.bool_()
    return pa.string()  # fallback for any other type


def discover_array_columns(ch_url: str, ch_user: str, ch_password: str,
                           ch_db: str) -> dict[str, pa.DataType]:
    """
    Query system.columns for event_result and return a mapping of
    array column name → PyArrow element type.

    ClickHouse type strings look like: Array(Int32), Array(String),
    Nullable(Array(Float64)), etc.
    """
    query = (
        f"SELECT name, type FROM system.columns "
        f"WHERE database='{ch_db}' AND table='event_result' "
        f"ORDER BY position FORMAT TSV"
    )
    text = _ch_post(ch_url, ch_user, ch_password, query)

    array_cols: dict[str, pa.DataType] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name, raw_type = parts
        t = raw_type.strip()
        # Strip outer Nullable
        if t.startswith("Nullable(") and t.endswith(")"):
            t = t[9:-1]
        if t.startswith("Array(") and t.endswith(")"):
            inner = t[6:-1]
            array_cols[name] = _ch_inner_type_to_pa(inner)

    return array_cols


def discover_primary_key(ch_url: str, ch_user: str, ch_password: str,
                         ch_db: str) -> str:
    """
    Query system.tables to find the primary key column for event_result.
    Returns the first component if the key is composite.
    """
    query = (
        f"SELECT primary_key FROM system.tables "
        f"WHERE database='{ch_db}' AND name='event_result' FORMAT TSV"
    )
    text = _ch_post(ch_url, ch_user, ch_password, query)
    # primary_key may be "customer_id" or "customer_id, event_id" — take first
    return text.split(",")[0].strip()


# ---------------------------------------------------------------------------
# Array parsing
# ---------------------------------------------------------------------------

def _parse_array_col(arr: pa.Array, elem_type: pa.DataType) -> pa.Array:
    """Parse a string column of Python list reprs into a real PyArrow list array."""
    list_type = pa.list_(elem_type)
    parsed = []
    for val in arr.to_pylist():
        if not val:
            parsed.append(None)
            continue
        try:
            items = ast.literal_eval(val)
            if pa.types.is_integer(elem_type):
                parsed.append([int(x) for x in items if x is not None])
            elif pa.types.is_floating(elem_type):
                parsed.append([float(x) for x in items if x is not None])
            else:
                parsed.append([str(x) for x in items if x is not None])
        except Exception:
            parsed.append(None)
    return pa.array(parsed, type=list_type)


# ---------------------------------------------------------------------------
# Batch transformation
# ---------------------------------------------------------------------------

def transform_batch(batch: pa.RecordBatch, renames: dict[str, str],
                    array_columns: dict[str, pa.DataType]) -> pa.RecordBatch:
    """
    Transform one batch:
      - Convert dv_isDetection → fromUpdateAPI (boolean NOT)
      - Rename fixed columns to their Iceberg names
      - Parse array columns from Python list repr strings to real list arrays
    """
    arrays = []
    fields = []

    for i, field in enumerate(batch.schema):
        name = field.name
        arr = batch.column(i)

        # dv_isDetection is stored inverted in Iceberg as fromUpdateAPI
        if name == "dv_isDetection":
            arrays.append(pc.invert(arr.cast(pa.bool_())))
            fields.append(pa.field("fromUpdateAPI", pa.bool_(), nullable=True))
            continue

        # Parse array columns (read as strings by the CSV reader)
        if name in array_columns:
            elem_type = array_columns[name]
            arr = _parse_array_col(arr, elem_type)
            field = pa.field(name, pa.list_(elem_type), nullable=True)

        # Apply column rename
        out_name = renames.get(name, name)
        fields.append(pa.field(out_name, field.type, nullable=True))
        arrays.append(arr)

    return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def state_path(tenant: str) -> str:
    return f"import_ch_export_state_{tenant}.json"


def load_state(tenant: str) -> dict:
    path = state_path(tenant)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(tenant: str, state: dict):
    with open(state_path(tenant), "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Iceberg helpers
# ---------------------------------------------------------------------------

def load_or_create_table(catalog, tenant: str, sample_batch: pa.RecordBatch):
    """
    Load existing Iceberg table and evolve its schema to include any new columns
    present in sample_batch, or create the table from scratch if it doesn't exist yet.

    Extra columns from ClickHouse (not produced by live Kafka Connect traffic) are
    added as optional fields.  New live rows will have NULL for those columns, which
    is harmless — Iceberg and StarRocks handle sparse nullable columns fine.
    """
    from pyiceberg.io.pyarrow import pyarrow_to_schema

    full_name = f"{tenant}.event_result"
    try:
        table = catalog.load_table(full_name)
        # Table already exists (e.g. pre-created by IcebergService.registerConnector).
        # Evolve schema to add any CH columns not yet present.
        batch_iceberg_schema = pyarrow_to_schema(
            pa.Table.from_batches([sample_batch]).schema
        )
        with table.update_schema() as upd:
            upd.union_by_name(batch_iceberg_schema)
        existing_cols = len(table.schema().fields)
        new_cols = len(batch_iceberg_schema.fields)
        if new_cols > existing_cols:
            print(
                f"  Evolved schema: added {new_cols - existing_cols} column(s) "
                f"({existing_cols} → {new_cols})",
                flush=True,
            )
        return table
    except NoSuchTableError:
        pass

    # Table does not exist at all — create it from the batch schema.
    try:
        catalog.create_namespace(tenant)
    except Exception:
        pass  # already exists

    import pyiceberg.types as T

    def to_iceberg(t: pa.DataType) -> T.IcebergType:
        if t == pa.string() or t == pa.large_string():  return T.StringType()
        if t == pa.int32():                              return T.IntegerType()
        if t == pa.int64():                              return T.LongType()
        if t == pa.float32():                            return T.FloatType()
        if t == pa.float64():                            return T.DoubleType()
        if t == pa.bool_():                              return T.BooleanType()
        if pa.types.is_list(t):
            elem = to_iceberg(t.value_type)
            return T.ListType(element_id=0, element_type=elem, element_required=False)
        return T.StringType()  # fallback

    from pyiceberg.schema import Schema
    fields = [
        T.NestedField(i + 1, f.name, to_iceberg(f.type), required=False)
        for i, f in enumerate(sample_batch.schema)
    ]
    schema = Schema(*fields)
    catalog.create_table(full_name, schema=schema)
    print(f"  Created Iceberg table {full_name} ({len(fields)} columns)", flush=True)
    return catalog.load_table(full_name)


# ---------------------------------------------------------------------------
# Single-file import
# ---------------------------------------------------------------------------

def import_file(fpath: str, fs_or_none, renames: dict[str, str],
                array_columns: dict[str, pa.DataType],
                catalog, tenant: str, dry_run: bool) -> int:
    """Read one CSV.gz and append to Iceberg. Returns rows processed."""

    # Open: local or S3
    if fs_or_none is not None:
        raw = fs_or_none.open(fpath, "rb")
        gz = gzip.open(raw)
    else:
        gz = gzip.open(fpath, "rb")

    try:
        # Force array-typed columns to be read as strings so we can parse them
        convert_opts = pv.ConvertOptions(
            column_types={col: pa.string() for col in array_columns},
            strings_can_be_null=True,
            quoted_strings_can_be_null=True,
        )
        read_opts = pv.ReadOptions(block_size=32 * 1024 * 1024)  # 32 MB blocks

        reader = pv.open_csv(gz, read_options=read_opts, convert_options=convert_opts)
        full_table = reader.read_all()
    finally:
        gz.close()
        if fs_or_none is not None:
            raw.close()

    total_rows = len(full_table)
    print(f"  {total_rows:,} rows  {len(full_table.schema)} columns", flush=True)

    if dry_run:
        print(f"  [dry-run] would append {total_rows:,} rows → {tenant}.event_result")
        return total_rows

    iceberg_table = None
    appended = 0

    for raw_batch in full_table.to_batches(max_chunksize=BATCH_SIZE):
        batch = transform_batch(raw_batch, renames, array_columns)

        if iceberg_table is None:
            iceberg_table = load_or_create_table(catalog, tenant, batch)

        iceberg_table.append(pa.Table.from_batches([batch]))
        appended += len(batch)
        print(f"  ... {appended:,}/{total_rows:,} rows appended", flush=True)

    return appended


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    # --- Schema discovery (or explicit primary key) ---
    array_columns: dict[str, pa.DataType] = {}
    primary_key = args.primary_key  # may be None

    if args.clickhouse_url:
        print("Discovering schema from ClickHouse ...", flush=True)
        array_columns = discover_array_columns(
            args.clickhouse_url, args.clickhouse_user, args.clickhouse_password,
            args.clickhouse_db,
        )
        print(f"  Found {len(array_columns)} array column(s): {list(array_columns)}")

        if primary_key is None:
            primary_key = discover_primary_key(
                args.clickhouse_url, args.clickhouse_user, args.clickhouse_password,
                args.clickhouse_db,
            )
            print(f"  Primary key: {primary_key}")
    else:
        if primary_key is None:
            print(
                "WARNING: no --clickhouse-url provided and no --primary-key specified. "
                "No column will be renamed to 'userId'. "
                "Array columns will be stored as raw strings.",
                file=sys.stderr,
            )
        else:
            print(
                "WARNING: no --clickhouse-url provided. "
                "Array columns will be stored as raw strings.",
                file=sys.stderr,
            )

    # Build column rename map: base renames + primary key → userId
    renames = dict(BASE_RENAMES)
    if primary_key and primary_key != "userId":
        renames[primary_key] = "userId"

    # Build Iceberg catalog
    catalog_kwargs: dict = {"uri": args.iceberg_catalog_url}
    if args.aws_region:
        catalog_kwargs["s3.region"] = args.aws_region
    catalog = load_catalog("rest", **catalog_kwargs)

    # Resolve source files
    if args.s3_source:
        src_fs = s3fs.S3FileSystem(anon=False)
        prefix = args.s3_source.rstrip("/").replace("s3://", "")
        all_keys = sorted(src_fs.ls(prefix, detail=False))
        files = sorted(
            [k for k in all_keys if "ch-exporter-event_result" in k.split("/")[-1]],
            key=lambda k: k.split("/")[-1],
        )
        print(f"Found {len(files)} CH export file(s) in {args.s3_source}")
    elif args.s3_file:
        src_fs = s3fs.S3FileSystem(anon=False)
        files = sorted(
            [f.replace("s3://", "") for f in args.s3_file],
            key=lambda k: k.split("/")[-1],
        )
        print(f"Processing {len(files)} explicit S3 file(s)")
    else:
        src_fs = None
        files = args.local_file
        print(f"Processing {len(files)} local file(s)")

    state = load_state(args.tenant)
    ok = skipped = failed = 0

    for fpath in files:
        fname = os.path.basename(fpath)

        if state.get(fname) == "DONE":
            print(f"[{fname}] SKIP (already done)")
            skipped += 1
            continue

        print(f"\n[{fname}]", flush=True)
        try:
            rows = import_file(
                fpath, src_fs, renames, array_columns,
                catalog, args.tenant, args.dry_run,
            )
            state[fname] = "DONE"
            save_state(args.tenant, state)
            suffix = "(dry-run)" if args.dry_run else "appended"
            print(f"[{fname}] OK — {rows:,} rows {suffix}")
            ok += 1
        except Exception as e:
            state[fname] = "FAILED"
            save_state(args.tenant, state)
            print(f"[{fname}] FAILED: {e}", file=sys.stderr)
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\nDone. ok={ok}  skipped={skipped}  failed={failed}")
    if failed:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Import CH export CSV.gz files into Iceberg (event_result)."
    )

    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--s3-source", metavar="S3_PREFIX",
        help="S3 prefix — auto-discovers all ch-exporter-event_result files under the prefix, "
             "e.g. s3://datavisor-rippling/DATASET/raw__/",
    )
    src.add_argument(
        "--s3-file", nargs="+", metavar="S3_URI",
        help="Explicit S3 file path(s), e.g. s3://datavisor-rippling/DATASET/raw__/raw__ch-exporter-event_result-*.csv.gz",
    )
    src.add_argument(
        "--local-file", nargs="+", metavar="FILE",
        help="Local CSV.gz file(s) — for testing only",
    )

    parser.add_argument("--tenant",              required=True,
                        help="Iceberg namespace — must match the tenant name in FP "
                             "(e.g. rippling). The Iceberg table will be {tenant}.event_result.")
    parser.add_argument("--iceberg-catalog-url", required=True,
                        help="Iceberg REST catalog URL. "
                             "This is the K8s service deployed by charts-iceberg. "
                             "Inside the cluster: http://iceberg-rest-catalog:8181. "
                             "From outside, use kubectl port-forward: "
                             "kubectl port-forward svc/iceberg-rest-catalog 8181:8181 "
                             "then use http://localhost:8181.")
    parser.add_argument("--aws-region",          default="us-west-2")

    # ClickHouse connection — optional but recommended for dynamic schema discovery.
    # The CH HTTP interface runs on port 8123 (not the native port 9000).
    # In preprod/prod the CH host is typically the internal K8s service or a direct
    # node IP — check the FP application.yaml clickhouseUrl for the correct host.
    parser.add_argument("--clickhouse-url",      default=None,
                        help="ClickHouse HTTP interface URL (port 8123, not 9000). "
                             "Find the host in FP application.yaml (clickhouseUrl). "
                             "Example: http://chi-dv-datavisor-0-0-0.clickhouse.svc:8123")
    parser.add_argument("--clickhouse-user",     default="default")
    parser.add_argument("--clickhouse-password", default="")
    parser.add_argument("--clickhouse-db",       default=None,
                        help="ClickHouse database name — usually the same as --tenant "
                             "(defaults to --tenant if omitted)")

    parser.add_argument("--primary-key",         default=None,
                        help="CH column to rename to 'userId'. "
                             "Auto-discovered from CH if --clickhouse-url is provided.")
    parser.add_argument("--dry-run",             action="store_true",
                        help="Parse and count rows, skip Iceberg writes")

    args = parser.parse_args()

    # Default CH DB to tenant name if not specified
    if args.clickhouse_url and not args.clickhouse_db:
        args.clickhouse_db = args.tenant

    run(args)


if __name__ == "__main__":
    main()
