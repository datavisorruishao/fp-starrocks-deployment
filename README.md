# fp-starrocks-deployment

Helm charts for the FP-11811 Feature Stats storage and query pipeline.

## Overview

The project is split into three parts deployed independently:

| Part | Chart | Owner | Status |
|------|-------|-------|--------|
| 1 — Iceberg storage | `charts-iceberg/` | infra (ruishao) | Production |
| 2 — FP Java integration | `feature-platform_FP-11811` branch | backend | Production |
| 3 — StarRocks query engine | `charts-production/` | infra (ruishao) | Production |

### Architecture

```
FP Events
    │
    ▼
Kafka topic  (velocity-al-<tenant>)
    │
    ▼
Kafka Connect  [charts-iceberg]
  + FeatureResolverTransform SMT
  (resolves integer feature IDs → named columns via FP MySQL)
    │
    ▼
S3 Parquet  (Iceberg format)
    │
    ├──► Iceberg REST Catalog  [charts-iceberg]
    │    (backed by FP MySQL iceberg_catalog DB)
    │
    └──► StarRocks FE + CN  [charts-production]
         (reads Iceberg tables from S3 via REST catalog)
         (used for ad-hoc SQL queries on feature stats)
```

## Repository Structure

```
fp-starrocks-deployment/
├── charts-iceberg/          # Part 1 — Iceberg storage pipeline (shared, multi-tenant)
├── charts-production/       # Part 3 — StarRocks query engine (deploy after charts-iceberg)
└── docs/                    # Architecture diagrams and design docs
    ├── CRE-6630-presentation.md
    ├── CRE-6630-api-gaps.md
    ├── brainstorm-summary.md
    ├── sizing-sheet.md
    └── starrocks-multi-tenant-architecture.excalidraw
```

## charts-iceberg (Part 1)

Shared infrastructure deployed **once** for all tenants. See [charts-iceberg/README.md](charts-iceberg/README.md).

Key components:
- Iceberg REST Catalog (backed by FP MySQL `iceberg_catalog` DB)
- Kafka Connect workers with the `FeatureResolverTransform` SMT built from source
- Per-tenant connectors are **not** registered by Helm — FP Java (`IcebergService`) registers them dynamically at tenant provisioning time

## charts-production (Part 3)

StarRocks FE + CN nodes that query Iceberg tables stored in S3. See [charts-production/README.md](charts-production/README.md).

Deploy **after** `charts-iceberg`. The `starrocks-init` Job automatically registers the Iceberg REST Catalog (deployed by `charts-iceberg`) as a StarRocks external catalog.

## Credentials

Passwords are never stored in `values.yaml`. Sensitive credentials are held in pre-existing K8s Secrets that must be created in the cluster before installing a chart. The chart references secrets by name (configurable in `values.yaml`) but never creates or owns them — following the same pattern as the `fp` chart in the `charts/` repo.

## Related

- FP Java changes: `feature-platform_FP-11811` branch (`analytic/IcebergService.java`, `APTenantService.java`, `YAMLConfig.java`)
- Jira: FP-11811
