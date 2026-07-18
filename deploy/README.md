# RAGSpine —— Docker Compose 一键自托管

把 RAGSpine 的服务层（FastAPI API + RQ worker + Redis）打成**一条命令就能起的离线栈**。
真实后端（Postgres/pgvector、Qdrant）与真实 LLM 经 **Compose profiles** 与 `RAGSPINE_*`
环境变量【选择性接入】——不改一行代码、不引入任何新配置系统，纯注入到现成的
`ServiceConfig`（`src/ragspine/service/config.py`）。

> 本文件即部署层的设计与取舍说明（原始 PRD 已退役，历史见 git）。

## 组成

| 文件 | 作用 |
|---|---|
| `deploy/Dockerfile` | 多阶段、uv 驱动；装 `ragspine[service,vector]` ＋ 内置 Studio Web UI 静态产物。**一个镜像两种角色**，server/worker 仅 command 不同。 |
| `deploy/compose.yaml` | `app`（server）+ `worker` + `redis`，默认离线；`postgres` / `qdrant` 两个 profile 选择性接入。 |
| `deploy/compose.prod.yaml` | 生产覆盖层：禁用本地 build，强制使用带版本号的 GHCR 镜像。 |
| `deploy/.env.example` | 所有 `RAGSPINE_*` 变量 + LLM key 占位符，离线安全默认（拷为 `deploy/.env` 用）。 |
| `deploy/.dockerignore` | 裁剪构建上下文（排除 `tests/` / venv / 缓存 / `.git` / `*.egg-info`）。 |

`app` 与 `worker` 共用同一个镜像（`ragspine:local`）；server 角色启动命令显式带
`--host 0.0.0.0`（`scripts/run_server.py` 默认 `127.0.0.1`，在容器内对外不可达）。

## 快速开始（默认离线，无需 API key）

从**仓库根目录**运行：

```bash
docker compose -f deploy/compose.yaml up --build
```

起来后：

```bash
# 健康检查
curl -s localhost:8000/healthz

# 问一个问题（离线确定性 MockProvider；空库时诚实返回「查不到」——反编造不变量）
curl -s localhost:8000/v1/ask -H 'content-type: application/json' \
     -d '{"question":"中国内地FY2024的REVENUE是多少"}'

# Dify 工作流服务化（ADR 0014）。analyze/compile 始终可用、绝对安全（不执行）；
# run 是信任边界，默认关（RAGSPINE_DIFY_RUN_ENABLED=true 才放行），经安全三层执行。
Y='{"yaml":"app:\n  mode: workflow\nkind: app\nversion: \"0.1.5\"\nworkflow:\n  graph:\n    nodes:\n      - {id: s, data: {type: start, variables: []}}\n      - {id: e, data: {type: end, outputs: []}}\n    edges:\n      - {source: s, target: e}"}'
curl -s localhost:8000/v1/dify/analyze -H 'content-type: application/json' -d "$Y"   # 优化建议
curl -s localhost:8000/v1/dify/compile -H 'content-type: application/json' -d "$Y"   # 纯 Python 代码字符串
# run 需先开启（默认 403）：RAGSPINE_DIFY_RUN_ENABLED=true 重启后
curl -s localhost:8000/v1/dify/run -H 'content-type: application/json' \
     -d '{"yaml":"...","inputs":{"question":"你好"}}'
```

> ⚠️ **开启 `/v1/dify/run` = 对外开放「受限代码执行」能力。** MVP 无鉴权（与 `/v1/ask` 一致）。
> 公网暴露前务必在前置反代 / ingress 加鉴权或网络白名单。执行经三层安全（L0 静态闸 + L1 受限
> builtins 沙箱 + L2 子进程隔离 + SIGKILL 超时 +(Linux)setrlimit），但安全默认仍是**关闭**。

默认栈：`RAGSPINE_PROVIDER=mock` + `RAGSPINE_VECTOR_STORE=sqlite_vec` +
`RAGSPINE_EMBEDDING=none`（纯 BM25）。sqlite 库与上传文件持久化在命名卷 `ragspine-data`
（容器内 `/var/lib/ragspine`）。要灌入演示/真实数据，可在容器内跑 ingestion 脚本或经
`POST /v1/ingest/...` 端点（产物落在同一卷上，重启不丢）。

停止 / 清理：

```bash
docker compose -f deploy/compose.yaml down            # 停服务，留数据卷
docker compose -f deploy/compose.yaml down -v         # 连命名卷一起删（清空持久化数据）
```

## Studio Web UI

镜像自带 Studio 前端（`studio/` 的 Vite 构建产物，Dockerfile 内置
`RAGSPINE_STUDIO_DIR=/opt/ragspine/studio`）。`up` 之后浏览器访问
`http://localhost:8000/studio/` 即可；把 `RAGSPINE_STUDIO_DIR` 设为空串可禁用挂载。
本地前端开发不必进容器：`make studio-dev` 起 Vite dev server（API 请求经 Vite proxy
转发到 :8000 的本地 server）。

## 接真实 LLM（Anthropic）

```bash
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env：取消注释并填
#   RAGSPINE_PROVIDER=anthropic
#   ANTHROPIC_API_KEY=sk-ant-...
docker compose -f deploy/compose.yaml --env-file deploy/.env up --build
```

`ANTHROPIC_API_KEY` 经 compose 的 `env_file` 注入容器，anthropic SDK 自行读取；`--env-file`
让 `deploy/.env` 同时参与 `RAGSPINE_PROVIDER` 等旋钮的 `${VAR}` 插值（见下「环境变量优先级」）。

## Profile：Postgres / pgvector（完整端到端的外部向量库）

```bash
cp deploy/.env.example deploy/.env     # 取消注释 RAGSPINE_VECTOR_STORE=pgvector
docker compose -f deploy/compose.yaml --env-file deploy/.env --profile postgres up --build
```

`--profile postgres` 起一个 `pgvector/pgvector:pg17` 服务；`RAGSPINE_PG_URL` 已默认指向它，
故只需把 `RAGSPINE_VECTOR_STORE` 设为 `pgvector`。pgvector adapter 经 `RAGSPINE_PG_URL`
**真正联网**连此 Postgres——完整持久、app 与 worker 共享同一外部索引。改密码时同步
`RAGSPINE_PG_PASSWORD` 与 `RAGSPINE_PG_URL` 里的密码。

## Profile：Qdrant

```bash
cp deploy/.env.example deploy/.env     # 取消注释 RAGSPINE_VECTOR_STORE=qdrant
docker compose -f deploy/compose.yaml --env-file deploy/.env --profile qdrant up --build
```

**诚实边界（重要）**：当前 `ServiceConfig` 的 `make_vector_store("qdrant")` 不接收服务 URL
（`config.py` 调用时不传 kwargs），跑的是 Qdrant **LOCAL 进程内模式**（`:memory:`）。也就是说
`--profile qdrant` 起的 `qdrant/qdrant` 服务**当前不会被 app 经网络连**——它为「把 Qdrant 服务
URL 接进 ServiceConfig」这一**后续步骤**预备（属本次部署层的 ADDITIVE 范围之外，需改 `config.py`）。
**今天要外部、联网、共享的向量库，请用 `--profile postgres`（pgvector）**。`sqlite_vec` 默认值
同理跑进程内 `:memory:`（在 `EMBEDDING=none` 默认下无语义向量、纯 BM25，故无副作用）。

## 环境变量优先级（避免踩坑）

- **config 旋钮**（`RAGSPINE_PROVIDER` / `RAGSPINE_VECTOR_STORE` / `RAGSPINE_EMBEDDING` /
  `RAGSPINE_PERSISTENCE_POLICY` / …）走 compose `environment` 的 `${VAR:-默认}` 插值。
  `RAGSPINE_PG_URL` 与 `RAGSPINE_PG_PASSWORD` 用【同样的】`${VAR}` 插值机制，但它们**不是
  ServiceConfig 旋钮**：前者由 pgvector adapter 直接读环境，后者只喂内置 postgres 容器密码（见下
  「诚实声明」）。要让 `deploy/.env` 参与插值，须加 `--env-file deploy/.env`（或直接用
  shell 环境变量，如 `RAGSPINE_VECTOR_STORE=pgvector docker compose ...`）。**只把旋钮写进
  `deploy/.env` 却忘了 `--env-file`** 时，插值会回落默认值——这是唯一的坑，故非默认配置一律带
  `--env-file deploy/.env`。
- **机密**（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`）走 compose `env_file` 直接注入容器，这些键
  【不】出现在 `environment`，故无优先级冲突，加不加 `--env-file` 都注入。

完整变量表见 `deploy/.env.example`（每个 `RAGSPINE_*` 都有说明与离线安全默认）。

## 关于本机无 docker 的诚实声明

本仓库所在开发机**没有 docker**，因此 `deploy/` 全部经**静态校验**落地：

- `compose.yaml` 用 PyYAML 解析通过（`python -c "import yaml; yaml.safe_load(open('deploy/compose.yaml'))"`）。
- 注入的 `RAGSPINE_*` 变量都对照其真实消费者核过：16 个旋钮对照 `ServiceConfig.from_env`
  （`src/ragspine/service/config.py`），确属现有 env 表面；两个**按设计**不进 ServiceConfig 的例外
  也各有归属——`RAGSPINE_PG_URL` 由 pgvector adapter 直接 `os.environ` 读取
  （`src/ragspine/retrieval/vector/adapters/pgvector.py`），`RAGSPINE_PG_PASSWORD` 是 compose-only
  插值变量（仅喂内置 postgres 容器的 `POSTGRES_PASSWORD`）。无悬空/伪旋钮。
- server 启动命令已确认含 `--host 0.0.0.0`。

**真实的 `docker compose up` boot-test 在远端 docker host 上单独完成**，不在本机进行。镜像由明确的
`COPY`（`src/` `scripts/` `config/` `data/`）拼装，绝不 `COPY . .`，故无论 `.dockerignore` 是否
被某次构建上下文采用，测试与缓存都不会进镜像。

## Kubernetes：Helm chart（已落地）

Kubernetes 的 Helm chart 已落地，见 [`deploy/helm/ragspine/`](helm/ragspine/)（快速开始：
[`deploy/helm/README.md`](helm/README.md)）。它复用**同一个镜像**（`ragspine:local`）与**同一套
`RAGSPINE_*` 注入**：Compose 的 `app`/`worker`/`redis` 映射为 Deployment，命名卷映射为共享数据
PVC（`/var/lib/ragspine`），`postgres`/`qdrant` profile 映射为 `values.yaml` 旋钮，`env_file`
机密映射为按需渲染的 `Secret`。默认 `helm install` 即离线精简栈（mock + sqlite_vec，无需 key）。

## 生产镜像：GHCR（自动 build / push）

`ragspine:local` 是**本地构建**的 tag——compose 的 `build:` 与 helm `values.yaml` 的
`image.repository: ragspine / tag: local` 默认走它，适合开发 / kind 验证。**生产**则从
`ghcr.io/voldemortgin/ragspine` 拉一个不可变镜像，由
[`.github/workflows/image-publish.yml`](../.github/workflows/image-publish.yml) 在**发布
GitHub Release** 时用**同一个 `deploy/Dockerfile`** 自动 build + push：

- tag：版本号（取自 `pyproject.toml`，如 `0.3.0` 与 `v0.3.0`）＋ `latest`（仅正式 Release，
  prerelease 不动 latest）。与 PyPI 的 `release.yml` 同源、同版本号约定。
- 登录用仓库内置 `GITHUB_TOKEN`（`packages: write`），**无需 PAT**；首次推送自动建 package，
  之后在 GitHub → Packages 把可见性设为 public（生产免登录 `docker pull`）或留 private。
- 也可在 Actions 里手动 `Run workflow`（`workflow_dispatch`）出一个按 pyproject 版本号打 tag
  的镜像（不动 `latest`），用于验证。

生产环境改用 GHCR 镜像（不本地 build）：

```bash
# compose：生产覆盖层要求显式版本号，禁止 latest 漂移
RAGSPINE_TAG=0.11.0 docker compose \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d

# helm：把 chart 的镜像指到 GHCR
helm install ragspine deploy/helm/ragspine \
  --set image.repository=ghcr.io/voldemortgin/ragspine \
  --set image.tag=0.3.0 \
  --set image.pullPolicy=IfNotPresent
```

`compose.prod.yaml` 只覆盖 `app`/`worker` 的构建来源，其余健康检查、持久卷、Redis 与安全默认
全部继承开发 Compose。`RAGSPINE_TAG` 未设置时配置阶段即失败，避免生产环境意外追随 `latest`。
