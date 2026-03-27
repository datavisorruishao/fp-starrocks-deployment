#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-/Users/rshao/work/code_repos/personal/historial_operations/kubeconfig/dev_a.config}"
NAMESPACE="duckdb"

K="kubectl --kubeconfig=${KUBECONFIG_PATH} -n ${NAMESPACE}"

echo "=== CRE-6630 Production Deploy to ${NAMESPACE} ==="
echo "Kubeconfig: ${KUBECONFIG_PATH}"
echo ""

# Pre-check: secrets file
if [ ! -f "${SCRIPT_DIR}/00-secrets.yaml" ]; then
  echo "ERROR: 00-secrets.yaml not found."
  echo "Copy 00-secrets.example.yaml to 00-secrets.yaml and fill in passwords."
  exit 1
fi

cd "${SCRIPT_DIR}"

echo "--- Step 1: Secrets ---"
$K apply -f 00-secrets.yaml

echo ""
echo "--- Step 2: SMT source ConfigMap ---"
$K apply -f 19-smt-source.yaml

echo ""
echo "--- Step 3: Iceberg Catalog MySQL ---"
$K apply -f 10-iceberg-catalog-mysql.yaml
echo "Waiting for iceberg-catalog-mysql..."
$K rollout status statefulset/iceberg-catalog-mysql --timeout=5m

echo ""
echo "--- Step 4: Iceberg REST Catalog ---"
$K apply -f 11-iceberg-rest-catalog.yaml
echo "Waiting for iceberg-rest-catalog..."
$K rollout status deployment/iceberg-rest-catalog --timeout=5m

echo ""
echo "--- Step 5: Kafka Connect ---"
$K apply -f 20-kafka-connect.yaml
echo "Waiting for iceberg-kafka-connect..."
$K rollout status deployment/iceberg-kafka-connect --timeout=10m

echo ""
echo "--- Step 6: Register Kafka Connect connector ---"
# Delete previous job run if exists
$K delete job iceberg-kafka-connect-register --ignore-not-found=true
$K apply -f 21-kafka-connect-register-job.yaml
echo "Waiting for connector registration..."
$K wait --for=condition=complete job/iceberg-kafka-connect-register --timeout=5m

echo ""
echo "--- Step 7: StarRocks FE ---"
$K apply -f 30-starrocks-fe.yaml
echo "Waiting for starrocks-fe..."
$K rollout status statefulset/starrocks-fe --timeout=5m

echo ""
echo "--- Step 8: StarRocks CN ---"
$K apply -f 31-starrocks-cn.yaml
echo "Waiting for starrocks-cn..."
$K rollout status deployment/starrocks-cn --timeout=5m

echo ""
echo "--- Step 9: StarRocks init (register CN + create catalog) ---"
$K delete job starrocks-init --ignore-not-found=true
$K apply -f 32-starrocks-init.yaml
echo "Waiting for starrocks-init..."
$K wait --for=condition=complete job/starrocks-init --timeout=5m

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Verify pods:"
$K get pods | grep -E 'starrocks|iceberg'
echo ""
echo "Next steps:"
echo "  1. Port-forward StarRocks FE: kubectl --kubeconfig=${KUBECONFIG_PATH} -n ${NAMESPACE} port-forward svc/starrocks-fe 9030:9030"
echo "  2. Connect: mysql -h 127.0.0.1 -P 9030 -u root"
echo "  3. Check: SHOW CATALOGS; SHOW COMPUTE NODES;"
