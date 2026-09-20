# 本地 embedding 与 rerank 服务

本项目通过两个独立的 OpenAI-compatible HTTP 适配器调用 embedding 与 rerank。两者各自要求 base URL、模型名和 API key，不继承 `OPENAI_*` 配置。HTTP base URL 只能是 `127.0.0.1`、`localhost` 或 `::1`；远端 GPU 服务必须先经项目管理的 SSH loopback tunnel 暴露到本机。

## 建立与管理隧道

隧道配置全部来自进程环境。公开代码和文档不记录实际主机名、IP 或账户。下面的值只是占位示例：

```sh
export LOCAL_MODELS_SSH_HOST='user@gpu-host.example'
export LOCAL_MODELS_SSH_PORT='6000'
export EMBEDDING_LOCAL_PORT='39002'
export EMBEDDING_REMOTE_PORT='28002'
export RERANK_LOCAL_PORT='39001'
export RERANK_REMOTE_PORT='28001'

uv run --locked python scripts/enterprise_pdf_rag/local_model_tunnel.py start
uv run --locked python scripts/enterprise_pdf_rag/local_model_tunnel.py status
```

SSH 以参数数组启动，不经 shell 展开；它启用 `BatchMode`、`ExitOnForwardFailure` 和 keepalive，并把两条转发都绑定在 `127.0.0.1`。脚本不会读取或传送模型 API key。PID 与日志写入已被 Git 忽略的 `data/local-models/`，其中也不保存 key。停止时会核对 PID 的 SSH 命令、两个端口映射与目标主机，只终止仍匹配本项目配置的进程：

```sh
uv run --locked python scripts/enterprise_pdf_rag/local_model_tunnel.py stop
```

## 进程配置

调用进程必须取得以下六个变量。API key 应由操作系统凭据存储或等效的受控启动器直接注入子进程环境；不要写入 `.env`、shell history、日志或可提交文件。

```text
EMBEDDING_BASE_URL=http://127.0.0.1:39002
EMBEDDING_MODEL=<served embedding model name>
EMBEDDING_API_KEY=<injected secret>

RERANK_BASE_URL=http://127.0.0.1:39001
RERANK_MODEL=<served rerank model name>
RERANK_API_KEY=<injected secret>
```

Python 入口是：

```python
from enterprise_pdf_rag.adapters.local_models import (
    LocalEmbeddingAdapter,
    LocalRerankAdapter,
)
from enterprise_pdf_rag.adapters.providers import load_local_model_config

embedder = LocalEmbeddingAdapter(load_local_model_config("embedding"))
reranker = LocalRerankAdapter(load_local_model_config("rerank"))
```

`LocalEmbeddingAdapter` 实现 `figures.EmbeddingPort`，只接受自然语言 description 或 query，返回不可变 `tuple[float, ...]`。ChartIR、SVG 和 source sidecar 不能传给它。`LocalRerankAdapter.rerank()` 接受 query、候选文本元组与明确的 `limit`，只返回候选位置和分数；即使 provider 回传文档正文，适配器也不会把正文保留在结果对象中。

已授权通过 SSH 读取远端 vLLM 容器 key 的部署，可用受控 launcher。容器名与模型名不是秘密，但仍由运行环境提供，不硬编码在仓库：

```sh
export EMBEDDING_MODEL='<served embedding model name>'
export RERANK_MODEL='<served rerank model name>'
export EMBEDDING_REMOTE_CONTAINER='<embedding container name>'
export RERANK_REMOTE_CONTAINER='<rerank container name>'

uv run --locked python scripts/enterprise_pdf_rag/with_local_models.py -- \
  uv run --locked python scripts/enterprise_pdf_rag/local_model_smoke.py
```

launcher 先确认本项目 PID 记录中的隧道仍在运行，再通过捕获的 SSH stdout 只读取得两个容器的 `VLLM_API_KEY`，随即把它们注入被 `exec` 替换后的单一子进程环境。key 不进入参数、终端输出、日志、PID 文件或磁盘。子进程环境按白名单重建，不继承无关的云凭据；需要云端图表模型的命令可显式传入现有 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和本次选择的 `OPENAI_MODEL`，但这些变量同样不得写入仓库。

## 显式连通性探针

六个模型变量已安全注入当前子进程时，可运行一次合成探针：

```sh
uv run --locked python scripts/enterprise_pdf_rag/local_model_smoke.py
```

它各发一个短请求，只输出 embedding fingerprint、向量维度和 rerank 候选索引，不输出 key、向量、provider body 或候选正文。该命令不是默认测试或 CI 的一部分，不会重试，也不能证明模型对财报语义的质量。默认 `bash scripts/ci.sh` 始终离线，使用 transport fake 验证请求与严格响应边界。

云端图表理解使用独立的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 与 `OPENAI_MODEL`。需要时只给对应受控子进程显式设置；不得把云端 key 或本地模型 key 互相复用或写入全局 shell 配置。
