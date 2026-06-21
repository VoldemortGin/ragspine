# RAGSpine —— Kubernetes Helm chart

把 RAGSpine 的服务层（FastAPI API + RQ worker + Redis）作为一个 **Helm chart** 部署到
Kubernetes。与 [`deploy/compose.yaml`](../compose.yaml) **同源同设计**：**一个镜像
`ragspine:local`、两种角色**（server / worker），默认 **离线精简栈**（mock provider +
sqlite_vec，无需任何 API key），真实后端经 `values.yaml` 旋钮选择性接入；配置纯走 `RAGSPINE_*`
环境变量注入到现成的 `ServiceConfig`（`src/ragspine/service/config.py`），不引入任何新配置系统。

> 设计与取舍见 [`deploy/README.md`](../README.md)。
> Compose 已把 env 契约钉死，本 chart 是**翻译**而非重设计。

## 组成

| 路径 | 作用 |
|---|---|
| `ragspine/Chart.yaml` | chart 元数据（`apiVersion v2`，`appVersion` 跟随 pyproject 版本）。 |
| `ragspine/values.yaml` | 全部旋钮，逐项注释；镜像 Compose 的设计。 |
| `ragspine/templates/` | server / worker Deployment、Service、env ConfigMap、（按需）Secret、共享数据 PVC、redis / postgres / qdrant、ingress、NOTES。 |

`server` 与 `worker` 共用同一镜像；server 角色启动命令显式带 `--host 0.0.0.0`
（`scripts/run_server.py` 默认 `127.0.0.1`，在容器内对外不可达）。

## kind 快速开始（默认离线，无需 API key）

本机没有 docker / kubectl / helm 时，下列命令在**有 kind 的远端集群**上执行。

```bash
# 1) 构建镜像（从仓库根目录；与 compose 同一个 Dockerfile / 同一个 tag）
docker build -t ragspine:local -f deploy/Dockerfile .

# 2) 把本地镜像灌进 kind 节点（否则 pullPolicy=IfNotPresent 在节点上找不到镜像）
kind load docker-image ragspine:local

# 3) 安装（离线默认：mock + sqlite_vec + 集群内 redis，无需任何 key）
helm install ragspine deploy/helm/ragspine

# 4) 端口转发 + 探活
kubectl port-forward svc/ragspine 8000:8000
curl -s localhost:8000/healthz

# 5) 问一个问题（空库时诚实返回「查不到」——反编造不变量）
curl -s localhost:8000/v1/ask -H 'content-type: application/json' \
     -d '{"question":"中国内地FY2024的REVENUE是多少"}'
```

卸载：`helm uninstall ragspine`（PVC 默认保留，需手动 `kubectl delete pvc -l app.kubernetes.io/instance=ragspine` 清数据）。

## 接 Postgres / pgvector（完整端到端的外部向量库）

```bash
helm install ragspine deploy/helm/ragspine \
  --set postgres.enabled=true \
  --set vectorStore=pgvector
```

`postgres.enabled=true` 在集群内起一个 `pgvector/pgvector:pg17`（Deployment + Service + PVC），
`RAGSPINE_PG_URL` 自动指向它；pgvector adapter 经它**真正联网**连此 Postgres——app 与 worker
共享同一外部索引。改密码用 `--set postgres.auth.password=...`（URL 会同步重算）。

## 接真实 LLM（Anthropic）

```bash
helm install ragspine deploy/helm/ragspine \
  --set llm.provider=anthropic \
  --set secrets.anthropicApiKey=sk-ant-xxxx
```

填了 `secrets.anthropicApiKey` 才会渲染一个 `Secret`，并以 `ANTHROPIC_API_KEY` env 注入两个角色，
anthropic SDK 自行读取。`secrets.openaiApiKey` 同理（`embedding=openai` 时需要）。

## Qdrant（诚实边界）

```bash
helm install ragspine deploy/helm/ragspine --set qdrant.enabled=true
```

**重要**：当前 `ServiceConfig` 的 `make_vector_store("qdrant")` 不接收服务 URL，跑的是 Qdrant
**LOCAL 进程内模式**（`:memory:`）。故 `qdrant.enabled=true` 起的 `qdrant/qdrant` 服务**当前不会
被 app 经网络连**——它为「把 Qdrant 服务 URL 接进 `ServiceConfig`」这一**后续步骤**预备（需改
`config.py`）。**今天要外部、联网、共享的向量库，请用 `postgres`（pgvector）**。

## 旋钮速查（values.yaml）

| values 键 | 注入的 env | 默认 |
|---|---|---|
| `llm.provider` / `llm.model` / `llm.baseUrl` | `RAGSPINE_PROVIDER` / `RAGSPINE_MODEL` / `RAGSPINE_BASE_URL` | `mock` / 空 / 空 |
| `vectorStore` | `RAGSPINE_VECTOR_STORE` | `sqlite_vec` |
| `embedding` | `RAGSPINE_EMBEDDING` | `none` |
| `persistencePolicy` | `RAGSPINE_PERSISTENCE_POLICY` | `default` |
| `redis.enabled` | `RAGSPINE_REDIS_URL`（指向集群内 redis） | `true` |
| `postgres.enabled` + `postgres.auth.*` | `RAGSPINE_PG_URL`（adapter 直接读，非 ServiceConfig 旋钮） | `false` |
| `secrets.anthropicApiKey` / `secrets.openaiApiKey` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`（仅填值时渲染 Secret） | 空 |
| `persistence.*` | sqlite 库 + 上传落在 `/var/lib/ragspine` 的共享 PVC | `enabled: true` |
| `ingress.*` | server Service 暴露到 host | `enabled: false` |

`RAGSPINE_DB_PATH` 等 sqlite 路径固定指向共享 PVC 的 `/var/lib/ragspine`（与 compose 一致）。
注意 `ReadWriteOnce` 时 server 与 worker 须同节点（kind 单节点满足）；多节点请用 `ReadWriteMany`
或外部后端（pgvector）。

## 关于本机无 helm 的诚实声明

本仓库所在开发机**没有 helm/kubectl/docker**，因此本 chart 经**静态校验**落地：

- `Chart.yaml` 与 `values.yaml` 用 PyYAML 解析通过。
- `templates/server-deployment.yaml` 的 server 启动命令已确认含 `--host 0.0.0.0`。
- 模板里引用的每个 `RAGSPINE_*` 都对照其真实消费者核过：旋钮属 `ServiceConfig.from_env`
  （`src/ragspine/service/config.py`）；`RAGSPINE_PG_URL` 例外，由 pgvector adapter 直接读环境。

**验证状态（远端 docker 主机 106.15.8.67 上实测）**：`helm lint` 通过（0 失败）；
`helm template` 在**离线默认**与 **`--set postgres.enabled=true --set vectorStore=pgvector`** 两种取值下
均正确渲染（kinds / `--host 0.0.0.0` / `/healthz` 探针 / `RAGSPINE_*` 注入 / `RAGSPINE_PG_URL` 接线均核对无误）。
活体 `helm install` **尚未跑通**：该 box 的 kind 控制面起不来（kubeadm 在 wait-control-plane 超时，
kubelet↔apiserver 出现 EOF——属该机器的 k8s 宿主基础设施问题，与本 chart 无关）。chart 的应用行为
已由**等价的 Docker Compose 栈在同机端到端实测**覆盖（构建 + `/healthz` + 反捏造拒答 + pgvector profile
的隔离不变量，见 [`deploy/README.md`](../README.md)）。换一台 kind 控制面健康的机器即可直接 `helm install`。
