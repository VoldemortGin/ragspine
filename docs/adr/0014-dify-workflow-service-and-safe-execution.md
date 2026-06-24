---
status: proposed
date: 2026-06-24
---

# ADR 0014 — Dify 工作流服务化（analyze / compile / run）与安全执行三层

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Builds on [0013](0013-dify-workflow-compiler.md)（Dify YAML → 纯 Python 编译器 + 静态优化建议器）。
Relates to [0009](0009-dependency-and-framework-policy.md)（无框架锁定 + permissive-only 许可门）、
[0010](0010-intent-parser-security-decoupling.md)（安全解耦）、family
[0001](../../../docs/adr/0001-spine-family-boundaries-and-dependency-direction.md)（家族边界 / LLM 缝）。

## Context

ADR 0013 把 Dify `.yml` 编译成可读纯 Python（`compile_dify_yaml` / `analyze`）。下一步是把这条能力
**服务化**，让消费者经 HTTP 用上：拿优化建议、拿可读代码、乃至**直接运行**编译产物。运行别人提交的
代码是**信任边界**问题——必须在「开箱可用」与「不轻率 exec」之间取一条安全默认。

约束：复用既有 ragspine service（app factory / DI / RQ 队列 / deploy），不另起服务；零真实 LLM API
跑测试（TestClient + MockProvider）；provider 由**服务端** env 决定（客户端不可注入 provider_expr，
防代码注入）；family 全同步；mypy `--strict`、`filterwarnings=error`、pydantic 仅在边界。

## Decision

**扩 ragspine service，三个端点信任级别递增；run 默认关、经安全三层执行。**

### 三端点（`src/ragspine/service/api/routes.py`，dify 编译器延迟 import）

- `POST /v1/dify/analyze` — 只跑静态优化分析（零代码生成、零 exec）。**始终可用**，绝对安全。
- `POST /v1/dify/compile` — 编译成纯 Python**代码字符串**（不执行）。**始终可用**，安全。
- `POST /v1/dify/run`、`POST /v1/dify/run/jobs`（异步） — 编译 + **受限执行**。**信任边界**：默认关
  （`RAGSPINE_DIFY_RUN_ENABLED=false` → 403），env 显式开启才放行；provider 由服务端注入。

错误整形：`DifyCompileError`（code `dify.*`）→ 400；L0 静态闸 `DifyUnsafeError`（未执行）→ 422；
执行/超时 `DifyRunError`/`DifyTimeoutError` → 400；未开启 → 403。异步复用既有 `GET /v1/jobs/{id}`。

### 安全三层（`src/ragspine/service/dify/{safety,runner}.py` + `scripts/run_dify_workflow.py`）

- **L0 静态闸（不可绕过，执行前）**：① `GeneratedCode.warnings` 非空（含 `NotImplementedError`
  骨架 / 不支持节点）→ 拒（422）；② AST 遍历生成源码 import，顶层根模块不在白名单（仅
  `__future__`/`dataclasses`/`typing`/`string`/`concurrent`/`json`/`corespine`/`ragspine`/`spineagent`）
  → 拒。白名单刻意**不含**任何 I/O / 进程 / 网络模块（os/sys/subprocess/socket/shutil/pathlib/importlib）。
- **L1 受限 in-process 沙箱**：一份明确的**安全 builtins 白名单 dict**（纯计算 / 容器 / 序列化 +
  生成代码会用到的异常类型）+ class/module body 执行必需 dunder（`__build_class__`）+ **受限
  `__import__`**（只放 L0 白名单根模块，连动态 `__import__('os')` 也挡掉）；`open`/`eval`/`exec`/
  `compile`/`input` 等逃逸面一律不入白名单。生成模块注册进 `sys.modules`（`from __future__ import
  annotations` 下 dataclass 解析字符串注解需要）。**线程软超时**（跨平台，不依赖 SIGALRM）。
- **L2 子进程隔离**：`isolation='subprocess'` → Linux 起独立子进程（`Popen(cwd=私有 tmp)` +
  `communicate(timeout)` + `kill()`=SIGKILL 硬杀失控进程 + `resource.setrlimit` 封顶 CPU 时间 /
  地址空间 / 禁新建文件）；子进程内仍叠加 L1 受限沙箱。**非 Linux（macOS/Windows）自动回落 L1**
  （这些平台 rlimit 不可靠，硬隔离价值有限）。

### provider 安全 & 隔离进程/worker 自建

客户端**不可**传 `provider_expr`（schema 无此字段，多余字段被 pydantic 忽略），生成代码内 provider
固定离线默认、运行期由服务端 `run_workflow(provider=...)` 覆盖。子进程 / RQ worker 用
`build_provider(provider_config_dict(config))` **自建** provider，绝不传 provider 实例 /
provider_expr——provider 始终由服务端 env 决定。

落点：`src/ragspine/service/dify/{safety,runner}.py`、`service/tasks/jobs.py:run_dify_workflow_job`、
`scripts/run_dify_workflow.py`、`service/api/{routes,schemas}.py`、`service/config.py`
（`dify_run_enabled`/`dify_run_timeout_s`/`dify_run_isolation` + `provider_config_dict`）。

## §安全默认（本轮全接受，待 review 收敛）

1. **安全默认全接受**：L0 静态闸 + L1 受限沙箱 + L2 子进程隔离三层全落地；run 默认关、默认离线 mock。
2. **MVP 无鉴权**：HTTP 层不做鉴权（与既有 `/v1/ask` 一致）。公网暴露务必先在前置反代 / ingress 加
   鉴权或网络白名单——已在 deploy（README / .env.example / compose / helm values）显著标注。
3. **沙箱完备性诚实边界**：L1 受限 builtins 是**纵深防御的一层**，不是完备沙箱；硬隔离靠 L2 子进程
   + SIGKILL +（Linux）rlimit。非 Linux 回落 L1，硬资源上限不生效——已在代码与文档明示。

## Alternatives considered (rejected)

- **/run 默认开**：拒。运行别人提交的编译产物是信任边界，安全默认必须是关；开箱即用由 analyze/compile
  覆盖（二者绝对安全）。
- **黑名单式受限 builtins（剔除危险名）**：拒。易漏（如最初漏掉 `__build_class__` 导致 class 定义 exec
  失败）。改用**明确的白名单 dict**——只放确知安全的名字，默认拒绝一切未列入项。
- **L1 用 `signal.SIGALRM` 超时**：拒。Unix-only，且与线程不兼容。改线程软超时（跨平台）+ L2 子进程
  SIGKILL 硬超时。
- **L1 `os.chdir(tmp)`**：拒（实现中踩坑后移除）。`os.chdir` 是进程级副作用，线程软超时杀不掉失控
  线程，超时线程会把**整进程** cwd 留在已删 tmp、污染后续子进程 spawn；且受限沙箱里 open/os/pathlib
  本不可达，chdir 零收益却有进程级风险。「在 tmp 里跑」语义改由 L2 子进程 `Popen(cwd=私有 tmp)` 承担。
- **客户端传 provider_expr**：拒。那是直接的代码注入面。provider 由服务端 env 决定，隔离进程/worker
  用 build_provider 自建。

## Consequences

- service 多三个端点（analyze/compile 安全常开；run 信任边界、默认关）；复用 app factory / DI / RQ
  队列 / deploy，无新服务、无新配置系统。
- `[service]` extra 加 PyYAML（dify 端点延迟 import 编译器所需；pydantic 由 fastapi 带入），核心
  `dependencies` 不动。部署镜像装 `.[service,vector,dify]`。
- 受限执行始终经 L0 + L1（+ Linux L2）；provider 永由服务端决定，客户端不可注入。
- 本 ADR 为 **Proposed**：安全默认（无鉴权 MVP / 沙箱完备性边界）待 review；任何改动按
  supersede-don't-edit 另立 / 改状态。
