"""Non-linear smoke test DAG for KubernetesExecutor.

Topology:

                    ┌─► branch_a ─┐
        start ──────┼─► branch_b ─┼──► join ──► end
                    └─► branch_c ─┘

6 tasks total. branch_{a,b,c} should run in parallel (three worker pods
scheduled at the same time), verifying that the KubernetesExecutor pod
template can spin up multiple workers concurrently and that fan-in
dependency resolution works.
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator


default_args = {
    "owner": "cre-6630",
    "retries": 0,
    "execution_timeout": timedelta(minutes=5),
}

with DAG(
    dag_id="non_linear_test_dag",
    description="Fan-out/fan-in smoke test for KubernetesExecutor",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    default_args=default_args,
    tags=["cre-6630", "smoke-test", "kubernetes-executor"],
) as dag:
    start = BashOperator(
        task_id="start",
        bash_command='echo "start at $(date -u +%FT%TZ) on $(hostname)"',
    )

    branch_a = BashOperator(
        task_id="branch_a",
        bash_command='echo "branch_a on $(hostname)" && sleep 5 && echo "branch_a done"',
    )

    branch_b = BashOperator(
        task_id="branch_b",
        bash_command='echo "branch_b on $(hostname)" && sleep 5 && echo "branch_b done"',
    )

    branch_c = BashOperator(
        task_id="branch_c",
        bash_command='echo "branch_c on $(hostname)" && sleep 5 && echo "branch_c done"',
    )

    join = BashOperator(
        task_id="join",
        bash_command='echo "join: all three branches converged on $(hostname)"',
    )

    end = BashOperator(
        task_id="end",
        bash_command='echo "end at $(date -u +%FT%TZ)"',
    )

    start >> [branch_a, branch_b, branch_c] >> join >> end
