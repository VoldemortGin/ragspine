"""HTTP 边界模型（Pydantic v2）：与内部 dataclass 解耦，保持向后兼容。

只承载序列化契约，不含业务逻辑。answer_kind / tool_status_summary 等派生值由
route 层计算后填入。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    question: str
    reference_date: str | None = None


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


class DifyAnalyzeRequest(BaseModel):
    """analyze：只跑静态优化分析（零代码生成、零 API、绝对安全）。"""

    yaml: str


class DifyAnalyzeResponse(BaseModel):
    request_id: str
    suggestions: list[DifySuggestionInfo] = Field(default_factory=list)


class DifyCompileRequest(BaseModel):
    """compile：把 Dify YAML 编译成纯 Python 代码字符串（不执行，安全）。"""

    yaml: str
    target: str = "ragspine"            # "ragspine" | "spineagent"
    fold_answer_question: bool = True


class DifyCompileResponse(BaseModel):
    request_id: str
    code: str
    entrypoint: str
    imports: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[DifySuggestionInfo] = Field(default_factory=list)


class DifyRunRequest(BaseModel):
    """run：编译 + 受限执行（信任边界——默认关闭，env 显式开启才放行）。"""

    yaml: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    fold_answer_question: bool = True


class DifyRunResponse(BaseModel):
    request_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
