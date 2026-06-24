"""受限执行 runner：把通过 L0 静态闸的 GeneratedCode 跑起来，纵深防御。

执行前先过 L0 静态闸（safety.assert_runnable）。之后在尽量收窄的命名空间里 exec：

- 受限 __builtins__：一份明确的【白名单 dict】（SAFE_BUILTINS）——只放纯计算 / 序列化用
  builtin，绝不放 open / eval / exec / compile / input / __import__(裸) 等。
  ⚠️ class / module body 执行需要 dunder：`@dataclass class Inputs` 要 `__build_class__`，
  `from __future__ import annotations` 下 dataclass 解析字符串注解要把生成模块注册进
  sys.modules（否则 dataclasses._is_type 取 sys.modules[__name__].__dict__ 触 AttributeError）。
  故白名单含 `__build_class__`，并提供【受限 __import__】（只放 L0 白名单根模块，连动态
  import('os') 也挡掉）。
- 超时：在工作线程跑 exec+调用，主线程 join(timeout)；超时抛 DifyTimeoutError（跨平台，
  不依赖 SIGALRM）。L1 线程软超时无法强杀失控线程——硬隔离由 L2 子进程（S6）负责。
- :memory: 库：生成代码的 KNOWLEDGE_CHUNK_DB / ANSWER_QUESTION_FACT_DB 模块常量默认即
  ":memory:"，无文件副作用。
- chdir(tmp)：在临时空目录里执行，任何相对路径文件写都落在用完即弃的 tmp（兜底）。

provider 由服务端注入（run_workflow(provider=...)），客户端不可控（防注入）。
"""

from __future__ import annotations

import builtins
import os
import sys
import tempfile
import types
from dataclasses import fields, is_dataclass
from pathlib import Path
from threading import Thread
from typing import Any, cast

from corespine import CorespineError, LLMProvider

from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.service.dify.safety import ALLOWED_IMPORT_ROOTS, assert_runnable

# 默认执行超时（秒）。ServiceConfig.dify_run_timeout_s 可覆盖。
DEFAULT_TIMEOUT_S: float = 10.0

# 受限 builtins 白名单：只放纯计算 / 容器 / 序列化与生成代码确实会用到的异常类型。
# 刻意【不含】open / eval / exec / compile / input / globals / locals / vars / help /
# breakpoint / 裸 __import__ —— 这些是越权 / 逃逸面。
_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    # 数值 / 字符串 / 容器
    "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "getattr", "hasattr",
    "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list", "map",
    "max", "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed",
    "round", "set", "setattr", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    # 面向对象 / 装饰器支撑（dataclass 等）
    "object", "super", "property", "staticmethod", "classmethod",
    # 生成代码 / 折叠链可能触达的异常类型
    "Exception", "BaseException", "TypeError", "ValueError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration", "ArithmeticError",
    "ZeroDivisionError", "LookupError", "NotImplementedError",
)


class DifyRunError(CorespineError):
    """工作流执行期错误（exec 抛出 / 入口缺失等）。code "dify.run_error"，HTTP 整形 400。"""

    code = "dify.run_error"


class DifyTimeoutError(CorespineError):
    """工作流执行超时。code "dify.timeout"，HTTP 整形 400（504 语义亦可，统一走 400 族）。"""

    code = "dify.timeout"


def _guarded_import(
    name: str,
    globals: Any = None,
    locals: Any = None,
    fromlist: Any = (),
    level: int = 0,
) -> types.ModuleType:
    """受限 __import__：只放 L0 白名单根模块，连动态 import('os') 也挡掉。

    签名刻意宽松（globals/locals/fromlist 用 Any）：解释器以 CPython __import__ 的实际
    调用约定回调本钩子（fromlist 可能为 None / list / tuple），不应被静态/运行期类型契约误伤。
    """
    root = name.split(".", 1)[0]
    if level != 0 or root not in ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"dify runner 禁止 import：{name!r}")
    return builtins.__import__(name, globals, locals, fromlist or (), level)


def _build_safe_builtins() -> dict[str, Any]:
    """组装受限 __builtins__ 白名单 dict（含 class/module body 必需 dunder + 受限 __import__）。"""
    safe: dict[str, Any] = {
        n: getattr(builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(builtins, n)
    }
    # class 语句 / module body 执行所需 dunder（见模块 docstring 的坑说明）。
    safe["__build_class__"] = builtins.__build_class__
    safe["__import__"] = _guarded_import
    return safe


def _make_inputs(inputs_cls: Any, inputs: dict[str, Any]) -> Any:
    """用客户端传入的 dict 构造生成模块的 Inputs dataclass（只取已声明字段，多余忽略）。"""
    if not (isinstance(inputs_cls, type) and is_dataclass(inputs_cls)):
        raise DifyRunError("生成模块缺少合法的 Inputs dataclass")
    factory: Any = inputs_cls  # 已确认是 dataclass 类型，按 Any 调用（构造实例）
    declared = {f.name for f in fields(inputs_cls)}
    kwargs = {k: v for k, v in inputs.items() if k in declared}
    return factory(**kwargs)


def _exec_in_sandbox(
    code: GeneratedCode,
    inputs: dict[str, Any],
    provider: LLMProvider,
    workdir: Path,
) -> dict[str, Any]:
    """在受限命名空间 + chdir(tmp) 里 exec 生成模块并调用 run_workflow。返回 _result dict。"""
    module_name = "__ragspine_dify_workflow__"
    module = types.ModuleType(module_name)
    module.__dict__["__builtins__"] = _build_safe_builtins()

    prev_cwd = Path.cwd()
    # 同名残留（理论上不会有）先清掉，避免污染；执行后恢复。
    saved = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        os.chdir(workdir)
        compiled = compile(code.source, "<dify-workflow>", "exec")
        exec(compiled, module.__dict__)  # noqa: S102 — 受限 builtins + L0 闸 + 隔离
        run_workflow = module.__dict__.get("run_workflow")
        inputs_cls = module.__dict__.get("Inputs")
        if not callable(run_workflow) or inputs_cls is None:
            raise DifyRunError("生成模块缺少 run_workflow / Inputs 入口")
        result = run_workflow(_make_inputs(inputs_cls, inputs), provider=provider)
        return cast("dict[str, Any]", result)
    finally:
        os.chdir(prev_cwd)
        if saved is not None:
            sys.modules[module_name] = saved
        else:
            sys.modules.pop(module_name, None)


def run_generated(
    code: GeneratedCode,
    inputs: dict[str, Any],
    provider: LLMProvider,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """L1 受限 in-process 执行：L0 闸 -> 受限 builtins + chdir(tmp) exec -> 线程软超时。

    先过 L0 静态闸（safety.assert_runnable），不过即 DifyUnsafeError（调用方整形 422）。
    在工作线程跑 _exec_in_sandbox，主线程 join(timeout_s)；超时抛 DifyTimeoutError。
    线程软超时不强杀失控线程（CPython 限制）——硬隔离由 L2 子进程隔离负责（S6）。
    """
    assert_runnable(code)  # L0：不过即抛 DifyUnsafeError（在任何 exec 之前）

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _worker() -> None:
        with tempfile.TemporaryDirectory(prefix="dify-wf-") as tmp:
            try:
                result.update(_exec_in_sandbox(code, inputs, provider, Path(tmp)))
            except BaseException as exc:  # noqa: BLE001 — 跨线程回传，主线程统一整形
                error.append(exc)

    thread = Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise DifyTimeoutError(
            f"工作流执行超过 {timeout_s}s 超时上限", timeout_s=timeout_s
        )
    if error:
        exc = error[0]
        if isinstance(exc, CorespineError):
            raise exc
        raise DifyRunError(f"工作流执行失败：{type(exc).__name__}: {exc}") from exc
    return result


def run_workflow_isolated(
    code: GeneratedCode,
    inputs: dict[str, Any],
    provider: LLMProvider,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    isolation: str = "inprocess",
) -> dict[str, Any]:
    """按隔离级别执行 GeneratedCode：'inprocess'(L1) 或 'subprocess'(L2)。

    'subprocess'（L2，S6）：子进程隔离 + SIGKILL 硬超时 +（Linux）resource.setrlimit；
    在不支持的平台（macOS / Windows）自动回落 L1 in-process。'inprocess' 直接走 L1。
    无论哪条路径，L0 静态闸都在执行前先跑（run_generated / subprocess 入口各自调用）。
    """
    if isolation == "subprocess":
        return _run_subprocess(code, inputs, provider, timeout_s=timeout_s)
    return run_generated(code, inputs, provider, timeout_s=timeout_s)


def _run_subprocess(
    code: GeneratedCode,
    inputs: dict[str, Any],
    provider: LLMProvider,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """L2 子进程隔离（S6 落地）。当前阶段：回落 L1 in-process。"""
    # S6 将在此接入 scripts/run_dify_workflow.py 子进程 + SIGKILL + Linux setrlimit。
    return run_generated(code, inputs, provider, timeout_s=timeout_s)
