"""门面：把 parse → IR → codegen + optimize 三段串成对外的一站式 API。

公开入口：
- compile_dify_yaml(source, *, target, provider_expr, emit_trace, analyze) -> CompileResult
- analyze(source, *, env=None) -> list[Suggestion]
低层入口（按需单独使用）：parse_dify_yaml / lower_to_ir / generate_code。

target 目前仅 'ragspine'（命令式纯 Python 脚本，ADR 0013 默认 1）；保留参数口子供 spineagent
等后续目标（P7）。emit_trace 预留：未来在生成代码里插入隐私 trace 钩子（corespine TraceSink 形状）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ragspine.dify.codegen.emitter import GeneratedCode, generate_code
from ragspine.dify.codegen.spineagent import generate_spineagent_code
from ragspine.dify.errors import DifyCompileError
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.ir.model import WorkflowIR
from ragspine.dify.optimize.analyzer import analyze_ir
from ragspine.dify.optimize.rules import OptimizeEnv
from ragspine.dify.optimize.suggestion import Suggestion
from ragspine.dify.parse.loader import parse_dify_yaml

# 支持的编译目标：'ragspine'（命令式纯 Python，默认）、'spineagent'（agent/tool-use 编排，P7）。
_SUPPORTED_TARGETS: frozenset[str] = frozenset({"ragspine", "spineagent"})

__all__ = [
    "CompileResult",
    "GeneratedCode",
    "compile_dify_yaml",
    "analyze",
    "parse_dify_yaml",
    "lower_to_ir",
    "generate_code",
]


@dataclass(frozen=True)
class CompileResult:
    """一次编译的完整产物：生成代码 + 优化建议 + 中间表示。"""

    code: GeneratedCode
    suggestions: tuple[Suggestion, ...]
    ir: WorkflowIR


def compile_dify_yaml(
    source: str | Path,
    *,
    target: str = "ragspine",
    provider_expr: str = "MockProvider()",
    emit_trace: bool = False,
    analyze: bool = True,
    fold_answer_question: bool = True,
) -> CompileResult:
    """把一个 Dify 工作流 YAML 编译成纯 Python（+ 可选静态优化建议）。

    参数：
        source: Dify DSL 的 YAML 文本，或指向 `.yml` 文件的路径（str/Path）。
        target: 编译目标，'ragspine'（命令式纯 Python 脚本，默认）或 'spineagent'（含 agent/tool-use
            结构时映射到 spineagent Coordinator/agent 编排，生成代码 import spineagent）。
        provider_expr: 生成代码内 provider 默认值表达式（默认 'MockProvider()'，离线可跑）。
        emit_trace: 预留——未来在生成代码插入隐私 trace 钩子（暂未实现，传 True 仅记录意图）。
        analyze: 是否一并跑静态优化分析（默认 True）。
        fold_answer_question: 是否把「问答骨架」（start→knowledge-retrieval→llm(context 指向该检索)
            →answer/end）折叠成一次 ragspine.answer_question（自带反幻觉/provenance）。默认 True；
            仅对 target='ragspine' 生效。

    返回 CompileResult(code, suggestions, ir)。任何阶段失败抛 DifyCompileError 系。
    """
    if target not in _SUPPORTED_TARGETS:
        supported = "、".join(sorted(_SUPPORTED_TARGETS))
        raise DifyCompileError(
            f"暂不支持编译目标 target={target!r}（当前支持：{supported}）。",
            code="dify.unsupported_target",
            target=target,
        )
    doc = parse_dify_yaml(source)
    ir = lower_to_ir(doc)
    if target == "spineagent":
        code = generate_spineagent_code(ir, provider_expr=provider_expr)
    else:
        code = generate_code(
            ir, provider_expr=provider_expr, fold_answer_question=fold_answer_question
        )
    suggestions = tuple(analyze_ir(ir)) if analyze else ()
    return CompileResult(code=code, suggestions=suggestions, ir=ir)


def analyze(source: str | Path, *, env: OptimizeEnv | None = None) -> list[Suggestion]:
    """只跑静态优化分析（parse → IR → rules），返回建议列表（零代码生成、零 API）。"""
    ir = lower_to_ir(parse_dify_yaml(source))
    return analyze_ir(ir, env=env)
