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

    context_ref（P7）：advanced-chat 的 LLM 节点可挂一个 context 变量（Dify `context.variable_selector`），
    把上游 knowledge-retrieval 的检索结果作为外部知识喂给提示。其存在是 answer_question 折叠
    （codegen/fold.py）识别「问答骨架」的关键信号——非 None 且上游为 KnowledgeRetrievalNode 即可折叠。
    """

    messages: tuple[LLMMessage, ...] = ()
    model_name: str = ""
    max_tokens: int | None = None
    context_ref: VarRef | None = None

    def inputs(self) -> tuple[Value, ...]:
        base = tuple(m.text for m in self.messages)
        return (*base, self.context_ref) if self.context_ref is not None else base


@dataclass(frozen=True)
class KnowledgeRetrievalNode(IRNode):
    """knowledge-retrieval 节点（P7 真实生成）：把一个查询打到 ragspine 叙事检索原语。

    query: 查询文本来源（VarRef，通常指向 start 的 question 或 sys.query）。
    dataset_ids: Dify 数据集 id（生成代码里作为 chunk_db 路径的占位/提示；离线默认空库）。
    top_k: 召回条数（Dify retrieval_mode 配置；缺省 4）。
    output_field: 本节点输出字段名（下游引用 {{#id.result#}} 取检索片段，默认 'result'）。

    生成代码：`build_narrative_retriever(chunk_db, provider=provider)` → `.retrieve(query, top_k=k)`，
    产出片段列表写回 _ctx（ragspine.retrieval.link.narrative_link 原语，离线 BM25+RRF 可跑）。
    """

    query: Value = field(default_factory=lambda: Literal(""))
    dataset_ids: tuple[str, ...] = ()
    top_k: int = 4
    output_field: str = "result"

    @property
    def kind(self) -> str:
        return "knowledge-retrieval"

    def inputs(self) -> tuple[Value, ...]:
        return (self.query,)


@dataclass(frozen=True)
class ExtractParam:
    """parameter-extractor 的一个待抽取参数：name + JSON-schema 类型 + 描述 + 是否必填。"""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class ParameterExtractorNode(IRNode):
    """parameter-extractor 节点（P7 真实生成）：用 LLM function-calling 从文本抽取结构化参数。

    query: 待抽取的文本来源（VarRef）。
    parameters: 要抽取的参数 (name, type, description, required) 列表（→ JSON-schema function tool）。
    instruction: 抽取指令（拼进提示）。
    model_name: 模型名（透传，离线 MockProvider 不依赖）。

    生成代码：拼一个 OpenAI function-tool schema（corespine provider.chat(tools=[...]) 形状）→
    `provider.chat(messages, tools=[schema])` → 取 choices[0].message.tool_calls[0].function.arguments
    （JSON）解析成 dict 写回 _ctx，每个参数一个输出字段。
    """

    query: Value = field(default_factory=lambda: Literal(""))
    parameters: tuple[ExtractParam, ...] = ()
    instruction: str = ""
    model_name: str = ""

    @property
    def kind(self) -> str:
        return "parameter-extractor"

    def inputs(self) -> tuple[Value, ...]:
        return (self.query,)


@dataclass(frozen=True)
class ToolNode(IRNode):
    """tool 节点（P7）：调用一个工具。生成代码映射到 spineagent `@function_tool` 形状。

    tool_name: 工具名（Dify provider_id/tool_name）。
    inputs_map: 工具入参名 → 取值。
    output_field: 输出字段名（默认 'text'）。

    生成代码：发出一个 spineagent `@function_tool` 装饰的占位函数（生成代码 import spineagent，
    编译器本身不新增依赖）+ 调用点；函数体留 NotImplementedError 待用户补全真实工具逻辑——
    工具的【副作用】无法由编译器凭空生成，但把它落成家族标准 FunctionTool 形状，便于接 spineagent 编排。
    """

    tool_name: str = ""
    inputs_map: tuple[tuple[str, Value], ...] = ()
    output_field: str = "text"

    @property
    def kind(self) -> str:
        return "tool"

    def inputs(self) -> tuple[Value, ...]:
        return tuple(v for _, v in self.inputs_map)


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
class AggregatorGroup:
    """variable-aggregator 分组模式的一组：组名 + 组内候选引用（按声明序取首个非空）。"""

    name: str
    items: tuple[VarRef, ...] = ()


@dataclass(frozen=True)
class VariableAggregatorNode(IRNode):
    """variable-aggregator 节点：多分支变量聚合，取首个已产出且非 None 的候选值。

    items: 基础模式的候选引用（按声明序）。
    groups: 分组模式（advanced_settings.group_enabled）——每组独立聚合，输出字段
        '<组名>.output'；基础模式输出字段 'output'。
    语义对齐 Dify 上游 first-non-null：未执行分支（运行期无产出）与产出 None 都跳过；
    空串等 falsy 值【算】有效产出（非真值判定）。
    """

    items: tuple[VarRef, ...] = ()
    groups: tuple[AggregatorGroup, ...] = ()

    @property
    def kind(self) -> str:
        return "variable-aggregator"

    def inputs(self) -> tuple[Value, ...]:
        out: list[Value] = list(self.items)
        for group in self.groups:
            out.extend(group.items)
        return tuple(out)


@dataclass(frozen=True)
class AssignItem:
    """variable-assigner（Dify assigner v2）的一条赋值：目标变量 + 操作 + 取值。

    target: 写入目标——conversation 变量为 VarRef('conversation', 名)，loop 变量为
        VarRef(循环节点 id, 变量名)（与下游 {{#conversation.x#}} 读取键一致）。
    operation: over-write/set/clear/append/extend/remove-first/remove-last/+=/-=/*=//=。
    value: 取值（input_type constant → Literal，variable → VarRef）；clear/remove-* 无值 → None。
    """

    target: VarRef
    operation: str
    value: Value | None = None


@dataclass(frozen=True)
class VariableAssignerNode(IRNode):
    """assigner 节点（Dify Variable Assigner v2）：向变量池写值。

    单发执行没有跨请求会话——conversation 变量落成【同一次运行内】的变量池：
    run_workflow 开头按 conversation_variables 声明种默认值（WorkflowIR.conversation_defaults），
    本节点在池内就地读改写，下游 {{#conversation.x#}} 立即可见；loop 变量同理写回循环节点键。
    """

    items: tuple[AssignItem, ...] = ()

    @property
    def kind(self) -> str:
        return "assigner"

    def inputs(self) -> tuple[Value, ...]:
        return tuple(i.value for i in self.items if i.value is not None)


@dataclass(frozen=True)
class DocumentExtractorNode(IRNode):
    """document-extractor 节点：文件/文本变量 → 纯文本（纯计算，零文件 I/O）。

    受限沙箱刻意没有文件系统能力，故按 PRD 收窄为 str/list→text 纯计算：字符串原样输出，
    列表逐项转文本输出列表（对齐 Dify 单文件 text / 多文件 array[string] 的输出形状，
    单/多按运行期值类型判定）。真实文件抽取留给上传通道接 ragspine.extraction 后再接入。
    """

    source: Value = field(default_factory=lambda: Literal(None))
    is_array_file: bool = False

    @property
    def kind(self) -> str:
        return "document-extractor"

    def inputs(self) -> tuple[Value, ...]:
        return (self.source,)


@dataclass(frozen=True)
class HttpRequestNode(IRNode):
    """http-request 节点：经受控客户端发一次 HTTP 请求（安全默认关）。

    生成代码【不】import 任何网络模块——只调用模块级 _dify_http 槽位；受控 urllib 客户端由
    受信 runner 在环境变量 RAGSPINE_DIFY_HTTP_ENABLED=1 时注入（强制超时 ≤30s、响应 1MB
    上限、仅 http/https、重定向不得离开 http(s)）。未启用时 L0 静态闸直接拒跑
    （GeneratedCode.requires_http），独立运行未注入时调用即抛清晰错误。
    输出字段：status_code / body / headers。
    """

    method: str = "get"
    url: TemplateValue = field(default_factory=lambda: TemplateValue(()))
    headers: TemplateValue = field(default_factory=lambda: TemplateValue(()))
    params: TemplateValue = field(default_factory=lambda: TemplateValue(()))
    body_type: str = "none"
    body: TemplateValue | None = None
    timeout_s: float | None = None
    ssl_verify: bool = True

    @property
    def kind(self) -> str:
        return "http-request"

    def inputs(self) -> tuple[Value, ...]:
        base: list[Value] = [self.url, self.headers, self.params]
        if self.body is not None:
            base.append(self.body)
        return tuple(base)


@dataclass(frozen=True)
class LoopNode(IRNode):
    """loop 节点：带退出条件的循环容器（区别于 iteration 的按数组逐项映射）。

    body: 内层子图。每轮在【同一】上下文里顺序执行——写入跨轮累积、循环后对下游可见
        （区别于 iteration 每项独立拷贝的隔离语义）。
    loop_count: 最大轮数护栏（lower 段钳制到 [0, 100]，对齐 Dify 画布上限，杜绝死循环；
        编译产物是有界 for，无 while 死循环面）。
    break_expr / break_refs: 退出条件（每轮体执行完后判定，满足即 break——对齐 Dify 上游
        「先跑本轮、后判退出」语义）；无条件则跑满 loop_count 轮。
    loop_vars: 循环变量 (名, 初值)——进循环前种到 (循环节点 id, 名)，assigner 可就地改写，
        循环结束后保留终值供下游引用。
    """

    body: WorkflowIR | None = None
    loop_count: int = 10
    break_expr: str | None = None
    break_refs: tuple[VarRef, ...] = ()
    loop_vars: tuple[tuple[str, Value], ...] = ()

    def inputs(self) -> tuple[Value, ...]:
        return tuple(v for _, v in self.loop_vars)


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
    conversation_defaults: 会话变量 (名, 默认值)（Dify workflow.conversation_variables）。
        单发执行没有跨请求会话——落成同一次运行内的变量池：run_workflow 开头种默认值，
        assigner 节点就地改写，{{#conversation.x#}} 引用即读该池。
    """

    mode: str
    graph: IRGraph
    topo_order: tuple[str, ...]
    parallel_layers: tuple[tuple[str, ...], ...]
    conversation_defaults: tuple[tuple[str, Any], ...] = ()

    def node(self, node_id: str) -> IRNode:
        """按 id 取节点（不存在则 KeyError——程序错误，不静默）。"""
        return self.graph.node_map()[node_id]

    def nodes_in_order(self) -> tuple[IRNode, ...]:
        """按拓扑序排好的节点序列（codegen 展平用）。"""
        index = self.graph.node_map()
        return tuple(index[nid] for nid in self.topo_order)
