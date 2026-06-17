"""HTTP 边界模型（Pydantic v2）：与内部 dataclass 解耦，保持向后兼容。

只承载序列化契约，不含业务逻辑。answer_kind / tool_status_summary 等派生值由
route 层计算后填入。
"""

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
    tool_status_summary: dict = Field(default_factory=dict)
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
    meta_by_doc: dict | None = None
    job_id: str | None = None


class JobSubmitResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    id: str
    status: str
    result: dict | None = None
    error: dict | None = None


class ErrorResponse(BaseModel):
    error: dict  # {type, message, request_id}
