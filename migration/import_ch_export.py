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
import csv
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
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.table.sorting import NullOrder, SortDirection, SortField, SortOrder
from pyiceberg.transforms import DayTransform, IdentityTransform

BATCH_SIZE = 500_000  # rows per Iceberg append (large batches → fewer files)

# ---------------------------------------------------------------------------
# Table optimizations — mirrors SINK_TARGETS in IcebergService.java exactly.
# Field IDs 1-5 correspond to the five fixed base columns (see BASE_FIELDS).
# ---------------------------------------------------------------------------

# Partition: day(processingTime=5), identity(eventType=2)
TABLE_PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=5, field_id=1000, transform=DayTransform(),    name="processingTime_day"),
    PartitionField(source_id=2, field_id=1001, transform=IdentityTransform(), name="eventType"),
)

# Sort: eventType(2) ASC, eventTime(4) ASC, eventId(1) ASC
TABLE_SORT_ORDER = SortOrder(
    SortField(source_id=2, transform=IdentityTransform(), direction=SortDirection.ASC, null_order=NullOrder.NULLS_LAST),
    SortField(source_id=4, transform=IdentityTransform(), direction=SortDirection.ASC, null_order=NullOrder.NULLS_LAST),
    SortField(source_id=1, transform=IdentityTransform(), direction=SortDirection.ASC, null_order=NullOrder.NULLS_LAST),
)

TABLE_PROPERTIES = {
    "format-version":                                    "2",
    "write.format.default":                              "parquet",
    "write.parquet.compression-codec":                   "zstd",
    "write.target-file-size-bytes":                      "1073741824",  # 1GB — avoid splitting within a single append
    "write.metadata.metrics.default":                    "none",
    "write.metadata.metrics.column.eventTime":           "full",
    "write.metadata.metrics.column.processingTime":      "full",
    "write.metadata.metrics.column.eventType":           "full",
    "write.metadata.metrics.column.eventId":             "counts",
    "write.parquet.bloom-filter-enabled.column.eventId": "true",
    "write.parquet.bloom-filter-enabled.column.userId":  "true",
}

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

        # dv_isDetection is stored inverted in Iceberg as fromUpdateAPI.
        # CSV values are "0"/"1" strings after forced-string read; cast via
        # int first since PyArrow bool cast only accepts "true"/"false".
        if name == "dv_isDetection":
            bool_arr = pc.not_equal(arr.cast(pa.int8()), 1)
            arrays.append(bool_arr)
            fields.append(pa.field("fromUpdateAPI", pa.bool_(), nullable=True))
            continue

        # Parse array columns (read as strings by the CSV reader)
        if name in array_columns:
            elem_type = array_columns[name]
            arr = _parse_array_col(arr, elem_type)
            field = pa.field(name, pa.list_(elem_type), nullable=True)

        # Apply column rename
        out_name = renames.get(name, name)

        # eventTime and processingTime must be long (ms since epoch) in Iceberg
        if out_name in ("eventTime", "processingTime") and arr.type == pa.string():
            arr = pc.cast(arr, pa.int64(), safe=False)
            field = pa.field(out_name, pa.int64(), nullable=True)
            fields.append(field)
            arrays.append(arr)
            continue

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

def _pa_to_iceberg_type(t: pa.DataType):
    """Convert a PyArrow type to an Iceberg type (simple mapping, no field-ids needed)."""
    import pyiceberg.types as T
    if t == pa.string() or t == pa.large_string(): return T.StringType()
    if t == pa.int32():    return T.IntegerType()
    if t == pa.int64():    return T.LongType()
    if t == pa.float32():  return T.FloatType()
    if t == pa.float64():  return T.DoubleType()
    if t == pa.bool_():    return T.BooleanType()
    if pa.types.is_list(t):
        return T.ListType(element_id=0, element_type=_pa_to_iceberg_type(t.value_type),
                          element_required=False)
    return T.StringType()  # fallback


def _batch_to_iceberg_schema(batch: pa.RecordBatch, start_id: int = 6):
    """Build an Iceberg Schema from a PyArrow batch without needing field-ids in the Arrow schema."""
    import pyiceberg.types as T
    from pyiceberg.schema import Schema
    base_names = {"eventId", "eventType", "userId", "eventTime", "processingTime"}
    fields = []
    fid = start_id
    for f in batch.schema:
        if f.name in base_names:
            continue
        fields.append(T.NestedField(fid, f.name, _pa_to_iceberg_type(f.type), required=False))
        fid += 1
    return Schema(*fields)


def load_or_create_table(catalog, tenant: str, sample_batch: pa.RecordBatch,
                         no_partition: bool = False):
    """
    Load existing Iceberg table and evolve its schema to include any new columns
    present in sample_batch, or create the table from scratch if it doesn't exist yet.

    Extra columns from ClickHouse (not produced by live Kafka Connect traffic) are
    added as optional fields.  New live rows will have NULL for those columns, which
    is harmless — Iceberg and StarRocks handle sparse nullable columns fine.
    """

    full_name = f"{tenant}.event_result"
    try:
        table = catalog.load_table(full_name)
        # Table already exists — try schema evolution, but skip gracefully
        # if pyarrow_to_schema fails (e.g. after disaster recovery when
        # the table lacks name-mapping metadata).
        try:
            batch_iceberg_schema = _batch_to_iceberg_schema(sample_batch)
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
        except (ValueError, Exception) as e:
            print(f"  Schema evolution skipped ({e.__class__.__name__}): {e}",
                  flush=True)
        return table
    except NoSuchTableError:
        pass

    # Table does not exist — create with ALL columns from the batch at once.
    # Using manual type mapping avoids pyarrow_to_schema field-id requirements.
    try:
        catalog.create_namespace(tenant)
    except Exception:
        pass  # already exists

    import pyiceberg.types as T
    from pyiceberg.schema import Schema

    # eventTime and processingTime are stored as long (ms since epoch) in Iceberg,
    # even though CSV import reads them as strings. Force LongType for these.
    long_columns = {"eventTime", "processingTime"}
    fields = [
        T.NestedField(i + 1, f.name,
                       T.LongType() if f.name in long_columns else _pa_to_iceberg_type(f.type),
                       required=False)
        for i, f in enumerate(sample_batch.schema)
    ]
    full_schema = Schema(*fields)
    table = catalog.create_table(
        full_name, schema=full_schema, properties=TABLE_PROPERTIES,
    )
    print(f"  Created {full_name} ({len(fields)} columns, no partition)", flush=True)
    return table


# ---------------------------------------------------------------------------
# Single-file import
# ---------------------------------------------------------------------------

def _open_gz(fpath: str, fs_or_none):
    """Open a gzip file from local or S3, return (gz_handle, raw_handle_or_None)."""
    if fs_or_none is not None:
        raw = fs_or_none.open(fpath, "rb")
        return gzip.open(raw), raw
    return gzip.open(fpath, "rb"), None


def import_file(fpath: str, fs_or_none, renames: dict[str, str],
                array_columns: dict[str, pa.DataType],
                catalog, tenant: str, dry_run: bool,
                no_partition: bool = False) -> int:
    """Read one CSV.gz and append to Iceberg. Returns rows processed."""

    # First pass: read header to discover column names.
    # We close and re-open because gzip.seek(0) is unreliable on S3 streams.
    gz, raw = _open_gz(fpath, fs_or_none)
    try:
        header_line = gz.readline().decode("utf-8").strip()
    finally:
        gz.close()
        if raw is not None:
            raw.close()

    col_names = next(iter(csv.reader([header_line])))
    all_string_types = {c: pa.string() for c in col_names}

    # Second pass: read full file with all columns forced to string.
    # This avoids PyArrow inferring null type on sparse feature columns
    # (which causes "CSV conversion error to null" when a value appears
    # after many empty rows).
    gz, raw = _open_gz(fpath, fs_or_none)
    try:
        parse_opts = pv.ParseOptions(newlines_in_values=True)
        convert_opts = pv.ConvertOptions(
            column_types=all_string_types,
            strings_can_be_null=True,
            quoted_strings_can_be_null=True,
        )
        read_opts = pv.ReadOptions(block_size=32 * 1024 * 1024)  # 32 MB blocks

        reader = pv.open_csv(gz, read_options=read_opts, parse_options=parse_opts,
                             convert_options=convert_opts)
        full_table = reader.read_all()
    finally:
        gz.close()
        if raw is not None:
            raw.close()

    total_rows = len(full_table)
    print(f"  {total_rows:,} rows  {len(full_table.schema)} columns", flush=True)

    if dry_run:
        print(f"  [dry-run] would append {total_rows:,} rows → {tenant}.event_result")
        return total_rows

    # Transform all batches and combine into one large table,
    # then write in BATCH_SIZE chunks. This avoids creating one file
    # per CSV reader block (~4300 rows) — instead we get one file per chunk.
    print(f"  Transforming...", flush=True)
    transformed_batches = [
        transform_batch(b, renames, array_columns)
        for b in full_table.to_batches()
    ]
    combined = pa.Table.from_batches(transformed_batches)

    iceberg_table = load_or_create_table(
        catalog, tenant, transformed_batches[0], no_partition=no_partition,
    )

    appended = 0
    for i in range(0, total_rows, BATCH_SIZE):
        chunk = combined.slice(i, BATCH_SIZE)
        iceberg_table.append(chunk)
        appended += len(chunk)
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
                no_partition=args.no_partition,
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

    # TODO: After bulk import, add partition spec for future Kafka Connect writes.
    # Requires fixing DayTransform compatibility with LongType (processingTime is
    # stored as milliseconds, not timestamp). For now the table stays unpartitioned.

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
    parser.add_argument("--no-partition",         action="store_true",
                        help="Create table WITHOUT partition spec (bulk import mode). "
                             "Produces fewer, larger files. Partition spec is added "
                             "after import for future Kafka Connect writes.")
    parser.add_argument("--dry-run",             action="store_true",
                        help="Parse and count rows, skip Iceberg writes")

    args = parser.parse_args()

    # Default CH DB to tenant name if not specified
    if args.clickhouse_url and not args.clickhouse_db:
        args.clickhouse_db = args.tenant

    run(args)


if __name__ == "__main__":
    main()