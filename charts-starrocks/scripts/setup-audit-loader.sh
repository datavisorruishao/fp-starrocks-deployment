#!/usr/bin/env bash
# Setup StarRocks AuditLoader plugin manually (if not done by starrocks-init Job).
# The starrocks-init Job installs AuditLoader automatically on helm install/upgrade.
# Use this script only when you need to install it manually on an existing cluster.
#
# Usage:
#   NAMESPACE=qa-security ./setup-audit-loader.sh
set -euo pipefail

NAMESPACE="${NAMESPACE:-qa-security}"
FE_POD=$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=starrocks-fe -o jsonpath='{.items[0].metadata.name}')
WORK_DIR="/tmp/auditloader-setup"
CONTAINER_PATH="/tmp/auditloader.zip"

echo "=== Setting up StarRocks AuditLoader Plugin (namespace: $NAMESPACE, pod: $FE_POD) ==="

# 1. Create audit database and table
echo "[1/4] Creating audit database and table..."
kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED -e \
  "CREATE DATABASE IF NOT EXISTS starrocks_audit_db__;"

kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED -e "
CREATE TABLE IF NOT EXISTS starrocks_audit_db__.starrocks_audit_tbl__ (
  queryId         VARCHAR(64),
  timestamp       DATETIME NOT NULL,
  queryType       VARCHAR(12),
  clientIp        VARCHAR(32),
  user            VARCHAR(64),
  authorizedUser  VARCHAR(64),
  resourceGroup   VARCHAR(64),
  catalog         VARCHAR(32),
  db              VARCHAR(96),
  state           VARCHAR(8),
  errorCode       VARCHAR(512),
  queryTime       BIGINT,
  scanBytes       BIGINT,
  scanRows        BIGINT,
  returnRows      BIGINT,
  cpuCostNs       BIGINT,
  memCostBytes    BIGINT,
  stmtId          INT,
  isQuery         TINYINT,
  feIp            VARCHAR(128),
  stmt            VARCHAR(1048576),
  digest          VARCHAR(32),
  planCpuCosts    DOUBLE,
  planMemCosts    DOUBLE,
  pendingTimeMs   BIGINT,
  candidateMVs    VARCHAR(65533) NULL,
  hitMvs          VARCHAR(65533) NULL,
  warehouse       VARCHAR(32) NULL
) ENGINE = OLAP
DUPLICATE KEY (queryId, timestamp, queryType)
PARTITION BY date_trunc('day', timestamp)
PROPERTIES (
  'replication_num' = '1',
  'partition_live_number' = '30'
);"

# 2. Download AuditLoader, configure plugin.conf, re-zip
echo "[2/4] Downloading and configuring AuditLoader..."
rm -rf "$WORK_DIR" && mkdir -p "$WORK_DIR"
curl -sL -o "$WORK_DIR/auditloader.zip" \
  https://releases.starrocks.io/resources/auditloader.zip
cd "$WORK_DIR" && unzip -q auditloader.zip

cat > "$WORK_DIR/plugin.conf" << 'CONF'
frontend_host_port=127.0.0.1:8030
database=starrocks_audit_db__
table=starrocks_audit_tbl__
user=root
password=
secret_key=
filter=

# Flush interval: 60 seconds is the production default.
# Audit data is used for post-analysis, not real-time monitoring,
# so 60s is sufficient. Lower only for debugging/testing.
max_batch_interval_sec=60
CONF

rm auditloader.zip
zip -qj auditloader.zip auditloader.jar plugin.conf plugin.properties

# 3. Copy into FE pod and install
echo "[3/4] Installing plugin..."
kubectl -n "$NAMESPACE" cp "$WORK_DIR/auditloader.zip" "$FE_POD:$CONTAINER_PATH"
kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED -e \
  "INSTALL PLUGIN FROM '$CONTAINER_PATH';"

# 4. Verify
echo "[4/4] Verifying installation..."
kubectl -n "$NAMESPACE" exec "$FE_POD" -- mysql -P 9030 -h 127.0.0.1 -u root --ssl-mode=DISABLED -e "SHOW PLUGINS;"

rm -rf "$WORK_DIR"
echo ""
echo "AuditLoader installed. All queries are now recorded in:"
echo "  starrocks_audit_db__.starrocks_audit_tbl__"
echo ""
echo "Query with: NAMESPACE=$NAMESPACE ./query-audit.sh"
