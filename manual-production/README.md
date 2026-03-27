# CRE-6630 Production Deploy (duckdb namespace)

Production-grade deployment with StarRocks FE+CN separation (shared-data mode).

## Architecture

```
fp-async (JDBC:9030) ──→ StarRocks FE (StatefulSet, 1 replica)
                              ├── CN Query (Deployment, 1 replica, stateless)
                              └── reads from S3

Kafka (velocity-al topic) ──→ Kafka Connect ──→ S3 Parquet (Iceberg)
                                                     ↑
                                              Iceberg REST Catalog
                                                     ↑
                                              Iceberg Catalog MySQL
```

## Components

| Component | K8s Resource | Service | Image |
|-----------|-------------|---------|-------|
| Iceberg Catalog MySQL | StatefulSet (1) | iceberg-catalog-mysql:3306 | mysql:8.0 |
| Iceberg REST Catalog | Deployment (1) | iceberg-rest-catalog:8181 | tabulario/iceberg-rest:1.6.0 |
| Kafka Connect | Deployment (1) | iceberg-kafka-connect:8083 | confluentinc/cp-kafka-connect-base:7.7.1 |
| StarRocks FE | StatefulSet (1) | starrocks-fe:9030 | starrocks/fe-ubuntu:3.3-latest |
| StarRocks CN | Deployment (1) | starrocks-cn:9050 | starrocks/cn-ubuntu:3.3-latest |

## Key Config (duckdb / qaautotest)

- Namespace: `duckdb`
- Kafka: `kafka3.duckdb:9092` (3 brokers)
- Source topic: `duckdb_fp_velocity-al-.qaautotest`
- FP MySQL (SMT): `fp-mysql.duckdb:3306/qaautotest` (read-only `dv.ro`)
- S3 warehouse: `s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/`
- StarRocks S3 data: `s3://datavisor-dev-us-west-2-iceberg/cre-6630/duckdb/starrocks-data/`
- Iceberg table: `qaautotest.event_result`

## Deploy

1. Copy and fill secrets:
   ```bash
   cp 00-secrets.example.yaml 00-secrets.yaml
   # Edit 00-secrets.yaml — fill in Iceberg MySQL passwords
   # The SMT password for dv.ro is already "password" in QA
   ```

2. Run deploy script:
   ```bash
   bash deploy-duckdb.sh
   ```

3. Verify:
   ```bash
   # Port-forward StarRocks
   kubectl --kubeconfig=...dev_a.config -n duckdb port-forward svc/starrocks-fe 9030:9030

   # Connect
   mysql -h 127.0.0.1 -P 9030 -u root

   # Check
   SHOW CATALOGS;
   SHOW COMPUTE NODES;
   SELECT COUNT(*) FROM iceberg_catalog.qaautotest.event_result;
   ```

## Differences from QA (manual/)

| Aspect | QA | Production |
|--------|-----|-----------|
| StarRocks | allin1 standalone | FE StatefulSet + CN Deployment (shared-data) |
| Namespace | qa-security | duckdb |
| Kafka topic | cre6630_fp_velocity.yysecurity | duckdb_fp_velocity-al-.qaautotest |
| Connect replication | 1 | 3 |
| FP MySQL | fp-mysql.qa-security/yysecurity | fp-mysql.duckdb/qaautotest |

## Cleanup

```bash
K="kubectl --kubeconfig=...dev_a.config -n duckdb"
$K delete job starrocks-init iceberg-kafka-connect-register --ignore-not-found
$K delete deployment starrocks-cn iceberg-kafka-connect iceberg-rest-catalog --ignore-not-found
$K delete statefulset starrocks-fe iceberg-catalog-mysql --ignore-not-found
$K delete svc starrocks-fe starrocks-cn iceberg-kafka-connect iceberg-rest-catalog iceberg-catalog-mysql --ignore-not-found
$K delete configmap starrocks-fe-config starrocks-cn-config iceberg-kafka-connect-connector fp-feature-smt-source --ignore-not-found
$K delete secret iceberg-catalog-mysql iceberg-rest-catalog-secret iceberg-smt-mysql --ignore-not-found
$K delete serviceaccount iceberg-rest-catalog iceberg-kafka-connect --ignore-not-found
$K delete pvc data-iceberg-catalog-mysql-0 fe-meta-starrocks-fe-0 --ignore-not-found
```
