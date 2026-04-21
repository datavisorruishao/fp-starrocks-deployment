# Airflow 部署架构 (CRE-6630, qa-security)

本文说明本次在 qa-security namespace 部署的 Airflow 3.2 是怎么跑起来的，重点是 **KubernetesExecutor 的工作方式** 和 **DAG 分发（git-sync）**。最后列一下"自研 MySQL-based DAG 分发"需要做哪些事情，并说明为什么短期内不推荐。

---

## 1. Airflow 角色

Airflow 可以拆成四个角色，彼此通过 **metadata DB** 交换状态：

| 角色 | 干什么 | 本次部署形态 |
|---|---|---|
| **webserver** | UI + REST API。查看 DAG / task 状态、手动 trigger、查 log | 常驻 Deployment `airflow-webserver` |
| **scheduler** | 周期性扫 DAG 文件夹，写 `dag_run` / `task_instance` 到 DB；**还内嵌了 executor 逻辑**，真正把 task 交给 worker 跑的就是它 | 常驻 Deployment `airflow-scheduler` |
| **metadata DB** | 所有状态的 source of truth：DAG 定义、run/task 状态、connection、variable、xcom | StatefulSet `airflow-db`（PostgreSQL 16，单实例 + PVC）|
| **executor + worker** | task 实际执行的地方。类型不同长相差别很大 | **KubernetesExecutor**：没有常驻 worker；scheduler 每来一个 task 就调 k8s API 现场起一个 worker Pod，跑完即销毁 |

### 数据流（简化版）

```
    ┌───────────────┐
    │  DAG 文件源    │  ← git-sync 从 GitHub 拉
    │ (airflow-dag  │
    │   branch)     │
    └───────┬───────┘
            │ git-sync 同步到 /opt/airflow/dags/
            ▼
 ┌─────────────────────┐          ┌─────────────────────┐
 │   scheduler pod     │◄────────►│    webserver pod    │
 │ ┌─────────────────┐ │          │ ┌─────────────────┐ │
 │ │    scheduler    │ │          │ │   api-server    │ │
 │ └─────────────────┘ │          │ └─────────────────┘ │
 │ ┌─────────────────┐ │          │ ┌─────────────────┐ │
 │ │ git-sync sidecar│ │          │ │ git-sync sidecar│ │
 │ └─────────────────┘ │          │ └─────────────────┘ │
 └──────────┬──────────┘          └──────────┬──────────┘
            │                                │
            │      ┌──────────────────┐      │
            └─────►│  airflow-db      │◄─────┘
                   │  (PostgreSQL)    │
                   └──────────────────┘
            │
            │ scheduler 发现有 task 要跑
            │ 调 k8s API POST /pods
            ▼
 ┌────────────────────────────────────────────────────────┐
 │  Worker Pod（现起现销，一个 task 一个 pod）             │
 │ ┌──────────────────────┐                               │
 │ │ initContainer:       │   先拉 DAG（git-sync one-time）│
 │ │   git-sync-init      │                               │
 │ └──────────────────────┘                               │
 │ ┌──────────────────────┐                               │
 │ │ container: base      │   跑 airflow tasks run ...   │
 │ │  - env: airflow-env  │                               │
 │ │  - mount: /opt/.../dags                              │
 │ └──────────────────────┘                               │
 └────────────────────────────────────────────────────────┘
```

---

## 2. KubernetesExecutor 细节

### 2.1 为什么没有 worker Deployment？

和 CeleryExecutor 不一样，KubernetesExecutor **不需要**一批常驻 worker 在 Redis/RabbitMQ 队列上等任务。scheduler 进程里的 `KubernetesExecutor` 类直接做这件事：

```
for each scheduled task:
    render pod spec from template  ──┐
    call k8s API: create pod         │  这几步都在 scheduler 进程里
    watch pod status → update DB    ──┘
```

好处：资源隔离（每个 task 独立 pod、独立 request/limit、崩了也不影响别的）、无闲置 worker。

代价：每个 task 都有冷启动（pod 创建 + image pull + init container）。

### 2.2 pod_template_file：worker pod 的"出生证明"

scheduler 要调 `POST /api/v1/namespaces/qa-security/pods` 就得有完整 Pod spec。这个 spec 来源：

1. **默认模板** —— Airflow image 里自带一份最简的 pod_template，只有一个 base 容器跑 airflow image。没有任何外挂 env、volume、secret。
2. **自定义模板** —— 由 `AIRFLOW__KUBERNETES_EXECUTOR__POD_TEMPLATE_FILE` 指向一个 yaml 文件。scheduler 以它为蓝本，填进 DAG / task 信息后发给 k8s。

**本次部署使用自定义模板**（default 模板跑不起我们的 DAG，原因：没 DAG volume、没 envFrom airflow-env、没 DB 连接）。

### 2.3 本次的 pod template

文件走 ConfigMap → mount 到 scheduler pod：

```
ConfigMap: airflow-worker-pod-template
  └── data["pod_template.yaml"]
        │
        ▼ mount as subPath file
  scheduler pod: /opt/airflow/pod_templates/pod_template.yaml
        │
        ▼ 通过 env AIRFLOW__KUBERNETES_EXECUTOR__POD_TEMPLATE_FILE
  scheduler 读取并拿它生成 worker pod spec
```

template 内容要点：
- `metadata.name: placeholder` — Airflow 自动重写为 `<dag_id>-<task_id>-<uuid>`
- `spec.restartPolicy: Never` — task 失败了就失败，不要 restart；Airflow 自己管重试
- `spec.serviceAccountName: airflow` — 用同一个 SA（但 worker pod 其实不需要 k8s RBAC，只是借用而已）
- `initContainers[0]: git-sync-init` — `GITSYNC_ONE_TIME=true`，拉完就退
- `containers[0].name: base` — **必须叫 base**，Airflow 会把 `["airflow", "tasks", "run", ...]` 命令注入到这个容器
- `containers[0].envFrom: airflow-env` ConfigMap — 继承 executor 配置
- `containers[0].env`: `POSTGRES_PASSWORD` + `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`（带 `$(POSTGRES_PASSWORD)` 展开）
- `containers[0].volumeMounts: dags → /opt/airflow/dags`
- `volumes.dags: emptyDir` — 和 init container 的 `/git` 是同一个 volume

### 2.4 worker pod 生命周期

```
t=0    scheduler 发现 task "branch_a" 排到了
t=0    scheduler 渲染 pod template，POST 给 k8s
t=1    k8s 调度 pod 到 node，pull image（如果没缓存）
t=5    init container "git-sync-init" 起来，克隆 airflow-dag 分支到 emptyDir
t=10   init 退出，base 容器起来
t=10   base 跑 `airflow tasks run non_linear_test_dag branch_a <run_id> ...`
t=10   airflow 进程从 DB 拿到 task 上下文，import DAG 文件，执行 BashOperator
t=15   task 结束，base 退出 0
t=16   scheduler 收到 pod 完成事件，写 state=success 到 DB
t=16   因为 DELETE_WORKER_PODS=true，pod 被清掉（失败时因 DELETE_WORKER_PODS_ON_FAILURE=false 保留 pod 供排查）
```

---

## 3. git-sync：DAG 分发机制

### 3.1 git-sync 是什么

Kubernetes SIG 维护的镜像（`registry.k8s.io/git-sync/git-sync:v4.x`），把一个 git 仓库持续同步到一个本地目录。关键特性：

- **原子切换**：它 clone 到 `/git/.git/`，checkout 到 `/git/<sha>/`，然后 atomic rename symlink `/git/<DEST> → /git/<sha>/`。读端不会看到半写状态。
- **按 polling interval 轮询**：默认 60s 一次，发现 branch HEAD 变了就拉新 sha 再切 symlink。
- **支持 HTTPS / SSH / token**；public repo 不需要鉴权。
- **有 `--one-time` 模式**：拉一次就 exit，专门给 init container 用。

### 3.2 sidecar 模式（scheduler / webserver）

scheduler / webserver 是常驻 pod，需要 DAG 目录始终最新。所以用 **sidecar 模式**：

```yaml
# 伪 yaml
pod:
  containers:
    - name: scheduler
      volumeMounts:
        - name: dags           # emptyDir
          mountPath: /opt/airflow/dags
    - name: git-sync           # sidecar，长跑
      image: git-sync:v4.2.4
      env:
        - GITSYNC_REPO, GITSYNC_BRANCH, GITSYNC_PERIOD=60
        - GITSYNC_ROOT=/git
        - GITSYNC_DEST=dags    # symlink 名
      volumeMounts:
        - name: dags           # 同一个 emptyDir
          mountPath: /git       # sidecar 看到的是 /git
  volumes:
    - name: dags
      emptyDir: {}
```

两个 container 共享 `dags` 这个 emptyDir：
- sidecar 写 `/git/<sha>/...` + `/git/dags → /git/<sha>`
- scheduler 在 `/opt/airflow/dags/dags/...` 就能读到同样的内容（因为 mountPath 不同但指向同一 emptyDir）

### 3.3 init-container 模式（worker pod）

worker pod 是短命的，只跑一个 task 就消失。**不能**用 sidecar（sidecar 会永远跑下去、pod 无法优雅终止）。所以用 **init container 模式**：

```yaml
initContainers:
  - name: git-sync-init
    env:
      - GITSYNC_ONE_TIME: "true"  # 关键：拉一次就 exit 0
    volumeMounts:
      - { name: dags, mountPath: /git }
containers:
  - name: base
    volumeMounts:
      - { name: dags, mountPath: /opt/airflow/dags }  # 读 init 写下来的内容
```

init 跑完后 emptyDir 里已经有当前 HEAD 的 DAG 文件，base 启动后直接能 import。

### 3.4 subPath 和 DAGS_FOLDER

git-sync 把**整个 repo** 克隆下来。如果 DAG 只放在 repo 的子目录（比如 `airflow-dag/`），需要让 Airflow 只扫这一个子目录，不然它会尝试 import repo 里的其他 `.py` 文件、产生大量 import error。

本次设置：
- git-sync `GITSYNC_DEST=dags` → scheduler 看到 `/opt/airflow/dags/dags/` 是 repo 根
- DAG 放在 repo 的 `airflow-dag/` 子目录
- 所以 `AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags/dags/airflow-dag`（chart 根据 `values.airflow.dags.gitSync.subPath` 自动推导）

### 3.5 本次部署拓扑

```
qa-security namespace
├── airflow-db-0 (StatefulSet)          [postgres]
├── airflow-scheduler-xxx (Deployment)
│   ├── container: scheduler
│   ├── container: git-sync (sidecar)
│   └── volumeMount: /opt/airflow/pod_templates/pod_template.yaml (from ConfigMap)
├── airflow-webserver-xxx (Deployment)
│   ├── container: webserver
│   └── container: git-sync (sidecar)
└── <dag>-<task>-<uuid> (Pod, 动态创建/销毁)
    ├── initContainer: git-sync-init (one-time)
    └── container: base
```

---

## 4. 对比：自研 "MySQL-backed DAG 分发"

原始想法是把 DAG 存在 MySQL 里，本地做一个 cron 不断 pull 到 `/opt/airflow/dags/`，类似 GitOps 但自己实现。

### 4.1 如果真要做，**最少**需要实现的东西

| 领域 | 任务 |
|---|---|
| 存储层 | MySQL schema：至少 `dag_files(id, path, content, version, updated_at, deleted_at)`；索引；大 DAG 的 blob 存储或外链 S3 |
| 拉取层 | 一个 side-pod 或 container：启动时全量拉；之后 cron 或轮询 delta 拉；可选 pub-sub（MySQL binlog → 消息） |
| 原子写入 | 要保证 scheduler 扫 DAG 时不会看到半写入。方案：写到 tmp dir + `rename`，或双目录 + symlink 切换 |
| 删除语义 | MySQL 删一行 → 本地文件也要删；需要对比本地和 MySQL 的 file list、做 diff |
| 版本管理 | 谁改的、什么时候、改了啥。需要 `dag_file_history` 表 + 审计 logger。git 白送 |
| 回滚 | "回到 10 分钟前那个版本" 怎么做？需要按 version 回滚的 API + UI |
| 多 pod 一致性 | scheduler / webserver / **每个 worker pod** 都要拉到同一个 version。worker pod 是现起现销的，要保证它看到的就是 trigger 时 scheduler 看到的 version → 需要在 pod env 里 pin 一个 version id，init container 拉那个 version |
| Authoring UI | 要让人能添加/修改 DAG，得有个 CRUD UI；权限模型；diff 显示；预览 |
| 校验 | DAG 是 Python code，要执行。上传时至少要做语法检查、`ast.parse`；理想情况跑 `airflow dags reserialize` 验证可 import |
| 测试 | 并发写、网络分区、MySQL 主从切换、partial failure 恢复 …… 每种场景都要写测试 |
| 运维 | 这个组件本身的监控、日志、告警；oncall 流程 |

### 4.2 和 git-sync 对比

| 维度 | git-sync | 自研 MySQL-puller |
|---|---|---|
| 成熟度 | K8s SIG 项目，几千个 prod 部署 | 全新自研，踩坑周期从零开始 |
| 版本/审计 | git log / blame 白送 | 自己写 `dag_file_history` + API |
| 回滚 | `git revert` 一条命令 | 自己实现版本回滚逻辑 |
| PR/Review | 原生支持 | 要么绕过 review（危险），要么自己再做一个审批流 |
| 本地开发 | IDE 直接开 repo，语法检查、lint、type check 全有 | 只有"DAG 管理界面"，开发体验差 |
| 原子性 | symlink atomic rename，天生原子 | 要自己做 tmpdir rename 或双目录 |
| 多 pod 分发 | 已解决（每个 pod 各自一个 sidecar/init）| 要自己解决（且 worker pod 的 version pinning 尤其麻烦）|
| 运维成本 | 零 —— 稳定几年没重大 bug | 自己背 oncall |
| 授权控制 | 靠 git host（GitHub team、GitLab group） | 自己做 RBAC |

### 4.3 什么情况下 MySQL 方案才合理

只有一种：组织层面必须让**非工程师**通过 UI 编辑 DAG，MySQL 是天然的后端持久化。但即便这样，**推荐解耦**：

```
  ┌──────────────────────┐
  │  Authoring UI        │   非工程师点点点
  └──────────┬───────────┘
             │ 写 MySQL
             ▼
  ┌──────────────────────┐
  │  MySQL (DAG 草稿)     │
  └──────────┬───────────┘
             │ CI job（读 MySQL → 生成 *.py → git commit + PR）
             ▼
  ┌──────────────────────┐
  │  Git repo (airflow   │   工程师 review / 历史
  │    dags branch)      │
  └──────────┬───────────┘
             │ git-sync
             ▼
  ┌──────────────────────┐
  │  Airflow pods        │
  └──────────────────────┘
```

MySQL 只当 "authoring 存储"，分发层仍然走 git-sync。这样既满足 UI 需求，又不丢掉 git 的所有好处。

### 4.4 结论

**短期 / 测试阶段：直接用 git-sync**。本次部署就是这个方案。

**长期，如果产品上真的需要 UI 编辑**：考虑 authoring → MySQL → CI → git → git-sync 的混合模式。不建议让 MySQL 直接扮演分发源。

---

## 5. 参考资料

- Airflow 官方 KubernetesExecutor 文档：https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/executor/kubernetes.html
- git-sync：https://github.com/kubernetes/git-sync
- 官方 Airflow Helm Chart（可参考它的 pod_template 模板写法）：https://airflow.apache.org/docs/helm-chart/stable/
