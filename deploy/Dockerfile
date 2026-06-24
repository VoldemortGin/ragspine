# syntax=docker/dockerfile:1
#
# RAGSpine 部署镜像（落地 deploy/README.md）。
#
# 多阶段、uv 驱动：builder 在隔离层里把 ragspine[service,vector] 装进 /opt/venv，runtime
# 只拷该 venv + 脚本运行所需的运行时文件，得到一个精简、不可变的镜像。
#
# 【一个镜像，两种角色】：server 与 worker 共用此镜像，角色仅由启动 command 决定——
#   server: python scripts/run_server.py --host 0.0.0.0 --port 8000
#   worker: python scripts/run_worker.py
# 见 deploy/compose.yaml。注意 run_server.py 默认 --host 127.0.0.1 在容器内【不可达】，
# 故 server command 必须显式传 --host 0.0.0.0。
#
# 构建上下文为【仓库根】（deploy/ 的上一级），故下面的 COPY 路径都相对仓库根。

# ---------------------------------------------------------------------------
# builder：用官方 uv 二进制把项目（含 [service,vector,dify] extra）装进独立 venv。
# builder 与 runtime 同一基础镜像（python:3.12-slim-bookworm），保证 /opt/venv 里的
# 解释器符号链接（/usr/local/bin/python3.12）在 runtime 阶段依然存在、venv 可直接用。
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# 从官方 uv 镜像取静态二进制（无需 pip 装 uv）。
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# 构建 wheel 所需的最小集合：pyproject（含 readme=README.md、SPDX license）+ 包源码。
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src/ ./src/

# 独立 venv + 非可编辑安装（不可变镜像，源码已烘进 wheel）。
RUN uv venv /opt/venv --python /usr/local/bin/python3.12 \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache ".[service,vector,dify]"

# ---------------------------------------------------------------------------
# runtime：拷 venv + 脚本运行所需的运行时文件，配置全走 RAGSPINE_* 环境变量。
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # ---- 离线精简栈默认（无需任何 API key；compose 可经 ${VAR} 覆盖）----
    RAGSPINE_PROVIDER=mock \
    RAGSPINE_VECTOR_STORE=sqlite_vec \
    RAGSPINE_EMBEDDING=none \
    RAGSPINE_PERSISTENCE_POLICY=default \
    RAGSPINE_REDIS_URL=redis://redis:6379/0 \
    # ---- Dify 工作流执行（默认安全：/run 关闭；compose 可经 ${VAR} 显式开启）----
    # 公网暴露时默认无鉴权——开启 /run 等于把受限代码执行能力对外开放，务必先加鉴权（见 deploy/README.md）。
    RAGSPINE_DIFY_RUN_ENABLED=false \
    RAGSPINE_DIFY_RUN_TIMEOUT_S=10 \
    RAGSPINE_DIFY_RUN_ISOLATION=subprocess \
    # ---- 持久化路径：落在命名卷 /var/lib/ragspine（compose 挂载）----
    RAGSPINE_DB_PATH=/var/lib/ragspine/fact_metric.db \
    RAGSPINE_CHUNK_DB_PATH=/var/lib/ragspine/narrative.db \
    RAGSPINE_MAPPING_DB_PATH=/var/lib/ragspine/mapping.db \
    RAGSPINE_QUEUE_DB_PATH=/var/lib/ragspine/review_queue.db \
    RAGSPINE_MANIFEST_DB_PATH=/var/lib/ragspine/manifest.db \
    RAGSPINE_ALLOWED_UPLOAD_ROOT=/var/lib/ragspine/uploads

# WORKDIR 必须是含 .project-root 的目录：rootutils.setup_root 以此为根并加入 sys.path。
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# 脚本运行时所需文件（.project-root 定位根；scripts/ 入口；src/ 源码；config/ 公司画像；
# data/ 版本化的 golden 评测/演示数据）。ragspine 本体已装进 venv，import 走 site-packages。
COPY .project-root ./.project-root
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY config/ ./config/
COPY data/ ./data/

# 命名卷挂载点（sqlite 库 + 上传目录）；先建目录 + 改属主，命名卷首次创建即继承该属主。
RUN mkdir -p /var/lib/ragspine/uploads \
 && useradd --create-home --uid 10001 ragspine \
 && chown -R ragspine:ragspine /app /var/lib/ragspine

USER ragspine
VOLUME ["/var/lib/ragspine"]
EXPOSE 8000

# 默认角色＝server（worker 由 compose 覆盖 command）。--host 0.0.0.0：容器内必须对外可达。
CMD ["python", "scripts/run_server.py", "--host", "0.0.0.0", "--port", "8000"]
