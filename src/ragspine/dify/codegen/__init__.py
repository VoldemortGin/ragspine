"""codegen 段：WorkflowIR → GeneratedCode（命令式纯 Python，拓扑展平 + import 收集）。

生成的脚本骨架：`def run_workflow(inputs: Inputs, *, provider: LLMProvider | None = None) -> dict`，
顶部 `from ragspine import MockProvider` / `from corespine import LLMProvider`，并行用
`concurrent.futures.ThreadPoolExecutor`（家族全同步，不生成 async）。纯 stdlib。

Submodules:
    naming.py     — node_id → 稳定确定性的 Python 变量名（去重、合法标识符）。
    emitter.py    — IR → GeneratedCode：拓扑展平、并行分层、import 收集、组装脚本。
    nodes.py      — 每类 IRNode → 代码片段的 dispatch（start/end/answer/llm/code/检索/抽参/tool/...）。
    fold.py       — answer_question 折叠：识别问答骨架并折叠成一次 ragspine.answer_question。
    spineagent.py — target='spineagent' 口子：含 tool-use 的图 → Coordinator/FunctionCallingAgent。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
