"""Demo DAG for airflow-dag-processor auto-parse.

Pushed to airflow-dag branch after dag-processor is running. If dag-processor
is working, this DAG should appear in `airflow dags list` within ~60s (the
git-sync interval) without any manual `airflow dags reserialize` step.

Keep it tiny — the point is proving the sync+parse loop, not KubernetesExecutor
(that's covered by non_linear_test_dag).
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator


with DAG(
    dag_id="hello_dag_processor",
    description="Smoke test that dag-processor auto-picks new DAG files",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    default_args={"owner": "cre-6630", "retries": 0, "execution_timeout": timedelta(minutes=2)},
    tags=["cre-6630", "smoke-test", "dag-processor"],
) as dag:
    BashOperator(
        task_id="hello",
        bash_command='echo "hello from dag-processor demo at $(date -u +%FT%TZ)"',
    )
