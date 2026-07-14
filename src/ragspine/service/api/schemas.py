"""HTTP 边界模型（Pydantic v2）：与内部 dataclass 解耦，保持向后兼容。

只承载序列化契约，不含业务逻辑。answer_kind / tool_status_summary 等派生值由
route 层计算后填入。
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class AskRequest(BaseModel):
    question: str
    reference_date: str | None = None
    # 可选对话历史（ADR 0017）：(role, text) 轮次序列，role ∈ {"user","assistant"}。默认 None＝
    # 无历史，行为逐字节不变。语义与引擎 answer_question(history=) 一致——历史只作 LLM 生成上下文，
    # 绝不进确定性意图解析、绝不产生新证据。
    history: list[tuple[str, str]] | None = Field(default=None)


class CacheInfo(BaseModel):
    hit: bool = False
    type: str | None = None
    faq_id: str | None = None
    version: int | None = None
    source: str | None = None


class ClarificationInfo(BaseModel):
    mode: str
    question: str | None = None
    narrowing_options: list[str] = Field(default_factory=list)
    assumption_note: str | None = None


class SourceInfo(BaseModel):
    # 容忍额外字段（不同通路的 source dict 形状略有差异）
    model_config = ConfigDict(extra="allow")

    doc: str | None = None
    locator: str | None = None


class AskResponse(BaseModel):
    request_id: str
    answer: str
    route: str
    answer_kind: str
    clarification: ClarificationInfo | None = None
    sources: list[SourceInfo] = Field(default_factory=list)
    tool_status_summary: dict[str, Any] = Field(default_factory=dict)
    cache: CacheInfo = Field(default_factory=CacheInfo)


class IngestStructuredJobRequest(BaseModel):
    file: str
    dry_run: bool = False
    valid_as_of: str | None = None
    batch_id: str | None = None
    job_id: str | None = None


class IngestNarrativeJobRequest(BaseModel):
    inputs: list[str] | str
    dry_run: bool = False
    meta_by_doc: dict[str, Any] | None = None
    job_id: str | None = None


class JobSubmitResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    id: str
    status: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: dict[str, Any]  # {type, message, request_id}


# ---------------------------------------------------------------------------
# Dify 工作流编译 / 运行（service/dify）
#
# 安全约束：客户端【不可】传 provider_expr（防代码注入）——生成代码内的 provider
# 由服务端 env 决定（build_provider），运行时以 run_workflow(provider=...) 注入。
# ---------------------------------------------------------------------------
class DifySuggestionInfo(BaseModel):
    """一条静态优化建议的对外形状（与内部 Suggestion dataclass 解耦）。"""

    rule_id: str
    severity: str
    category: str
    title: str
    detail: str = ""
    node_ids: list[str] = Field(default_factory=list)


class _DifyDocumentRequest(BaseModel):
    """JSON HTTP envelope carrying either canonical JSON or legacy YAML text."""

    model_config = ConfigDict(extra="forbid")

    workflow: dict[str, JsonValue] | None = None
    # The decoded document is bounded by UTF-8 bytes (not Python characters) at the
    # route boundary so canonical JSON and legacy YAML share exactly the same limit.
    yaml: str | None = None

    @model_validator(mode="after")
    def _exactly_one_document(self) -> Self:
        if (self.workflow is None) == (self.yaml is None):
            raise ValueError("exactly one of workflow or yaml is required")
        return self


class DifyAnalyzeRequest(_DifyDocumentRequest):
    """analyze：只跑静态优化分析（零代码生成、零 API、绝对安全）。"""


class DifyAnalyzeResponse(BaseModel):
    request_id: str
    suggestions: list[DifySuggestionInfo] = Field(default_factory=list)


class DifyCompileRequest(_DifyDocumentRequest):
    """compile：把 canonical workflow 编译成纯 Python 代码字符串（不执行）。"""

    target: Literal["ragspine", "spineagent"] = "ragspine"
    fold_answer_question: bool = True


class DifyCompileResponse(BaseModel):
    request_id: str
    code: str
    entrypoint: str
    imports: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[DifySuggestionInfo] = Field(default_factory=list)


class DifyRunRequest(_DifyDocumentRequest):
    """run：编译 + 受限执行（信任边界——默认关闭，env 显式开启才放行）。"""

    inputs: dict[str, Any] = Field(default_factory=dict)
    fold_answer_question: bool = True


class NodeTrace(BaseModel):
    """一条节点级执行 trace（run 端点的可观测面；字段名为前端消费契约，定死）。"""

    index: int  # 执行顺序，从 0 开始（skipped 记录排在最后）
    node_id: str  # dify DSL 节点 id
    title: str  # 节点标题（IR 的 title）
    node_type: str  # llm / code / if-else / knowledge-retrieval / ...（IRNode.kind）
    status: Literal["succeeded", "failed", "skipped"]
    elapsed_ms: float
    inputs: dict[str, Any] | None  # 输入变量快照（dep_refs 解析），无依赖为 None
    outputs: dict[str, Any] | None  # 该节点写入 _ctx 的输出字段快照，空为 None
    error: str | None


class DifyRunResponse(BaseModel):
    request_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    node_traces: list[NodeTrace] | None = None


# ---------------------------------------------------------------------------
# 管线拓扑导出（pipeline/topology → Studio 可视化）
# ---------------------------------------------------------------------------
class TopologyNodeInfo(BaseModel):
    """拓扑节点的对外形状（与内部 pipeline.graph.Node dataclass 解耦）。"""

    id: str
    label: str
    kind: str
    domain: str | None = None
    symbol: str | None = None


class TopologyEdgeInfo(BaseModel):
    """拓扑边的对外形状（与内部 pipeline.graph.Edge dataclass 解耦）。"""

    src: str
    dst: str
    label: str | None = None
    kind: str = "flow"


class TopologyResponse(BaseModel):
    request_id: str
    title: str
    nodes: list[TopologyNodeInfo] = Field(default_factory=list)
    edges: list[TopologyEdgeInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# n8n workflow 兼容层（ragspine.n8n）：n8n JSON ↔ Dify DSL 转换 / 运行
# ---------------------------------------------------------------------------
class N8nConvertRequest(BaseModel):
    """convert：n8n workflow JSON ↔ Dify DSL 双向转换（纯转换，不执行，安全）。"""

    direction: Literal["n8n_to_dify", "dify_to_n8n"]
    workflow: dict[str, Any] | str  # str 时按 JSON/YAML 自动识别


class N8nConvertResponse(BaseModel):
    """workflow 是目标格式 dict；n8n_to_dify 方向另给序列化好的 Dify DSL YAML。"""

    request_id: str
    workflow: dict[str, Any] = Field(default_factory=dict)
    yaml: str | None = None  # to dify 时为 Dify DSL YAML；to n8n 时为 None
    warnings: list[str] = Field(default_factory=list)


class N8nRunRequest(BaseModel):
    """run：n8n workflow → dify DSL → 完整复用 /v1/dify/run 管线（同一信任边界开关）。"""

    workflow: dict[str, Any]
    inputs: dict[str, Any] = Field(default_factory=dict)


class N8nRunResponse(DifyRunResponse):
    """形状 = DifyRunResponse（result/warnings/node_traces）+ n8n→dify 转换期 warnings。"""

    convert_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 离线工作流模板 catalog / scaffold（ragspine.workflows）
# ---------------------------------------------------------------------------
class WorkflowCompatibilityInfo(BaseModel):
    """模板与可导入工作流格式的兼容信息。"""

    model_config = ConfigDict(from_attributes=True)

    format: str
    dsl_version: str
    status: Literal["runnable", "import_only", "blocked"]


class WorkflowRequirementInfo(BaseModel):
    """使用模板前需由用户显式配置的一项能力。"""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    name: str
    required: bool = True


class WorkflowSourceMetadata(BaseModel):
    """只含事实型来源元数据；不缓存上游正文、凭据或密钥。"""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    title: str
    author: str | None = None
    upstream_id: str | None = None
    upstream_url: str | None = None
    license_status: str = "unknown"
    observed_metric: str | None = None
    observed_value: int | None = None
    observed_at: str | None = None


class WorkflowTemplateInfo(BaseModel):
    """列表项：刻意不含完整 YAML。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    compatibility: WorkflowCompatibilityInfo
    requirements: list[WorkflowRequirementInfo] = Field(default_factory=list)
    source: WorkflowSourceMetadata | None = None
    sha256: str


class WorkflowTemplateListResponse(BaseModel):
    request_id: str
    templates: list[WorkflowTemplateInfo] = Field(default_factory=list)


class WorkflowTemplateDetailResponse(WorkflowTemplateInfo):
    request_id: str
    workflow: dict[str, JsonValue]
    yaml: str


class WorkflowScaffoldRequest(BaseModel):
    """只允许语义描述与 catalog 选择；客户端不能注入 provider/I/O 能力。"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=4096)
    template_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
    )
    reuse: bool = True


class WorkflowScaffoldResponse(BaseModel):
    request_id: str
    workflow: dict[str, JsonValue]
    yaml: str
    template_id: str | None = None
    origin: Literal["template", "generated"]
    confidence: float = Field(ge=0.0, le=1.0)
    matcher: str
    warnings: list[str] = Field(default_factory=list)
    compatibility: WorkflowCompatibilityInfo
    requirements: list[WorkflowRequirementInfo] = Field(default_factory=list)
    source: WorkflowSourceMetadata | None = None
