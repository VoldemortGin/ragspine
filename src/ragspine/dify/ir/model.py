"""去 Dify 化的中间表示（IR）：值引用 + 节点图 + 工作流。全 frozen dataclass，纯 stdlib。

这是三段的解耦中枢。IR【不】认 Dify 的 wire 形状（那留在 parse 段），也【不】认生成代码的
Python 细节（那留在 codegen 段）；它只表达「一个有向无环的节点图，节点间用具名值流连接」。

值模型（节点输入怎么取值）：
- VarRef(node_id, field)  —— 引用另一节点的输出字段（Dify 的 value_selector / {{#id.field#}}）。
- Literal(value)          —— 一个常量字面量。
- TemplateValue(parts)    —— 模板串：文本片段与 VarRef 交替（{{#id.field#}} 内插）。

节点模型（IRNode 子类，按职责分）：start/end/answer/llm/code/if-else/iteration/
template-transform，以及一个兜底的 UnsupportedNode（留钩子：生成 NotImplementedError 骨架）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 值模型：节点输入如何取值（去 Dify value_selector / {{#id.field#}} 化）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VarRef:
    """对另一节点输出字段的引用。node_id 为来源节点，field 为其输出字段名（如 'text'/'output'）。

    特例：来源为 start 节点时，field 即工作流输入变量名（如 'question'）。
    """

    node_id: str
    field: str


@dataclass(frozen=True)
class Literal:
    """一个常量字面量（字符串/数字/布尔等，原样进生成代码）。"""

    value: Any


@dataclass(frozen=True)
class TemplateValue:
    """模板串：parts 为 (str | VarRef) 的有序序列，渲染时文本原样、VarRef 取值内插。

    例：`"你好 {{#start.name#}}！"` → parts=("你好 ", VarRef("start","name"), "！")。
    """

    parts: tuple[str | VarRef, ...]

    def refs(self) -> tuple[VarRef, ...]:
        """模板中出现的全部 VarRef（去 Dify 化后的依赖来源）。"""
        return tuple(p for p in self.parts if isinstance(p, VarRef))


# 一个节点输入值可以是这三者之一。
Value = VarRef | Literal | TemplateValue


def value_refs(value: Value) -> tuple[VarRef, ...]:
    """从任意 Value 取出它依赖的 VarRef（Literal 无依赖）。"""
    if isinstance(value, VarRef):
        return (value,)
    if isinstance(value, TemplateValue):
        return value.refs()
    return ()


# ---------------------------------------------------------------------------
# 节点模型：IRNode 基类 + 各类型子类（frozen dataclass）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IRNode:
    """IR 节点基类：稳定 id + 人类可读 title。子类按节点职责承载各自字段。

    子类不覆盖 `kind`（用于 codegen dispatch 与优化规则识别）时，默认取类名小写去 'node'。
    """

    id: str
    title: str = ""

    @property
    def kind(self) -> str:
        """节点种类标签（codegen / optimize 据此分派）。子类可覆盖为更稳定的字符串。"""
        name = type(self).__name__
        return name[:-4].lower() if name.endswith("Node") else name.lower()

    def inputs(self) -> tuple[Value, ...]:
        """本节点取值用到的全部输入 Value（用于数据依赖分析）。默认无。"""
        return ()

    def dep_refs(self) -> tuple[VarRef, ...]:
        """本节点依赖的全部 VarRef（从 inputs() 摊平；用于拓扑/并行分析的数据依赖边）。"""
        out: list[VarRef] = []
        for v in self.inputs():
            out.extend(value_refs(v))
        return tuple(out)


@dataclass(frozen=True)
class StartNode(IRNode):
    """工作流起点：声明输入变量（名 → 是否必填）。生成代码读 inputs.<var>。"""

    variables: tuple[str, ...] = ()


@dataclass(frozen=True)
class EndNode(IRNode):
    """workflow 终点：收集 outputs（变量名 → 取值）进返回 dict。"""

    outputs: tuple[tuple[str, Value], ...] = ()

    def inputs(self) -> tuple[Value, ...]:
        return tuple(v for _, v in self.outputs)


@dataclass(frozen=True)
class AnswerNode(IRNode):
    """advanced-chat 的回复节点：把一个模板渲染成最终 answer。"""

    answer: TemplateValue = field(default_factory=lambda: TemplateValue(()))

    def inputs(self) -> tuple[Value, ...]:
        return (self.answer,)


@dataclass(frozen=True)
class LLMMessage:
    """LLM 节点的一条提示消息：role + 模板化文本。"""

    role: str
    text: TemplateValue


@dataclass(frozen=True)
class LLMNode(IRNode):
    """LLM 调用节点：一组提示消息 + 模型配置（max_tokens 等供优化规则用）。

    生成代码：把 messages 渲染成 OpenAI 形状 list[dict]，调 provider.chat(messages)，
    取 choices[0].message.content。
    """

    messages: tuple[LLMMessage, ...] = ()
    model_name: str = ""
    max_tokens: int | None = None

    def inputs(self) -> tuple[Value, ...]:
        return tuple(m.text for m in self.messages)


@dataclass(frozen=True)
class CodeNode(IRNode):
    """code 节点：内联用户代码（来源信任假设，ADR 0013 默认 3）。

    code 为原始用户代码体；inputs_map 把代码入参名映射到取值；outputs 为代码产出的字段名。
    """

    code: str = ""
    code_language: str = "python3"
    inputs_map: tuple[tuple[str, Value], ...] = ()
    outputs: tuple[str, ...] = ()

    def inputs(self) -> tuple[Value, ...]:
        return tuple(v for _, v in self.inputs_map)


@dataclass(frozen=True)
class IfBranch:
    """if-else / question-classifier 的一条分支：handle（true/false/case_id）+ 目标节点 id。

    condition_expr 为已渲染的 Python 条件表达式文本（else 分支为 None）。
    """

    handle: str
    condition_expr: str | None
    refs: tuple[VarRef, ...] = ()


@dataclass(frozen=True)
class IfElseNode(IRNode):
    """条件分流节点：按各分支条件把控制流导向不同后继（codegen → if/elif/else）。

    branches 不含 else；else 出边（无条件的兜底分支）由图结构在 codegen 段据 source_handle 处理。
    """

    branches: tuple[IfBranch, ...] = ()

    @property
    def kind(self) -> str:
        return "if-else"

    def inputs(self) -> tuple[Value, ...]:
        # 条件里的 VarRef 以 Literal 占位不便，直接在 dep_refs 里补。
        return ()

    def dep_refs(self) -> tuple[VarRef, ...]:
        out: list[VarRef] = []
        for b in self.branches:
            out.extend(b.refs)
        return tuple(out)


@dataclass(frozen=True)
class TemplateTransformNode(IRNode):
    """template-transform 节点：把一个模板（Jinja2/string.Template）渲染成 output 字段。"""

    template: TemplateValue = field(default_factory=lambda: TemplateValue(()))

    @property
    def kind(self) -> str:
        return "template-transform"

    def inputs(self) -> tuple[Value, ...]:
        return (self.template,)


@dataclass(frozen=True)
class IterationNode(IRNode):
    """迭代节点：对一个数组逐项跑内层子图，收集每项输出为数组。

    iterator: 被迭代的数组来源（VarRef）。
    body: 内层子图（已 lower 的 WorkflowIR，节点引用以 item 为入口变量）。
    output: 内层每轮的输出取值（VarRef，指向子图某节点）。
    is_parallel / parallel_nums: 是否并行 + 并发上限（codegen → for vs ThreadPoolExecutor）。
    """

    iterator: VarRef = field(default_factory=lambda: VarRef("", ""))
    body: WorkflowIR | None = None
    output: VarRef | None = None
    is_parallel: bool = False
    parallel_nums: int = 1

    def inputs(self) -> tuple[Value, ...]:
        return (self.iterator,)


@dataclass(frozen=True)
class UnsupportedNode(IRNode):
    """尚未建模/不支持的节点（http-request/tool/knowledge-retrieval/插件等）。

    留钩子：codegen 生成带 raise NotImplementedError + 详细 docstring 的骨架函数 + warning，
    产可运行骨架而非整体失败（ADR 0013 默认 4）。raw 保留原始 data 供 docstring 写明。
    """

    node_type: str = ""
    raw: tuple[tuple[str, Any], ...] = ()

    @property
    def kind(self) -> str:
        return self.node_type or "unsupported"


# ---------------------------------------------------------------------------
# 图与工作流。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IREdge:
    """IR 有向边：source → target，source_handle 区分 if-else 的 true/false/case 分支。"""

    source: str
    target: str
    source_handle: str | None = None


@dataclass(frozen=True)
class IRGraph:
    """节点图：节点表（id → IRNode）+ 边表。"""

    nodes: tuple[IRNode, ...]
    edges: tuple[IREdge, ...]

    def node_map(self) -> dict[str, IRNode]:
        """id → IRNode 索引。"""
        return {n.id: n for n in self.nodes}

    def successors(self, node_id: str) -> tuple[IREdge, ...]:
        """node_id 的全部出边。"""
        return tuple(e for e in self.edges if e.source == node_id)

    def predecessors(self, node_id: str) -> tuple[IREdge, ...]:
        """node_id 的全部入边。"""
        return tuple(e for e in self.edges if e.target == node_id)


@dataclass(frozen=True)
class WorkflowIR:
    """一个去 Dify 化的工作流：模式 + 图 + 拓扑序 + 并行分层。

    mode: 'workflow' | 'advanced-chat'。
    graph: 节点图。
    topo_order: 节点 id 的一个合法拓扑序（Kahn）。
    parallel_layers: 拓扑分层——同层节点彼此无依赖、可并发；层间有序。
    """

    mode: str
    graph: IRGraph
    topo_order: tuple[str, ...]
    parallel_layers: tuple[tuple[str, ...], ...]

    def node(self, node_id: str) -> IRNode:
        """按 id 取节点（不存在则 KeyError——程序错误，不静默）。"""
        return self.graph.node_map()[node_id]

    def nodes_in_order(self) -> tuple[IRNode, ...]:
        """按拓扑序排好的节点序列（codegen 展平用）。"""
        index = self.graph.node_map()
        return tuple(index[nid] for nid in self.topo_order)
