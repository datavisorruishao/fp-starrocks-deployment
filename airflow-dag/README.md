# airflow-dag branch

This branch holds Airflow DAG files synced to the qa-security cluster via **git-sync**.

## How it's synced

See `docs/AIRFLOW-ARCHITECTURE.md` on `main` for the full model. Short version:

- `charts-airflow` Helm release has git-sync enabled (sidecar on scheduler / webserver; init container on KubernetesExecutor worker pods).
- git-sync tracks this branch (`airflow-dag`) and the `airflow-dag/` subdirectory.
- Airflow's `AIRFLOW__CORE__DAGS_FOLDER` resolves to `/opt/airflow/dags/dags/airflow-dag/` inside every pod.

## Why a separate branch

DAG code iteration should not require `main` reviews for chart/infra. This branch exists so anyone adding/fixing a DAG can commit here without touching the Helm chart on `main`.

## Adding a DAG

1. Check out this branch
2. Drop a `.py` file into `airflow-dag/` (no subdirectories — keeps `DAGS_FOLDER` flat)
3. Commit and push — git-sync will pick it up within ~60 s
4. Verify in the Airflow UI that the DAG appears and parses cleanly
