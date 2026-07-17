"""Deterministic lexical and injectable semantic workflow-template matching."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from concurrent.futures import Future
from threading import Lock
from typing import Protocol, runtime_checkable

from ragspine.retrieval.lexical.retrieval import (
    EmbeddingBackend,
    bm25_scores,
    cosine_similarity,
    tokenize,
)
from ragspine.workflows.errors import WorkflowMatcherError
from ragspine.workflows.model import TemplateMatch, WorkflowTemplate

_GENERIC_QUERY_TOKENS = frozenset(
    {
        "a",
        "an",
        "analysis",
        "analyze",
        "and",
        "build",
        "content",
        "create",
        "flow",
        "for",
        "make",
        "of",
        "paper",
        "please",
        "support",
        "the",
        "to",
        "workflow",
    }
).union(
    token
    for phrase in ("工作流", "分析", "处理", "信息", "内容", "支持", "创建", "构建", "论文")
    for token in tokenize(phrase)
)

# Cross-language taxonomy signals are deliberately small and controlled.  Generated
# catalog entries expose canonical ``industry:*`` / ``use-case:*`` categories in
# English, while a CLI request is frequently Chinese.  BM25 alone therefore rewards
# incidental shared CJK characters over the intended category.  These aliases bridge
# only that vocabulary gap; they do not consume upstream descriptions or workflow data.
_TAXONOMY_ALIAS_EXTRAS: dict[str, tuple[str, ...]] = {
    "industry:legal": ("法律", "法务", "合同", "contract", "case law"),
    "industry:healthcare": ("医疗", "患者", "医院", "诊所", "patient", "clinic", "hospital"),
    "industry:finance": ("金融", "投资", "股票", "financial", "investment", "stock"),
    "industry:banking": ("银行", "贷款", "bank", "loan", "mortgage"),
    "industry:insurance": ("保险", "理赔", "claim", "underwriting"),
    "industry:accounting": ("财会", "会计", "发票", "税务", "invoice", "receipt", "tax"),
    "industry:retail": ("零售", "门店", "retail store"),
    "industry:ecommerce": ("电商", "电子商务", "网店", "ecommerce", "online shop", "checkout"),
    "industry:manufacturing": ("制造", "工厂", "factory", "production line"),
    "industry:logistics": ("物流", "仓储", "配送", "shipment", "warehouse", "supply chain"),
    "industry:transportation": ("交通运输", "运输", "车队", "fleet", "freight"),
    "industry:real-estate": ("房地产", "房产", "物业", "property", "tenant", "rental"),
    "industry:construction": ("建筑工程", "建筑", "施工", "contractor"),
    "industry:energy": ("能源", "石油", "天然气", "太阳能", "oil", "gas", "solar"),
    "industry:telecommunications": ("通信", "电信", "telecom", "carrier", "subscriber"),
    "industry:technology": ("科技", "软件", "代码", "数据库", "software", "developer", "database"),
    "industry:cybersecurity": (
        "网络安全",
        "漏洞",
        "网络威胁",
        "cybersecurity",
        "vulnerability",
        "phishing",
    ),
    "industry:education": ("教育", "学生", "教师", "课程", "学校", "student", "teacher", "course"),
    "industry:research": ("科研", "论文", "文献", "学术", "paper", "literature", "academic"),
    "industry:media": ("媒体", "新闻", "播客", "news", "journalism", "podcast"),
    "industry:publishing": ("出版", "图书", "编辑出版", "publisher", "editorial", "newsletter"),
    "industry:marketing": ("市场营销", "营销", "品牌", "广告", "campaign", "seo", "brand"),
    "industry:sales": ("销售", "销售线索", "sales lead", "prospect", "crm", "deal"),
    "industry:customer-service": ("客户服务", "客服行业", "helpdesk operation"),
    "industry:human-resources": ("人力资源", "人事", "员工管理", "workforce", "employee"),
    "industry:recruitment": (
        "招聘",
        "简历",
        "面试",
        "候选人",
        "resume",
        "cv",
        "candidate",
        "interview",
    ),
    "industry:travel": ("旅行", "旅游", "航班", "行程", "trip", "flight", "itinerary"),
    "industry:hospitality": (
        "酒店",
        "宾客",
        "酒店预订",
        "hotel",
        "guest",
        "booking",
        "reservation",
    ),
    "industry:food-service": ("餐饮", "餐厅", "菜单", "食谱", "restaurant", "menu", "recipe"),
    "industry:government": ("政务", "政府", "市政", "public sector", "municipal"),
    "industry:nonprofit": ("公益组织", "公益", "慈善", "捐赠", "志愿者", "charity", "donation"),
    "industry:automotive": ("汽车", "汽车维修", "car dealer", "vehicle repair"),
    "industry:agriculture": ("农业", "农场", "作物", "farm", "crop", "livestock"),
    "industry:pharmaceuticals": (
        "制药",
        "药品",
        "药物",
        "临床试验",
        "pharma",
        "drug",
        "clinical trial",
    ),
    "industry:environment": (
        "环境",
        "气候",
        "天气",
        "碳排放",
        "可持续",
        "weather",
        "climate",
        "sustainability",
    ),
    "industry:sports": ("体育", "运动", "球队", "比赛", "运动员", "sport", "athlete"),
    "industry:entertainment": ("娱乐", "电影", "音乐", "游戏", "movie", "music", "game"),
    "industry:personal-productivity": ("个人效率", "个人生产力", "personal assistant"),
    "use-case:alerting": ("告警", "警报", "异常提醒", "alert", "notify", "notification", "warning"),
    "use-case:analysis": (
        "证据分析",
        "数据分析",
        "评估",
        "情感分析",
        "analyze",
        "evaluate",
        "sentiment analysis",
    ),
    "use-case:classification": ("分类", "打标签", "classify", "categorize", "labeling"),
    "use-case:compliance-review": ("合规", "合规审查", "审计", "compliance", "policy audit"),
    "use-case:content-creation": (
        "内容创作",
        "文案写作",
        "脚本创作",
        "content writing",
        "copywriting",
    ),
    "use-case:customer-support": (
        "客服",
        "客户支持",
        "工单处理",
        "投诉处理",
        "support",
        "ticket",
        "helpdesk",
    ),
    "use-case:data-enrichment": ("数据补全", "数据丰富", "enrich", "enrichment"),
    "use-case:data-synchronization": (
        "数据同步",
        "记录同步",
        "同步数据",
        "sync",
        "synchronize",
        "upsert",
    ),
    "use-case:document-processing": ("文档处理", "文件解析", "文档解析", "pdf processing"),
    "use-case:execution-planning": ("执行计划", "任务编排", "execution plan", "orchestration plan"),
    "use-case:extraction": ("结构化抽取", "抽取", "提取", "字段识别", "extract", "parse", "ocr"),
    "use-case:general-assistance": ("通用辅助", "通用助手"),
    "use-case:invoice-processing": (
        "发票",
        "发票处理",
        "发票审核",
        "发票识别",
        "invoice",
        "receipt",
        "billing",
    ),
    "use-case:knowledge-retrieval": (
        "知识检索",
        "知识库",
        "检索",
        "rag",
        "retrieval",
        "knowledge search",
    ),
    "use-case:lead-generation": (
        "线索发现",
        "销售线索",
        "潜在客户",
        "lead discovery",
        "prospecting",
    ),
    "use-case:monitoring": (
        "信号监测",
        "监控",
        "监测",
        "跟踪",
        "追踪",
        "monitor",
        "tracking",
        "tracker",
    ),
    "use-case:onboarding": ("引导式入门", "员工入职", "入门培训", "onboarding", "new hire guide"),
    "use-case:outreach": (
        "外联",
        "外联邮件",
        "跟进邮件",
        "outreach",
        "cold email",
        "follow-up email",
    ),
    "use-case:question-answering": (
        "有据问答",
        "问答",
        "回答问题",
        "q&a",
        "question answering",
        "chatbot",
    ),
    "use-case:recommendation": (
        "有界建议",
        "方案建议",
        "决策建议",
        "选型建议",
        "recommend options",
    ),
    "use-case:report-generation": (
        "报告生成",
        "生成报告",
        "分析报告",
        "report generation",
        "dashboard",
        "presentation",
    ),
    "use-case:research": ("专题研究", "研究", "调研", "investigate", "deep research"),
    "use-case:routing": ("请求分流", "路由", "分流", "分派", "route", "triage", "assign"),
    "use-case:scheduling": (
        "日程协调",
        "预约",
        "日程",
        "排期",
        "schedule",
        "calendar",
        "appointment",
    ),
    "use-case:social-publishing": (
        "社媒发布",
        "社交媒体发布",
        "社交媒体发帖",
        "social media publishing",
        "social media post",
        "publish to social media",
        "post to social media",
        "scheduled post",
    ),
    "use-case:summarization": (
        "证据摘要",
        "摘要",
        "总结",
        "汇总",
        "简报",
        "summarize",
        "summary",
        "digest",
    ),
    "use-case:translation": ("术语一致翻译", "翻译", "本地化", "translate", "localize"),
}

_LEGACY_TAXONOMY: dict[str, frozenset[str]] = {
    "rag-paper-qa": frozenset(
        {"industry:research", "use-case:knowledge-retrieval", "use-case:question-answering"}
    ),
    "executive-summary": frozenset({"use-case:summarization"}),
    "multilingual-translation": frozenset({"use-case:translation"}),
    "batch-content-processing": frozenset({"use-case:content-creation"}),
    "parallel-perspective-analysis": frozenset({"use-case:analysis", "use-case:research"}),
    "structured-information-extraction": frozenset({"use-case:extraction"}),
    "conditional-response-routing": frozenset(
        {
            "industry:customer-service",
            "use-case:classification",
            "use-case:customer-support",
            "use-case:routing",
        }
    ),
}

_USE_CASE_CLOSURE: dict[str, frozenset[str]] = {
    "use-case:alerting": frozenset(
        {"use-case:alerting", "use-case:classification", "use-case:routing"}
    ),
    "use-case:customer-support": frozenset({"use-case:customer-support", "use-case:routing"}),
    "use-case:invoice-processing": frozenset(
        {"use-case:extraction", "use-case:invoice-processing"}
    ),
    "use-case:knowledge-retrieval": frozenset(
        {"use-case:knowledge-retrieval", "use-case:question-answering"}
    ),
    "use-case:question-answering": frozenset(
        {"use-case:knowledge-retrieval", "use-case:question-answering"}
    ),
}

_LEGACY_FUNCTIONAL_BUCKETS: dict[str, tuple[str, ...]] = {
    "rag-paper-qa": (
        "archetype:synthesis",
        "use-case:question-answering",
    ),
    "executive-summary": (
        "archetype:synthesis",
        "use-case:summarization",
    ),
    "multilingual-translation": (
        "archetype:transformation",
        "use-case:translation",
    ),
    "structured-information-extraction": (
        "archetype:extraction",
        "use-case:extraction",
    ),
    "conditional-response-routing": (
        "archetype:routing",
        "use-case:customer-support",
    ),
}

_TAXONOMY_BOOST = 0.72
_PARTIAL_TAXONOMY_CONFIDENCE_CAP = 0.68


@runtime_checkable
class TemplateMatcher(Protocol):
    """Rank a natural-language request against catalog metadata."""

    name: str
    reuse_threshold: float
    reuse_margin: float

    def rank(
        self, query: str, templates: Sequence[WorkflowTemplate]
    ) -> tuple[TemplateMatch, ...]: ...


class LexicalTemplateMatcher:
    """Offline BM25 + token-coverage matcher with deterministic tie-breaking."""

    name = "lexical"
    reuse_threshold = 0.74
    reuse_margin = 0.08

    def __init__(self) -> None:
        self._template_token_cache: tuple[tuple[tuple[str, str], ...], list[list[str]]] | None = (
            None
        )
        self._template_token_lock = Lock()
        self._template_token_flights: dict[
            tuple[tuple[str, str], ...], Future[list[list[str]]]
        ] = {}

    def rank(self, query: str, templates: Sequence[WorkflowTemplate]) -> tuple[TemplateMatch, ...]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return ()
        query_taxonomy = _query_taxonomy(query)
        template_key = tuple((template.id, template.search_text) for template in templates)
        docs_tokens = self._tokens_for_templates(template_key)
        raw_scores = bm25_scores(query_tokens, docs_tokens)
        max_bm25 = max(raw_scores, default=0.0)
        query_set = set(query_tokens)
        normalized_query = " ".join(query.lower().split())

        matches: list[TemplateMatch] = []
        for template, doc_tokens, bm25 in zip(templates, docs_tokens, raw_scores, strict=True):
            overlap_tokens = query_set.intersection(doc_tokens)
            overlap = len(overlap_tokens) / len(query_set)
            bm25_part = bm25 / max_bm25 if max_bm25 > 0 else 0.0
            normalized_examples = {
                " ".join(item.lower().split()) for item in (template.name, *template.examples)
            }
            exact = normalized_query in normalized_examples
            confidence = 1.0 if exact else min(1.0, 0.62 * overlap + 0.38 * bm25_part)
            if not exact and query_taxonomy:
                taxonomy_alignment, taxonomy_complete = _taxonomy_alignment(
                    query_taxonomy,
                    template,
                )
                confidence += (1.0 - confidence) * _TAXONOMY_BOOST * taxonomy_alignment
                if not taxonomy_complete:
                    # A domain-only or function-only near neighbor is useful for ranking,
                    # but must not be mistaken for a reusable implementation of a
                    # multi-dimensional request (for example recruitment + classification).
                    confidence = min(confidence, _PARTIAL_TAXONOMY_CONFIDENCE_CAP)
            informative_overlap = overlap_tokens.difference(_GENERIC_QUERY_TOKENS)
            if not exact and len(informative_overlap) < 2:
                # A corpus-relative BM25 leader is not semantic certainty: a single generic
                # token ("paper", "analysis", "workflow", …) must never trigger reuse.
                confidence = min(confidence, self.reuse_threshold - 0.01)
            matches.append(
                TemplateMatch(
                    template=template,
                    confidence=confidence,
                    matcher=self.name,
                )
            )
        matches.sort(key=lambda match: (-match.confidence, match.template.id))
        return tuple(matches)

    def _tokens_for_templates(
        self,
        template_key: tuple[tuple[str, str], ...],
    ) -> list[list[str]]:
        """Tokenize one catalog once while concurrent callers wait outside the lock."""

        with self._template_token_lock:
            cache = self._template_token_cache
            if cache is not None and cache[0] == template_key:
                return cache[1]
            flight = self._template_token_flights.get(template_key)
            leader = flight is None
            if flight is None:
                flight = Future()
                self._template_token_flights[template_key] = flight

        if not leader:
            return flight.result()

        try:
            docs_tokens = [tokenize(search_text) for _, search_text in template_key]
        except BaseException as exc:
            with self._template_token_lock:
                if self._template_token_flights.get(template_key) is flight:
                    del self._template_token_flights[template_key]
            flight.set_exception(exc)
            raise

        with self._template_token_lock:
            self._template_token_cache = (template_key, docs_tokens)
            if self._template_token_flights.get(template_key) is flight:
                del self._template_token_flights[template_key]
        flight.set_result(docs_tokens)
        return docs_tokens


def _contains_alias(normalized_query: str, alias: str) -> bool:
    normalized_alias = alias.casefold()
    if normalized_alias.isascii():
        return (
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
                normalized_query,
            )
            is not None
        )
    return normalized_alias in normalized_query


def _query_taxonomy(query: str) -> frozenset[str]:
    """Resolve controlled bilingual category signals without fuzzy guessing."""

    normalized = query.casefold()
    signals: set[str] = set()
    for category, extras in _TAXONOMY_ALIAS_EXTRAS.items():
        canonical = category.split(":", 1)[1].replace("-", " ")
        if _contains_alias(normalized, canonical) or any(
            _contains_alias(normalized, alias) for alias in extras
        ):
            signals.add(category)

    # A noun such as report is commonly the output of research or monitoring, not a
    # second requested workflow.  Keeping only the operative verb prevents false
    # multi-function ambiguity while preserving a standalone report request.
    if "use-case:research" in signals or "use-case:monitoring" in signals:
        signals.discard("use-case:report-generation")
    if "use-case:invoice-processing" in signals:
        signals.discard("use-case:extraction")
    specific_industries = {
        signal
        for signal in signals
        if signal.startswith("industry:") and signal != "industry:research"
    }
    if specific_industries:
        # "legal research" and "financial research" name a function inside the
        # specified industry; they are not requests for the academic-research industry.
        signals.discard("industry:research")
    if "use-case:social-publishing" in signals and len(specific_industries) > 1:
        # In "marketing post for social media", media is the channel rather than
        # the business domain.  Preserve it only when no other industry is stated.
        signals.discard("industry:media")
    if {
        "use-case:knowledge-retrieval",
        "use-case:question-answering",
    }.intersection(signals) and not any(
        _contains_alias(normalized, cue)
        for cue in ("deep research", "专题研究", "深度研究", "市场调研", "investigate")
    ):
        # In "RAG over a research paper", research describes the corpus.  The
        # requested function is retrieval/Q&A, not a separate research-agent flow.
        signals.discard("use-case:research")

    # In "knowledge-base support Q&A", 客服 describes the audience/channel.  It is a
    # support workflow only when an operational support cue (ticket, complaint, route)
    # is also present.
    if {
        "use-case:knowledge-retrieval",
        "use-case:question-answering",
    }.intersection(signals) and not any(
        _contains_alias(normalized, cue)
        for cue in (
            "工单",
            "投诉",
            "路由",
            "分流",
            "ticket",
            "complaint",
            "triage",
            "route",
        )
    ):
        signals.discard("use-case:customer-support")
    return frozenset(signals)


def _template_taxonomy(template: WorkflowTemplate) -> frozenset[str]:
    direct = {
        category
        for category in template.categories
        if category.startswith(("industry:", "use-case:"))
    }
    direct.update(_LEGACY_TAXONOMY.get(template.id, ()))
    expanded = set(direct)
    for category in direct:
        expanded.update(_USE_CASE_CLOSURE.get(category, ()))
    return frozenset(expanded)


def _taxonomy_alignment(
    query_taxonomy: frozenset[str],
    template: WorkflowTemplate,
) -> tuple[float, bool]:
    """Return field-aware alignment and whether every requested dimension is met."""

    template_taxonomy = _template_taxonomy(template)
    query_industries = {item for item in query_taxonomy if item.startswith("industry:")}
    query_use_cases = {item for item in query_taxonomy if item.startswith("use-case:")}
    template_industries = {item for item in template_taxonomy if item.startswith("industry:")}
    template_use_cases = {item for item in template_taxonomy if item.startswith("use-case:")}

    weighted_scores: list[tuple[float, float]] = []
    complete = True
    if query_industries:
        industry_match = bool(query_industries.intersection(template_industries))
        if industry_match:
            industry_score = 1.0
        elif not template_industries or "industry:cross-industry" in template_industries:
            industry_score = 0.15
        else:
            industry_score = 0.0
        weighted_scores.append((industry_score, 0.5 if query_use_cases else 1.0))
        complete = complete and industry_match

    if query_use_cases:
        matched_use_cases = query_use_cases.intersection(template_use_cases)
        use_case_score = len(matched_use_cases) / len(query_use_cases)
        weighted_scores.append((use_case_score, 0.5 if query_industries else 1.0))
        complete = complete and matched_use_cases == query_use_cases

    if not weighted_scores:
        return 0.0, False
    total_weight = sum(weight for _, weight in weighted_scores)
    alignment = sum(score * weight for score, weight in weighted_scores) / total_weight
    return alignment, complete


def _functional_bucket(template: WorkflowTemplate) -> tuple[str, ...] | None:
    """Identify generated candidates backed by the same functional contract."""

    legacy = _LEGACY_FUNCTIONAL_BUCKETS.get(template.id)
    if legacy is not None:
        return legacy
    categories = tuple(
        sorted(
            category
            for category in template.categories
            if category.startswith(("use-case:", "archetype:"))
        )
    )
    return categories or None


class EmbeddingTemplateMatcher:
    """True semantic cosine matcher over public catalog metadata.

    The embedding backend is injected.  No SDK or model is selected by this
    class, keeping tests deterministic and model/network use explicit.
    """

    name = "embedding"
    reuse_threshold = 0.82
    reuse_margin = 0.05

    def __init__(self, backend: EmbeddingBackend, *, name: str = "embedding") -> None:
        self._backend = backend
        self.name = name
        self._template_vector_cache: (
            tuple[tuple[tuple[str, str], ...], tuple[list[float], ...]] | None
        ) = None
        self._template_vector_lock = Lock()
        self._template_vector_flights: dict[
            tuple[tuple[str, str], ...], Future[tuple[list[float], ...]]
        ] = {}

    def rank(self, query: str, templates: Sequence[WorkflowTemplate]) -> tuple[TemplateMatch, ...]:
        try:
            if not templates:
                return ()
            template_key = tuple((template.id, template.search_text) for template in templates)
            query_vector, template_vectors = self._vectors_for_query(query, template_key)

            matches: list[TemplateMatch] = []
            for template, vector in zip(templates, template_vectors, strict=True):
                confidence = max(0.0, min(1.0, cosine_similarity(query_vector, vector)))
                matches.append(
                    TemplateMatch(
                        template=template,
                        confidence=confidence,
                        matcher=self.name,
                    )
                )
            matches.sort(key=lambda match: (-match.confidence, match.template.id))
            return tuple(matches)
        except WorkflowMatcherError:
            raise
        except Exception as exc:
            raise WorkflowMatcherError("semantic embedding matcher 不可用") from exc

    def _vectors_for_query(
        self,
        query: str,
        template_key: tuple[tuple[str, str], ...],
    ) -> tuple[list[float], tuple[list[float], ...]]:
        """Encode one query and coalesce the cold template batch per catalog key."""

        with self._template_vector_lock:
            cache = self._template_vector_cache
            if cache is not None and cache[0] == template_key:
                cached_vectors = cache[1]
                flight: Future[tuple[list[float], ...]] | None = None
                leader = False
            else:
                cached_vectors = None
                flight = self._template_vector_flights.get(template_key)
                leader = flight is None
                if flight is None:
                    flight = Future()
                    self._template_vector_flights[template_key] = flight

        if cached_vectors is not None:
            query_vector = self._encode_query(query)
            self._validate_vectors(query_vector, cached_vectors, len(template_key))
            return query_vector, cached_vectors

        assert flight is not None
        if not leader:
            template_vectors = flight.result()
            query_vector = self._encode_query(query)
            self._validate_vectors(query_vector, template_vectors, len(template_key))
            return query_vector, template_vectors

        try:
            texts = [query, *(item[1] for item in template_key)]
            encoded = self._backend.embed_texts(texts)
            if len(encoded) != len(texts):
                raise WorkflowMatcherError("embedding 返回条数不正确")
            query_vector = encoded[0]
            template_vectors = tuple(list(vector) for vector in encoded[1:])
            self._validate_vectors(query_vector, template_vectors, len(template_key))
        except BaseException as exc:
            with self._template_vector_lock:
                if self._template_vector_flights.get(template_key) is flight:
                    del self._template_vector_flights[template_key]
            flight.set_exception(exc)
            raise

        with self._template_vector_lock:
            self._template_vector_cache = (template_key, template_vectors)
            if self._template_vector_flights.get(template_key) is flight:
                del self._template_vector_flights[template_key]
        flight.set_result(template_vectors)
        return query_vector, template_vectors

    def _encode_query(self, query: str) -> list[float]:
        encoded = self._backend.embed_texts([query])
        if len(encoded) != 1:
            raise WorkflowMatcherError("embedding 返回条数不正确")
        return encoded[0]

    @staticmethod
    def _validate_vectors(
        query_vector: list[float],
        template_vectors: Sequence[list[float]],
        expected_templates: int,
    ) -> None:
        if not query_vector:
            raise WorkflowMatcherError("embedding 返回空 query 向量")
        if len(template_vectors) != expected_templates:
            raise WorkflowMatcherError("embedding 返回条数不正确")
        for vector in template_vectors:
            if len(vector) != len(query_vector) or not vector:
                raise WorkflowMatcherError("embedding 维度不一致或为空")
            if not all(math.isfinite(value) for value in (*query_vector, *vector)):
                raise WorkflowMatcherError("embedding 含 NaN/Inf")


def make_template_matcher(spec: str = "auto") -> TemplateMatcher:
    """Build a workflow matcher without loading a model or touching the network."""

    from ragspine.retrieval.vector.embedding_backends import make_embedding_backend

    normalized = spec.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"none", "lexical"}:
        return LexicalTemplateMatcher()
    if normalized not in {"auto", "onnx"}:
        raise WorkflowMatcherError("workflow matcher 只支持 auto/none/onnx")
    try:
        backend = make_embedding_backend(normalized)
    except Exception as exc:
        raise WorkflowMatcherError("semantic embedding matcher 初始化失败") from exc
    if backend is None:
        if normalized == "auto":
            return LexicalTemplateMatcher()
        raise WorkflowMatcherError("onnx workflow matcher 不可用")
    return EmbeddingTemplateMatcher(backend, name="onnx")


def choose_reusable(
    matches: Sequence[TemplateMatch],
    *,
    threshold: float,
    margin: float,
) -> TemplateMatch | None:
    """Choose only a high-confidence leader; ambiguity always generates anew."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须在 0..1")
    if not 0.0 <= margin <= 1.0:
        raise ValueError("margin 必须在 0..1")
    if not matches:
        return None
    first = matches[0]
    first_bucket = _functional_bucket(first.template)
    runner_up = next(
        (
            match.confidence
            for match in matches[1:]
            if first_bucket is None or _functional_bucket(match.template) != first_bucket
        ),
        0.0,
    )
    if first.confidence < threshold or first.confidence - runner_up < margin:
        return None
    return first


__all__ = [
    "TemplateMatcher",
    "LexicalTemplateMatcher",
    "EmbeddingTemplateMatcher",
    "make_template_matcher",
    "choose_reusable",
]
