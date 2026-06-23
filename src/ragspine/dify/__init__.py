"""ragspine.dify —— Dify 工作流 YAML → 纯 Python 编译器 + 静态优化建议器。

把一个 Dify `.yml`（workflow / advanced-chat）编译成一段【无框架、命令式、可离线运行】的
纯 Python 脚本：顶层 `run_workflow(inputs, *, provider=None) -> dict`，LLM 节点走家族
`corespine.LLMProvider.chat(messages)` 缝（默认 `ragspine.MockProvider()`），并行用
`concurrent.futures.ThreadPoolExecutor`（家族全同步，不生成 async）。同时给一份纯静态的
优化建议（零 API：并行机会 / 串行瓶颈 / 缓存 / 资源 / LLM 配置）。

三段经 IR 解耦（去 Dify 化）：

    parse/    .yml 文本 → 校验过的 DifyDoc（pydantic v2 边界，extra='allow' 宽松未知字段）
    ir/       DifyDoc → WorkflowIR（去 Dify 化节点图 + 数据流 + 拓扑 + parallel_layers）
    codegen/  WorkflowIR → GeneratedCode（命令式纯 Python，拓扑展平 + import 收集）
    optimize/ WorkflowIR → list[Suggestion]（8 条静态规则纯函数）

公开门面（api.py）：compile_dify_yaml / analyze / CompileResult / GeneratedCode；低层
parse_dify_yaml / lower_to_ir / generate_code。详见子包宪章 CLAUDE.md 与
docs/adr/0013-dify-workflow-compiler.md。

Submodules:
    parse/    — YAML 装载 + pydantic 边界校验（loader.py / schema.py）。
    ir/       — 去 Dify 化中间表示 + 拓扑（model.py / lower.py / topo.py）。
    codegen/  — IR → 纯 Python 代码（naming.py / emitter.py / nodes.py）。
    optimize/ — 静态优化建议器（suggestion.py / rules.py / analyzer.py）。
    api.py    — 门面：compile_dify_yaml / analyze / CompileResult / 低层入口。
    errors.py — 域统一异常（DifyCompileError 及子类）。
"""

import importlib

from ragspine import _lazy_submodules

_submodule_getattr, _submodule_dir = _lazy_submodules(__name__, __path__)

# 门面 API curated 暴露：`from ragspine.dify import compile_dify_yaml`。仍走惰性解析——
# `import ragspine.dify` 不急切 import api.py（也就不急切拉起 parse 段的 PyYAML）。
_CURATED: dict[str, str] = {
    "compile_dify_yaml": "api",
    "analyze": "api",
    "CompileResult": "api",
    "GeneratedCode": "api",
    "parse_dify_yaml": "api",
    "lower_to_ir": "api",
    "generate_code": "api",
    "DifyCompileError": "errors",
    "UnsupportedAppMode": "errors",
    "UnsupportedNodeType": "errors",
    "CyclicGraph": "errors",
}

__all__ = list(_CURATED)


def __getattr__(name: str) -> object:
    module_name = _CURATED.get(name)
    if module_name is not None:
        module = importlib.import_module(f"{__name__}.{module_name}")
        return getattr(module, name)
    return _submodule_getattr(name)


def __dir__() -> list[str]:
    return sorted({*__all__, *_submodule_dir()})
