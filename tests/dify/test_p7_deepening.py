"""P7 深化验收：knowledge-retrieval / parameter-extractor 真实生成、answer_question 折叠、
tool 节点（ragspine 目标的 @function_tool 占位）与 target='spineagent' 编排口子。

全程离线、零真实 LLAPI：LLM 走 ragspine.MockProvider（确定性）或本地 scripted provider。
spineagent 未装在 ragspine 的 venv 里，故对「import spineagent」的生成代码，先把一个极小离线
stub 模块注入 sys.modules['spineagent'] 再 exec（fixture 收尾还原）。带文件副作用的检索/折叠
exec 一律 monkeypatch.chdir(tmp_path) 隔离，断言不落任何 .sqlite 文件。
"""

from __future__ import annotations

import ast
import json
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from corespine.llm.provider import (
    ChatCompletion,
    Choice,
    FunctionCall,
    ResponseMessage,
    ToolCall,
)

from ragspine import MockProvider
from ragspine.dify import (
    DifyCompileError,
    compile_dify_yaml,
    lower_to_ir,
    parse_dify_yaml,
)
from ragspine.dify.codegen.fold import detect_answer_question_folds
from ragspine.dify.codegen.spineagent import generate_spineagent_code

# ---------------------------------------------------------------------------
# 公共 helper：编译 + exec（与 test_p3_codegen 同风格）。
# ---------------------------------------------------------------------------


def _compile(fixture_text: Callable[[str], str], name: str, **kw: Any) -> Any:
    """编译一个 fixture，返回 CompileResult。"""
    return compile_dify_yaml(fixture_text(name), **kw)


def _exec(source: str) -> dict[str, Any]:
    """exec 生成代码到一个新命名空间并返回（暴露 run_workflow / run_agent / Inputs）。"""
    ns: dict[str, Any] = {}
    exec(compile(source, "<dify:p7>", "exec"), ns)  # noqa: S102
    return ns


# ---------------------------------------------------------------------------
# spineagent 离线 stub：让「import spineagent」的生成代码可 exec（spineagent 未装在本 venv）。
# 形态对齐家族 spineagent：function_tool / FunctionCallingAgent / Coordinator / AgentResult。
# ---------------------------------------------------------------------------


@pytest.fixture
def spineagent_stub() -> Iterator[types.ModuleType]:
    """注入一个极小离线 spineagent stub 到 sys.modules，yield 后还原。

    - function_tool：装饰器 → 带 .name/.description/.schema()/.invoke(args) 的工具对象。
    - FunctionCallingAgent.step：以 t.schema() dict 调 model.chat（MockProvider 的 beartype
      要求 tools 是 list[dict]|None），有 tool_calls 则解析 arguments 并 invoke 匹配工具，
      否则返回消息文本。
    - Coordinator.run_sequential：顺序对每个 agent 调 step。
    - AgentResult：带 .agent / .output。
    """

    class AgentResult:
        """agent 单步结果：归属 agent 名 + 输出文本/观测。"""

        def __init__(self, agent: str, output: str) -> None:
            self.agent = agent
            self.output = output

    class _Tool:
        """@function_tool 产物：包一个函数，派生 OpenAI function-tool schema 并可 invoke。"""

        def __init__(self, fn: Callable[..., Any]) -> None:
            self._fn = fn
            self.name = fn.__name__
            self.description = (fn.__doc__ or "").strip()

        def schema(self) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }

        def invoke(self, args: dict[str, Any]) -> str:
            return str(self._fn(**args))

    def function_tool(fn: Callable[..., Any]) -> _Tool:
        return _Tool(fn)

    class FunctionCallingAgent:
        """最小 function-calling agent：单步调 model.chat，有 tool_calls 则调工具。"""

        def __init__(
            self,
            name: str,
            model: Any,
            tools: list[_Tool],
            *,
            system: str = "",
            max_steps: int = 8,
        ) -> None:
            self.name = name
            self.model = model
            self.tools = tools
            self.system = system
            self.max_steps = max_steps

        def step(self, task: str, *, trace: Any = None) -> AgentResult:
            resp = self.model.chat(
                [{"role": "user", "content": task}],
                tools=[t.schema() for t in self.tools],
            )
            msg = resp.choices[0].message
            if getattr(msg, "tool_calls", None):
                call = msg.tool_calls[0]
                args = json.loads(call.function.arguments)
                for tool in self.tools:
                    if tool.name == call.function.name:
                        return AgentResult(self.name, tool.invoke(args))
                return AgentResult(self.name, "")
            return AgentResult(self.name, msg.content or "")

    class Coordinator:
        """顺序编排：对每个 agent 调一次 step，收集 AgentResult。"""

        def __init__(self, agents: list[FunctionCallingAgent], *, trace: Any = None) -> None:
            self.agents = agents

        def run_sequential(
            self, task: str, *, resilient: bool = False
        ) -> list[AgentResult]:
            return [a.step(task) for a in self.agents]

    module = types.ModuleType("spineagent")
    module.AgentResult = AgentResult  # type: ignore[attr-defined]
    module.function_tool = function_tool  # type: ignore[attr-defined]
    module.FunctionCallingAgent = FunctionCallingAgent  # type: ignore[attr-defined]
    module.Coordinator = Coordinator  # type: ignore[attr-defined]

    saved = sys.modules.get("spineagent")
    sys.modules["spineagent"] = module
    try:
        yield module
    finally:
        if saved is not None:
            sys.modules["spineagent"] = saved
        else:
            sys.modules.pop("spineagent", None)


def _no_sqlite_in(directory: Path) -> bool:
    """目录下无任何 .sqlite 文件副作用（离线 ':memory:' 库不应落盘）。"""
    return not list(directory.glob("*.sqlite"))


# ---------------------------------------------------------------------------
# Group 1：knowledge-retrieval 真实生成（ragspine 叙事检索原语）。
# ---------------------------------------------------------------------------


def test_knowledge_retrieval_generates_real_retriever(
    fixture_text: Callable[[str], str],
) -> None:
    """knowledge.yml → 真实 build_narrative_retriever + .retrieve(top_k=3)，离线空库常量在位。"""
    code = _compile(fixture_text, "knowledge").code
    src = code.source
    assert "build_narrative_retriever(" in src
    assert ".retrieve(" in src
    assert "top_k=3" in src
    assert 'KNOWLEDGE_CHUNK_DB = ":memory:"' in src
    assert any(
        "build_narrative_retriever" in imp for imp in code.imports
    ), "build_narrative_retriever 的 import 应被收集进 code.imports"


def test_knowledge_retrieval_source_is_valid_python(
    fixture_text: Callable[[str], str],
) -> None:
    """knowledge.yml 生成源是合法 Python（AST 可解析）。"""
    code = _compile(fixture_text, "knowledge").code
    tree = ast.parse(code.source)
    assert isinstance(tree, ast.Module)


def test_knowledge_retrieval_runs_offline_no_file_side_effect(
    fixture_text: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """exec 后 run_workflow 离线跑通（':memory:' 空库无果亦不崩），且不落任何 .sqlite。"""
    monkeypatch.chdir(tmp_path)
    ns = _exec(_compile(fixture_text, "knowledge").code.source)
    out = ns["run_workflow"](
        ns["Inputs"](question="香港REVENUE多少"), provider=MockProvider()
    )
    assert isinstance(out, dict)
    assert "answer" in out
    assert _no_sqlite_in(tmp_path), "':memory:' 库不应在 CWD 落任何 .sqlite 文件"


# ---------------------------------------------------------------------------
# Group 2：parameter-extractor 真实生成（corespine function-calling 形状）。
# ---------------------------------------------------------------------------


def test_parameter_extractor_generates_function_calling(
    fixture_text: Callable[[str], str],
) -> None:
    """knowledge.yml → function-tool schema + provider.chat(tools=[_pe_schema...]) + json.loads/tool_calls。"""
    code = _compile(fixture_text, "knowledge").code
    src = code.source
    assert "'type': 'function'" in src
    assert "provider.chat(" in src
    assert "tools=[_pe_schema" in src
    assert "json.loads(" in src
    assert ".tool_calls" in src
    assert "import json" in code.imports


def test_parameter_extractor_handles_no_tool_calls(
    fixture_text: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MockProvider 对抽取工具不发 tool_calls → 参数抽取分支回退空 dict，run_workflow 不崩。"""
    monkeypatch.chdir(tmp_path)
    ns = _exec(_compile(fixture_text, "knowledge").code.source)
    out = ns["run_workflow"](
        ns["Inputs"](question="香港REVENUE多少"), provider=MockProvider()
    )
    assert isinstance(out, dict)
    assert "answer" in out


# ---------------------------------------------------------------------------
# Group 3：answer_question 折叠（问答骨架识别 + 折叠代码 + 关闭还原）。
# ---------------------------------------------------------------------------


def test_detect_fold_on_qa_skeleton(fixture_text: Callable[[str], str]) -> None:
    """qa_fold.yml IR 恰好命中 1 个折叠：(kr_1, llm_1)。"""
    ir = lower_to_ir(parse_dify_yaml(fixture_text("qa_fold")))
    plans = detect_answer_question_folds(ir)
    assert len(plans) == 1
    assert plans[0].kr_id == "kr_1"
    assert plans[0].llm_id == "llm_1"


def test_detect_fold_quiet_without_context_ref(
    fixture_text: Callable[[str], str],
) -> None:
    """knowledge.yml 的 llm_1 不挂 context → 无问答骨架，折叠为空。"""
    ir = lower_to_ir(parse_dify_yaml(fixture_text("knowledge")))
    assert detect_answer_question_folds(ir) == ()


def test_fold_generates_answer_question_call(
    fixture_text: Callable[[str], str],
) -> None:
    """qa_fold.yml 默认折叠：发 answer_question(narrative_retriever=...)+FactStore，折叠掉 llm 的逐节点 chat。"""
    code = _compile(fixture_text, "qa_fold").code
    src = code.source
    assert "answer_question(" in src
    assert "narrative_retriever=" in src
    assert "FactStore(" in src
    assert "ANSWER_QUESTION_FACT_DB" in src
    # 被折叠的 llm_1 不再逐节点拼 messages（折叠取代了 retrieve+chat 手拼）。
    assert "_messages_llm_1" not in src
    assert "from ragspine import FactStore, answer_question" in code.imports


def test_fold_disabled_falls_back_to_per_node(
    fixture_text: Callable[[str], str],
) -> None:
    """fold_answer_question=False → 不折叠，还原成逐节点 .retrieve + provider.chat。"""
    code = _compile(fixture_text, "qa_fold", fold_answer_question=False).code
    src = code.source
    assert "answer_question(" not in src
    assert ".retrieve(" in src
    assert "provider.chat(" in src


def test_fold_runs_offline(
    fixture_text: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """exec 折叠代码离线跑通：answer_question 返回非空 answer 字符串，且不落 .sqlite。"""
    monkeypatch.chdir(tmp_path)
    ns = _exec(_compile(fixture_text, "qa_fold").code.source)
    out = ns["run_workflow"](
        ns["Inputs"](question="香港REVENUE多少"), provider=MockProvider()
    )
    assert isinstance(out, dict)
    assert "answer" in out
    assert isinstance(out["answer"], str)
    assert out["answer"]
    assert _no_sqlite_in(tmp_path)


def test_fold_codegen_is_deterministic(fixture_text: Callable[[str], str]) -> None:
    """折叠编译两次字节级一致（离线可复现）。"""
    a = _compile(fixture_text, "qa_fold").code.source
    b = _compile(fixture_text, "qa_fold").code.source
    assert a == b


# ---------------------------------------------------------------------------
# Group 4：tool 节点（target='ragspine'）→ spineagent @function_tool 占位 + invoke。
# ---------------------------------------------------------------------------


def test_tool_node_generates_function_tool_placeholder(
    fixture_text: Callable[[str], str],
) -> None:
    """agent_tool.yml（ragspine 目标）→ @function_tool 占位 _tool_tool_1 + invoke 调用点，warning 提示。"""
    code = _compile(fixture_text, "agent_tool").code
    src = code.source
    assert "@function_tool" in src
    assert "def _tool_tool_1(" in src
    assert ".invoke(" in src
    assert "from spineagent import function_tool" in code.imports
    assert any("占位" in w or "placeholder" in w.lower() for w in code.warnings)
    assert isinstance(ast.parse(src), ast.Module)


def test_tool_node_exec_raises_not_implemented(
    fixture_text: Callable[[str], str],
    spineagent_stub: types.ModuleType,
) -> None:
    """注入 spineagent stub 后 exec ragspine-target tool 代码：占位工具运行即抛 NotImplementedError。"""
    ns = _exec(_compile(fixture_text, "agent_tool").code.source)
    with pytest.raises(NotImplementedError):
        ns["run_workflow"](ns["Inputs"](city="HK"), provider=MockProvider())


# ---------------------------------------------------------------------------
# Group 5：target='spineagent'（含 tool 节点 → Coordinator/FunctionCallingAgent 编排）。
# ---------------------------------------------------------------------------


def test_spineagent_target_emits_agent_orchestration(
    fixture_text: Callable[[str], str],
) -> None:
    """agent_tool.yml target='spineagent'：入口 run_agent，含 FunctionCallingAgent/Coordinator/run_sequential。"""
    code = _compile(fixture_text, "agent_tool", target="spineagent").code
    src = code.source
    assert code.entrypoint == "run_agent"
    assert "FunctionCallingAgent(" in src
    assert "Coordinator(" in src
    assert "run_sequential(" in src
    assert "@function_tool" in src
    assert "def _tool_tool_1(" in src
    assert any("spineagent import" in imp for imp in code.imports)
    assert isinstance(ast.parse(src), ast.Module)


def test_spineagent_target_rejects_no_tool_structure(
    fixture_text: Callable[[str], str],
) -> None:
    """无 tool 节点的工作流（seq.yml）→ generate_spineagent_code 抛 dify.no_agent_structure。"""
    ir = lower_to_ir(parse_dify_yaml(fixture_text("seq")))
    with pytest.raises(DifyCompileError) as exc:
        generate_spineagent_code(ir)
    assert exc.value.code == "dify.no_agent_structure"


def test_spineagent_target_runs_one_turn_text(
    fixture_text: Callable[[str], str],
    spineagent_stub: types.ModuleType,
) -> None:
    """PATH A：MockProvider 不发 tool_calls → 一轮出文本，run_agent 返回带 .output 字符串的结果。"""
    ns = _exec(_compile(fixture_text, "agent_tool", target="spineagent").code.source)
    result = ns["run_agent"](ns["Inputs"](city="HK"), provider=MockProvider())
    assert hasattr(result, "output")
    assert isinstance(result.output, str)


def test_spineagent_target_invokes_tool_and_raises(
    fixture_text: Callable[[str], str],
    spineagent_stub: types.ModuleType,
) -> None:
    """PATH B：scripted provider 发 tool_call 到 _tool_tool_1 → agent 调占位工具 → NotImplementedError。"""

    class _ScriptedProvider:
        """确定性 provider：单轮即发一个 _tool_tool_1 的 tool_call（驱动 agent 真正调工具）。"""

        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            tools: list[dict[str, Any]] | None = None,
        ) -> ChatCompletion:
            call = ToolCall(
                "call_1",
                FunctionCall("_tool_tool_1", '{"location":"HK","unit":"celsius"}'),
            )
            msg = ResponseMessage("assistant", None, (call,))
            return ChatCompletion(choices=(Choice(0, msg, "tool_calls"),))

    ns = _exec(_compile(fixture_text, "agent_tool", target="spineagent").code.source)
    with pytest.raises(NotImplementedError):
        ns["run_agent"](ns["Inputs"](city="HK"), provider=_ScriptedProvider())
