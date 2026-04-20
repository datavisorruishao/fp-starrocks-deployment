#!/usr/bin/env bash
# Query StarRocks audit log table for Iceberg query resource metrics.
#
# Usage:
#   NAMESPACE=qa-security ./query-audit.sh
set -euo pipefail

NAMESPACE="${NAMESPACE:-qa-security}"
FE_POD=$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=starrocks-fe -o jsonpath='{.items[0].metadata.name}')

echo "=== Iceberg queries — resource usage (most recent) ==="
kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED --table -e "
SELECT
    DATE_FORMAT(timestamp, '%H:%i:%S')   AS time,
    queryTime                            AS wall_ms,
    scanBytes                            AS scan_bytes,
    scanRows                             AS scan_rows,
    ROUND(cpuCostNs / 1e6, 2)           AS cpu_ms,
    ROUND(memCostBytes / 1024 / 1024, 2) AS mem_mb,
    LEFT(stmt, 80)                       AS query
FROM starrocks_audit_db__.starrocks_audit_tbl__
WHERE isQuery = 1
  AND catalog = 'iceberg_catalog'
ORDER BY timestamp DESC
LIMIT 20;"

echo ""
echo "=== Top queries by CPU cost ==="
kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED --table -e "
SELECT
    ROUND(cpuCostNs / 1e6, 2)           AS cpu_ms,
    scanBytes                            AS scan_bytes,
    scanRows                             AS scan_rows,
    queryTime                            AS wall_ms,
    LEFT(stmt, 80)                       AS query
FROM starrocks_audit_db__.starrocks_audit_tbl__
WHERE isQuery = 1
  AND catalog = 'iceberg_catalog'
ORDER BY cpuCostNs DESC
LIMIT 10;"

echo ""
echo "=== Failed queries ==="
kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED --table -e "
SELECT
    DATE_FORMAT(timestamp, '%H:%i:%S') AS time,
    state,
    errorCode,
    LEFT(stmt, 80)                     AS query
FROM starrocks_audit_db__.starrocks_audit_tbl__
WHERE state != 'EOF'
ORDER BY timestamp DESC
LIMIT 10;"
