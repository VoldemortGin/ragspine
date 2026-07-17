"""Generate the reviewed workflow descriptor catalog from public metadata only.

The upstream services are used strictly as attribution and popularity indexes.
Every executable-facing field is authored here from controlled industry and
use-case vocabularies; upstream workflow definitions are never requested.

Run from the Spine workspace root::

    uv --project ragspine run python ragspine/scripts/generate_workflow_catalog.py \
        --observed-at 2026-07-15T00:00:00+08:00
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DIFY_SEARCH_URL = "https://marketplace.dify.ai/api/v1/templates/search/advanced"
N8N_SEARCH_URL = "https://api.n8n.io/api/templates/search"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ragspine"
    / "workflows"
    / "templates"
    / "generated-catalog.json"
)
DEFAULT_OBSERVED_AT = "2026-07-15T00:00:00+08:00"

DIFY_PAGE_SIZE = 40
DIFY_EXPECTED_TOTAL = 238
DIFY_TARGET = 234
N8N_PAGE_SIZE = 250
N8N_SEED_PAGE_SIZE = 5
N8N_CORE_TARGET = 500
N8N_INDUSTRY_SEED_TARGET = 36
N8N_MIN_SEED_INDUSTRIES = 30
N8N_POPULAR_TARGET = N8N_CORE_TARGET - N8N_INDUSTRY_SEED_TARGET
N8N_STRATIFIED_TARGET = 259
N8N_TARGET = N8N_CORE_TARGET + N8N_STRATIFIED_TARGET
CATALOG_TARGET = DIFY_TARGET + N8N_TARGET
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 4
ALLOWED_METADATA_HOSTS = frozenset({"api.n8n.io", "marketplace.dify.ai"})

DIFY_LEGACY_SOURCE_IDS = frozenset(
    {
        "8514f1a1-9ed6-4fc1-8e9e-5d77dbfbbe3d",
        "d2d2ae92-d95b-4d55-b343-be411c3147d3",
        "c32f60fa-0551-4db0-8970-9faae6b76861",
        "b7f34cb1-f944-4741-8a50-818726383619",
    }
)
N8N_LEGACY_SOURCE_IDS = frozenset({"2165", "4336", "3149"})

# Hamilton allocation from the official top-level category hit counts observed
# for this release snapshot.  The quotas are fixed so the 259-row supplement is
# reproducible even if marketplace totals move during a later regeneration.
N8N_STRATA = (
    ("AI", 109),
    ("Marketing", 49),
    ("Other", 27),
    ("Sales", 23),
    ("Document Ops", 20),
    ("IT Ops", 17),
    ("Support", 14),
)

ARCHETYPES = ("analysis", "extraction", "routing", "synthesis", "transformation")


class CatalogGenerationError(RuntimeError):
    """Raised when a remote metadata snapshot cannot satisfy the contract."""


@dataclass(frozen=True)
class SourceItem:
    """Allowlisted upstream fields plus controlled sampling labels."""

    provider: str
    upstream_id: str
    title: str
    categories: tuple[str, ...]
    author: str
    popularity: int
    popularity_metric: str
    url: str
    sampling_layer: str = "marketplace"
    seed_industry: str | None = None


@dataclass(frozen=True)
class Industry:
    key: str
    label_en: str
    label_zh: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class UseCase:
    key: str
    label_en: str
    label_zh: str
    archetype: str
    intent_en: str
    intent_zh: str
    goal_en: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    """Evidence-backed controlled labels for one source title."""

    industry: Industry
    use_case: UseCase
    industry_evidence: tuple[str, ...]
    use_case_evidence: tuple[str, ...]
    industry_status: str
    use_case_status: str


INDUSTRIES = (
    Industry(
        "legal",
        "Legal",
        "法律",
        ("legal", "law", "contract", "court", "attorney", "法律", "法务", "合同", "契約"),
    ),
    Industry(
        "healthcare",
        "Healthcare",
        "医疗健康",
        ("healthcare", "medical", "patient", "clinic", "hospital", "医疗", "患者", "医院", "診療"),
    ),
    Industry(
        "finance",
        "Finance",
        "金融",
        ("finance", "financial", "stock", "investment", "金融", "投资", "美股", "投資"),
    ),
    Industry(
        "banking",
        "Banking",
        "银行",
        ("bank", "banking", "loan", "credit", "mortgage", "银行", "銀行", "贷款"),
    ),
    Industry(
        "insurance",
        "Insurance",
        "保险",
        ("insurance", "claim", "policyholder", "underwriting", "保险", "理赔", "保険"),
    ),
    Industry(
        "accounting",
        "Accounting",
        "财会",
        (
            "accounting",
            "invoice",
            "expense",
            "tax",
            "bookkeeping",
            "财会",
            "会计",
            "发票",
            "税务",
            "会計",
            "請求書",
        ),
    ),
    Industry(
        "retail",
        "Retail",
        "零售",
        ("retail", "store", "catalog", "inventory", "零售", "门店", "小売"),
    ),
    Industry(
        "ecommerce",
        "E-commerce",
        "电商",
        ("commerce", "shop", "order", "cart", "checkout", "电商", "电子商务", "跨境电商"),
    ),
    Industry(
        "manufacturing",
        "Manufacturing",
        "制造业",
        ("manufacturing", "factory", "production", "quality control", "制造", "工厂", "製造"),
    ),
    Industry(
        "logistics",
        "Logistics",
        "物流",
        (
            "logistics",
            "shipment",
            "warehouse",
            "delivery",
            "supply chain",
            "物流",
            "仓储",
            "配送",
        ),
    ),
    Industry(
        "transportation",
        "Transportation",
        "交通运输",
        (
            "transport",
            "transportation",
            "fleet",
            "vehicle",
            "freight",
            "交通",
            "运输",
            "輸送",
        ),
    ),
    Industry(
        "real-estate",
        "Real Estate",
        "房地产",
        (
            "real estate",
            "property",
            "tenant",
            "listing",
            "rental",
            "房地产",
            "房产",
            "物业",
            "不動産",
        ),
    ),
    Industry(
        "construction",
        "Construction",
        "建筑工程",
        ("construction", "project site", "contractor", "建筑", "施工", "建設"),
    ),
    Industry(
        "energy",
        "Energy",
        "能源",
        ("energy", "oil", "gas", "solar", "能源", "石油", "天然气", "太阳能", "エネルギー"),
    ),
    Industry(
        "utilities",
        "Utilities",
        "公用事业",
        ("utility", "water", "meter", "grid", "electricity", "公用事业", "水务", "电网", "電力"),
    ),
    Industry(
        "telecommunications",
        "Telecommunications",
        "通信",
        ("telecom", "telecommunications", "carrier", "subscriber", "通信", "电信", "通訊"),
    ),
    Industry(
        "technology",
        "Technology",
        "科技",
        (
            "software",
            "developer",
            "code",
            "api",
            "database",
            "软件",
            "代码",
            "数据库",
            "ソフトウェア",
            "コード",
        ),
    ),
    Industry(
        "cybersecurity",
        "Cybersecurity",
        "网络安全",
        (
            "security",
            "threat",
            "vulnerability",
            "incident",
            "phishing",
            "网络安全",
            "漏洞",
            "威胁",
            "サイバーセキュリティ",
        ),
    ),
    Industry(
        "education",
        "Education",
        "教育",
        (
            "education",
            "educational",
            "student",
            "teacher",
            "course",
            "exam",
            "教育",
            "学生",
            "教师",
            "课程",
            "高考",
            "大学",
            "招生",
            "学校",
        ),
    ),
    Industry(
        "research",
        "Research",
        "科研",
        (
            "research",
            "paper",
            "study",
            "literature",
            "academic",
            "科研",
            "研究",
            "论文",
            "文献",
            "学术",
        ),
    ),
    Industry(
        "media",
        "Media",
        "媒体",
        ("media", "news", "journalism", "podcast", "broadcast", "媒体", "新闻", "ニュース", "播客"),
    ),
    Industry(
        "publishing",
        "Publishing",
        "出版",
        (
            "publishing",
            "article",
            "book",
            "editorial",
            "newsletter",
            "出版",
            "文章",
            "图书",
            "記事",
        ),
    ),
    Industry(
        "marketing",
        "Marketing",
        "市场营销",
        (
            "marketing",
            "campaign",
            "seo",
            "audience",
            "brand",
            "营销",
            "品牌",
            "广告",
            "マーケティング",
            "ブランド",
        ),
    ),
    Industry(
        "sales", "Sales", "销售", ("sales", "lead", "prospect", "deal", "crm", "销售", "営業")
    ),
    Industry(
        "customer-service",
        "Customer Service",
        "客户服务",
        (
            "customer",
            "support",
            "ticket",
            "helpdesk",
            "complaint",
            "客服",
            "客户",
            "工单",
            "投诉",
            "サポート",
        ),
    ),
    Industry(
        "human-resources",
        "Human Resources",
        "人力资源",
        ("human resources", "employee", "workforce", "hr", "人力资源", "员工", "人事", "従業員"),
    ),
    Industry(
        "recruitment",
        "Recruitment",
        "招聘",
        (
            "recruit",
            "candidate",
            "resume",
            "cv",
            "interview",
            "招聘",
            "求职",
            "简历",
            "面试",
            "採用",
            "履歴書",
            "面接",
        ),
    ),
    Industry(
        "travel",
        "Travel",
        "旅行",
        ("travel", "trip", "flight", "itinerary", "tour", "旅行", "旅游", "航班", "行程"),
    ),
    Industry(
        "hospitality",
        "Hospitality",
        "酒店服务",
        (
            "hotel",
            "guest",
            "booking",
            "reservation",
            "hospitality",
            "酒店",
            "宾客",
            "预订",
            "ホテル",
        ),
    ),
    Industry(
        "food-service",
        "Food Service",
        "餐饮",
        (
            "restaurant",
            "food",
            "menu",
            "recipe",
            "catering",
            "餐厅",
            "餐饮",
            "食谱",
            "菜单",
            "レストラン",
        ),
    ),
    Industry(
        "government",
        "Government",
        "政务",
        (
            "government",
            "public sector",
            "municipal",
            "citizen",
            "permit",
            "政府",
            "政务",
            "公共部门",
            "市政",
        ),
    ),
    Industry(
        "nonprofit",
        "Nonprofit",
        "公益组织",
        (
            "nonprofit",
            "donation",
            "charity",
            "volunteer",
            "fundraising",
            "公益",
            "慈善",
            "捐赠",
            "志愿者",
            "非営利",
        ),
    ),
    Industry(
        "automotive",
        "Automotive",
        "汽车",
        ("automotive", "car", "vehicle", "dealer", "repair", "汽车", "自動車"),
    ),
    Industry(
        "agriculture",
        "Agriculture",
        "农业",
        ("agriculture", "farm", "crop", "livestock", "soil", "农业", "农场", "作物", "農業"),
    ),
    Industry(
        "pharmaceuticals",
        "Pharmaceuticals",
        "制药",
        (
            "pharma",
            "pharmaceutical",
            "drug",
            "medication",
            "clinical trial",
            "制药",
            "药品",
            "药物",
            "临床试验",
            "製薬",
            "医薬品",
        ),
    ),
    Industry(
        "biotechnology",
        "Biotechnology",
        "生物科技",
        (
            "biotech",
            "genomic",
            "protein",
            "laboratory",
            "biology",
            "生物科技",
            "基因",
            "蛋白质",
            "实验室",
            "バイオ",
        ),
    ),
    Industry(
        "environment",
        "Environment",
        "环境",
        (
            "environment",
            "climate",
            "weather",
            "carbon",
            "sustainability",
            "waste",
            "环境",
            "气候",
            "天气",
            "碳排放",
            "可持续",
            "環境",
            "天気",
        ),
    ),
    Industry(
        "sports",
        "Sports",
        "体育",
        (
            "sport",
            "sports",
            "team",
            "match",
            "athlete",
            "fitness",
            "体育",
            "运动",
            "球队",
            "比赛",
            "运动员",
            "スポーツ",
        ),
    ),
    Industry(
        "entertainment",
        "Entertainment",
        "娱乐",
        (
            "entertainment",
            "movie",
            "music",
            "game",
            "video",
            "娱乐",
            "电影",
            "音乐",
            "游戏",
            "映画",
            "音楽",
            "ゲーム",
        ),
    ),
    Industry(
        "personal-productivity",
        "Personal Productivity",
        "个人效率",
        (
            "personal",
            "productivity",
            "个人效率",
            "生产力",
        ),
    ),
)

INDUSTRY_SEED_QUERIES = (
    ("legal", "legal"),
    ("healthcare", "healthcare"),
    ("finance", "finance"),
    ("banking", "bank transaction"),
    ("insurance", "insurance"),
    ("accounting", "accounting"),
    ("retail", "retail"),
    ("ecommerce", "ecommerce"),
    ("manufacturing", "manufacturing"),
    ("logistics", "logistics"),
    ("transportation", "transportation"),
    ("real-estate", "real estate"),
    ("construction", "construction"),
    ("energy", "energy"),
    ("utilities", "utilities"),
    ("telecommunications", "telecommunications"),
    ("technology", "software"),
    ("cybersecurity", "cybersecurity"),
    ("education", "education"),
    ("research", "research"),
    ("media", "news"),
    ("publishing", "publishing"),
    ("marketing", "marketing"),
    ("sales", "sales"),
    ("customer-service", "customer support"),
    ("human-resources", "employee"),
    ("recruitment", "recruitment"),
    ("travel", "travel"),
    ("hospitality", "hospitality"),
    ("food-service", "restaurant"),
    ("government", "government"),
    ("nonprofit", "fundraising"),
    ("automotive", "automotive"),
    ("agriculture", "farm"),
    ("pharmaceuticals", "pharmaceutical"),
    ("biotechnology", "biotechnology"),
    ("environment", "weather"),
    ("sports", "sports"),
    ("entertainment", "movie"),
    ("personal-productivity", "productivity"),
)

INDUSTRY_BY_KEY = {industry.key: industry for industry in INDUSTRIES}

USE_CASES = (
    UseCase(
        "summarization",
        "Evidence Summary",
        "证据摘要",
        "synthesis",
        "summarize supplied evidence",
        "归纳所提供的证据",
        "produce a concise evidence-grounded summary",
        (
            "summary",
            "summarize",
            "summarization",
            "digest",
            "brief",
            "meeting minutes",
            "meeting summary",
            "newsletter summarization",
            "总结",
            "摘要",
            "要約",
            "検索と要約",
        ),
    ),
    UseCase(
        "question-answering",
        "Grounded Q&A",
        "有据问答",
        "synthesis",
        "answer questions from supplied material",
        "依据所提供的材料回答问题",
        "answer questions without inventing unsupported facts",
        (
            "question",
            "answer",
            "chat",
            "chatbot",
            "qa",
            "q&a",
            "问答",
            "聊天机器人",
            "チャットボット",
        ),
    ),
    UseCase(
        "research",
        "Focused Research",
        "专题研究",
        "analysis",
        "research a focused topic",
        "研究一个明确主题",
        "analyze the supplied research evidence and uncertainty",
        (
            "research",
            "research agent",
            "market research",
            "deepresearch",
            "investigate",
            "study",
            "insight",
            "aggregator",
            "研究",
            "投研",
            "科研",
            "リサーチ",
        ),
    ),
    UseCase(
        "extraction",
        "Structured Extraction",
        "结构化抽取",
        "extraction",
        "extract supported fields",
        "抽取有依据的字段",
        "extract explicit facts into a clear structure",
        ("extract", "extraction", "parse", "reader", "scraper", "ocr", "field", "抽取", "提取"),
    ),
    UseCase(
        "classification",
        "Explainable Classification",
        "可解释分类",
        "routing",
        "classify incoming material",
        "对输入材料进行分类",
        "classify input with an explicit rationale",
        (
            "classify",
            "classification",
            "question classifier",
            "categorize",
            "label",
            "分类",
            "分類",
        ),
    ),
    UseCase(
        "routing",
        "Request Routing",
        "请求分流",
        "routing",
        "route requests by policy",
        "按照规则分流请求",
        "recommend a policy-aligned destination and priority",
        ("route", "router", "routing", "triage", "assign", "路由", "分流", "ルーター"),
    ),
    UseCase(
        "lead-generation",
        "Lead Discovery",
        "线索发现",
        "extraction",
        "identify qualified leads",
        "识别合格线索",
        "identify and structure supported prospect signals",
        ("lead", "prospect", "contact", "business email", "线索", "潜在客户"),
    ),
    UseCase(
        "outreach",
        "Outreach Drafting",
        "外联起草",
        "transformation",
        "draft contextual outreach",
        "起草有上下文的外联内容",
        "transform supplied context into a tailored outreach draft",
        (
            "outreach",
            "email",
            "email draft",
            "email generator",
            "auto reply",
            "message",
            "follow-up",
            "外联",
        ),
    ),
    UseCase(
        "content-creation",
        "Content Development",
        "内容创作",
        "transformation",
        "develop audience-ready content",
        "生成面向受众的内容",
        "transform supplied ideas into audience-ready content",
        (
            "content",
            "write",
            "writing",
            "writer",
            "copy",
            "post",
            "script",
            "generator",
            "creator",
            "builder",
            "yaml",
            "yml",
            "sql",
            "quiz generator",
            "shorts generator",
            "poster",
            "脚本",
            "内容创作",
            "生成全文",
            "写作",
            "生成器",
            "クリエイター",
        ),
    ),
    UseCase(
        "social-publishing",
        "Social Publishing",
        "社媒发布",
        "transformation",
        "prepare social publishing material",
        "准备社交媒体发布材料",
        "adapt supplied material for controlled social publishing",
        (
            "social",
            "publish",
            "auto post",
            "post tiktok",
            "scheduled post",
            "instagram",
            "tiktok",
            "youtube",
            "发布",
            "配信",
        ),
    ),
    UseCase(
        "translation",
        "Terminology-Aware Translation",
        "术语一致翻译",
        "transformation",
        "translate while preserving meaning",
        "在保留含义的前提下翻译",
        "translate supplied text while preserving facts and terminology",
        (
            "translate",
            "translator",
            "translation",
            "file translation",
            "converter",
            "language",
            "localize",
            "翻译",
            "翻訳",
            "转换器",
            "変換機",
        ),
    ),
    UseCase(
        "document-processing",
        "Document Processing",
        "文档处理",
        "extraction",
        "process business documents",
        "处理业务文档",
        "extract and organize supported document information",
        ("document", "pdf", "file", "form", "document processing", "文档", "文件"),
    ),
    UseCase(
        "invoice-processing",
        "Invoice Review",
        "发票审核",
        "extraction",
        "review invoice information",
        "审核发票信息",
        "extract and check explicit invoice fields",
        (
            "invoice",
            "receipt",
            "billing",
            "payment",
            "发票",
            "发票解析",
            "請求書",
        ),
    ),
    UseCase(
        "customer-support",
        "Support Resolution",
        "支持处理",
        "routing",
        "triage support requests",
        "分诊支持请求",
        "route support requests and draft a bounded response",
        (
            "support",
            "ticket",
            "customer service",
            "helpdesk",
            "客服",
            "客户支持",
            "サポート",
            "受付チャットボット",
        ),
    ),
    UseCase(
        "knowledge-retrieval",
        "Knowledge Retrieval",
        "知识检索",
        "synthesis",
        "retrieve relevant knowledge",
        "检索相关知识",
        "synthesize only the relevant supplied knowledge",
        (
            "knowledge",
            "rag",
            "retrieval",
            "search",
            "知识库",
            "搜索",
            "检索",
            "検索",
        ),
    ),
    UseCase(
        "report-generation",
        "Report Assembly",
        "报告生成",
        "synthesis",
        "assemble a decision-ready report",
        "整理可供决策的报告",
        "assemble supplied findings into a structured report",
        (
            "report",
            "dashboard",
            "analysis report",
            "research report",
            "presentation",
            "presentation generator",
            "报告",
            "分析报告",
            "日报",
            "レポート",
        ),
    ),
    UseCase(
        "monitoring",
        "Signal Monitoring",
        "信号监测",
        "analysis",
        "monitor material signals",
        "监测材料中的关键信号",
        "analyze supplied observations for meaningful changes",
        (
            "monitor",
            "tracking",
            "tracker",
            "scanner",
            "aggregator",
            "watch",
            "detect",
            "监测",
            "监控",
            "跟踪",
            "追踪",
            "トラッカー",
            "スキャナー",
        ),
    ),
    UseCase(
        "alerting",
        "Priority Alerting",
        "优先级告警",
        "routing",
        "prioritize detected events",
        "确定事件优先级",
        "classify events and recommend an explainable alert level",
        (
            "alert",
            "notify",
            "notification",
            "warning",
            "告警",
            "警报",
            "提醒",
            "通知",
            "アラート",
        ),
    ),
    UseCase(
        "data-enrichment",
        "Data Enrichment",
        "数据补全",
        "extraction",
        "enrich structured records",
        "补全结构化记录",
        "derive supported enrichment fields from supplied records",
        ("enrich", "enrichment", "profile", "lookup", "补全", "富化"),
    ),
    UseCase(
        "scheduling",
        "Schedule Coordination",
        "日程协调",
        "routing",
        "coordinate scheduling requests",
        "协调日程请求",
        "recommend a schedule using explicit constraints",
        (
            "schedule",
            "calendar",
            "meeting",
            "appointment",
            "日程",
            "预约",
            "会议",
            "スケジュール",
        ),
    ),
    UseCase(
        "analysis",
        "Evidence Analysis",
        "证据分析",
        "analysis",
        "analyze evidence and risks",
        "分析证据与风险",
        "identify findings, risks, and uncertainty in supplied evidence",
        (
            "analyze",
            "analyzer",
            "analysis",
            "data analysis",
            "sentiment analysis",
            "report analyzer",
            "compare",
            "evaluate",
            "evaluation",
            "分析",
            "评估",
            "检测",
            "解析器",
            "感情分析",
            "レビュー分析",
            "アナライザー",
        ),
    ),
    UseCase(
        "compliance-review",
        "Compliance Review",
        "合规审查",
        "analysis",
        "review policy compliance",
        "审查规则符合性",
        "assess supplied material against explicit policy criteria",
        ("compliance", "audit", "policy", "risk", "合规", "合规检测", "审计", "監査"),
    ),
    UseCase(
        "onboarding",
        "Guided Onboarding",
        "引导式入门",
        "transformation",
        "prepare onboarding guidance",
        "准备入门指引",
        "transform supplied procedures into clear onboarding guidance",
        (
            "onboarding",
            "tutorial",
            "guide",
            "training",
            "入门",
            "教程",
            "课程",
            "培训",
        ),
    ),
    UseCase(
        "recommendation",
        "Bounded Recommendation",
        "有界建议",
        "synthesis",
        "form a bounded recommendation",
        "形成有边界的建议",
        "synthesize options into a recommendation with stated assumptions",
        (
            "recommend",
            "recommendation",
            "suggest",
            "plan",
            "planner",
            "optimizer",
            "推荐",
            "规划",
            "决策",
            "顾问",
            "志愿填报",
            "プランナー",
        ),
    ),
    UseCase(
        "data-synchronization",
        "Record Synchronization",
        "记录同步",
        "transformation",
        "prepare records for synchronization",
        "准备记录同步",
        "normalize supplied records for a controlled synchronization step",
        (
            "sync",
            "synchronize",
            "upsert",
            "migration",
            "transfer",
            "update",
            "同步",
            "迁移",
            "转存",
            "転送",
        ),
    ),
    UseCase(
        "execution-planning",
        "Controlled Execution Planning",
        "受控执行规划",
        "routing",
        "plan a bounded execution sequence",
        "规划有边界的执行步骤",
        "prepare an explicit execution plan without performing external actions",
        (
            "executor",
            "action executor",
            "interpreter",
            "trigger",
            "automation flow",
            "workflow automation",
            "执行器",
            "自动化流程",
            "インタープリター",
        ),
    ),
)

CROSS_INDUSTRY = Industry("cross-industry", "Cross-Industry", "跨行业", ())
GENERAL_USE_CASE = UseCase(
    "general-assistance",
    "General Workflow Assistance",
    "通用工作流辅助",
    "synthesis",
    "organize supplied material into a useful result",
    "把所提供的材料整理为可用结果",
    "organize supplied material without claiming unsupported domain semantics",
    (),
)

BRAND_TERMS = frozenset(
    {
        "airtable",
        "airtop",
        "anthropic",
        "apify",
        "asana",
        "aws",
        "azure",
        "blotato",
        "bright",
        "browserbase",
        "browserless",
        "calendly",
        "chatgpt",
        "claude",
        "clickup",
        "cloudflare",
        "cloudinary",
        "cohere",
        "deepseek",
        "dify",
        "discord",
        "dropbox",
        "edgeone",
        "elevenlabs",
        "facebook",
        "fal",
        "firecrawl",
        "flux",
        "gemini",
        "gemma",
        "github",
        "gitlab",
        "gmail",
        "google",
        "gpt",
        "groq",
        "hubspot",
        "huggingface",
        "instagram",
        "jira",
        "kling",
        "kucoin",
        "langchain",
        "langgraph",
        "linear",
        "linkedin",
        "llama",
        "mailchimp",
        "meta",
        "microsoft",
        "midjourney",
        "mistral",
        "mixtral",
        "mongodb",
        "mysql",
        "n8n",
        "newsapi",
        "notion",
        "ollama",
        "openai",
        "perplexity",
        "pinecone",
        "postgres",
        "postgresql",
        "qdrant",
        "qwen",
        "rapidwa",
        "rapidapi",
        "rapiwa",
        "reddit",
        "replicate",
        "retell",
        "salesforce",
        "seedance",
        "sendgrid",
        "serpapi",
        "serper",
        "shopify",
        "slack",
        "spotify",
        "stripe",
        "supabase",
        "tavily",
        "telegram",
        "tiktok",
        "trello",
        "twilio",
        "vapi",
        "veo",
        "whatsapp",
        "wordpress",
        "xai",
        "youtube",
        "zapier",
        "zoom",
    }
)
VERSIONED_BRAND_PREFIXES = (
    "chatgpt",
    "claude",
    "deepseek",
    "gemini",
    "gemma",
    "gpt",
    "llama",
    "mistral",
    "mixtral",
    "notion",
    "openai",
    "qwen",
    "shopify",
    "tiktok",
    "veo",
    "youtube",
)
TOPIC_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "auto",
        "automate",
        "automated",
        "automation",
        "based",
        "build",
        "builder",
        "building",
        "by",
        "create",
        "creating",
        "for",
        "from",
        "generate",
        "how",
        "in",
        "into",
        "like",
        "multi",
        "of",
        "on",
        "or",
        "powered",
        "the",
        "to",
        "using",
        "via",
        "with",
        "without",
        "workflow",
        "workflows",
        "your",
        "ai",
        "agent",
        "agents",
        "bot",
        "assistant",
        "api",
        "advanced",
        "complete",
        "fully",
        "simple",
        "smart",
    }
)
TOKEN_ALIASES = {
    "alerts": "alert",
    "analyzers": "analyzer",
    "articles": "article",
    "candidates": "candidate",
    "classifier": "classify",
    "classifiers": "classify",
    "customers": "customer",
    "documents": "document",
    "docs": "document",
    "emails": "email",
    "extractor": "extract",
    "extractors": "extract",
    "invoices": "invoice",
    "leads": "lead",
    "messages": "message",
    "notifications": "notification",
    "orders": "order",
    "parser": "parse",
    "parsers": "parse",
    "plans": "plan",
    "posters": "poster",
    "posts": "post",
    "reports": "report",
    "replies": "reply",
    "resumes": "resume",
    "scanners": "scanner",
    "summaries": "summary",
    "summarizer": "summary",
    "summarizers": "summary",
    "tickets": "ticket",
    "trackers": "tracker",
    "videos": "video",
    "webpages": "webpage",
}
FOCUS_ADJECTIVES = (
    "Evidence",
    "Priority",
    "Context",
    "Quality",
    "Risk",
    "Insight",
    "Signal",
    "Record",
    "Policy",
    "Service",
    "Outcome",
    "Intake",
    "Knowledge",
    "Response",
    "Decision",
    "Review",
)
FOCUS_NOUNS = (
    "Lens",
    "Desk",
    "Brief",
    "Map",
    "Guide",
    "Monitor",
    "Review",
    "Path",
    "Frame",
    "Digest",
    "Queue",
    "Compass",
    "Ledger",
    "Board",
    "Lane",
    "Profile",
)

_WORD_RE = re.compile(r"[a-z][a-z0-9]{1,31}|[\u3400-\u9fff]{2,12}", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_EAST_ASIAN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogGenerationError(f"{label} must be a string-keyed object")
    return value


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise CatalogGenerationError(f"{label} must be a list")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CatalogGenerationError(f"{label} must be a non-empty text value")
    return value.strip()


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogGenerationError(f"{label} must be a non-negative integer")
    return value


def _popularity(value: object, label: str) -> int:
    """Normalize an officially unreported popularity value to zero."""

    if value is None:
        return 0
    return _integer(value, label)


def _categories(value: object, label: str) -> tuple[str, ...]:
    raw = _sequence(value, label)
    result = tuple(_text(item, f"{label}[]") for item in raw)
    return tuple(dict.fromkeys(result))


def _decode_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogGenerationError(f"{label} did not return UTF-8") from exc

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise CatalogGenerationError(f"{label} returned duplicate JSON keys")
            output[key] = value
        return output

    try:
        value = json.loads(text, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CatalogGenerationError(f"{label} did not return valid JSON") from exc
    return _mapping(value, label)


def _validate_metadata_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_METADATA_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise CatalogGenerationError("metadata URL must use an allowlisted HTTPS host")


class _MetadataRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        resolved = urljoin(req.full_url, newurl)
        _validate_metadata_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def _request_json(
    url: str,
    *,
    body: dict[str, object] | None = None,
    timeout: float,
) -> dict[str, Any]:
    _validate_metadata_url(url)
    headers = {
        "Accept": "application/json",
        "User-Agent": "rag-spine-catalog-generator/1.0",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    opener = build_opener(_MetadataRedirectHandler())

    for attempt in range(MAX_ATTEMPTS):
        try:
            with opener.open(request, timeout=timeout) as response:
                _validate_metadata_url(response.geturl())
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise CatalogGenerationError(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
            return _decode_json(payload, url)
        except HTTPError as exc:
            transient = exc.code == 429 or 500 <= exc.code < 600
            exc.close()
            if not transient or attempt + 1 == MAX_ATTEMPTS:
                raise CatalogGenerationError(
                    f"metadata request failed with HTTP {exc.code}"
                ) from exc
        except URLError as exc:
            if attempt + 1 == MAX_ATTEMPTS:
                raise CatalogGenerationError("metadata request failed after retries") from exc
        time.sleep(2**attempt)
    raise AssertionError("unreachable retry state")


def fetch_dify_metadata(*, timeout: float) -> tuple[SourceItem, ...]:
    """Fetch and project all Dify template search rows onto the allowlist."""

    page = 1
    total: int | None = None
    items: dict[str, SourceItem] = {}
    while total is None or len(items) < total:
        document = _request_json(
            DIFY_SEARCH_URL,
            body={
                "page": page,
                "page_size": DIFY_PAGE_SIZE,
                "sort_by": "usage_count",
                "sort_order": "DESC",
            },
            timeout=timeout,
        )
        data = _mapping(document.get("data"), "Dify data")
        current_total = _integer(data.get("total"), "Dify total")
        if total is None:
            total = current_total
        elif current_total != total:
            raise CatalogGenerationError("Dify total changed while paging")
        raw_templates = _sequence(data.get("templates"), "Dify templates")
        if not raw_templates and len(items) < total:
            raise CatalogGenerationError("Dify pagination ended before total")
        for index, value in enumerate(raw_templates):
            raw = _mapping(value, f"Dify templates[{index}]")
            upstream_id = _text(raw.get("id"), "Dify id")
            title = _text(raw.get("template_name"), "Dify title")
            author = _text(raw.get("publisher_handle"), "Dify author")
            item = SourceItem(
                provider="dify",
                upstream_id=upstream_id,
                title=title,
                categories=_categories(raw.get("categories"), "Dify categories"),
                author=author,
                popularity=_popularity(raw.get("usage_count"), "Dify usage_count"),
                popularity_metric="usage_count",
                url=(
                    "https://marketplace.dify.ai/template/"
                    f"{quote(author, safe='')}/{quote(title, safe='')}?"
                    + urlencode(
                        {
                            "creationType": "templates",
                            "language": "en-US",
                            "templateId": upstream_id,
                        }
                    )
                ),
            )
            previous = items.setdefault(upstream_id, item)
            if previous != item:
                raise CatalogGenerationError(f"Dify id changed across pages: {upstream_id}")
        page += 1

    if total != DIFY_EXPECTED_TOTAL or len(items) != DIFY_EXPECTED_TOTAL:
        raise CatalogGenerationError(
            f"Dify snapshot must contain {DIFY_EXPECTED_TOTAL} unique rows; got total={total}, unique={len(items)}"
        )
    missing_legacy = DIFY_LEGACY_SOURCE_IDS.difference(items)
    if missing_legacy:
        raise CatalogGenerationError(f"Dify legacy ids missing: {sorted(missing_legacy)}")
    selected = tuple(
        sorted(
            (item for key, item in items.items() if key not in DIFY_LEGACY_SOURCE_IDS),
            key=lambda item: (-item.popularity, item.upstream_id),
        )
    )
    if len(selected) != DIFY_TARGET:
        raise CatalogGenerationError(f"Dify selection must contain {DIFY_TARGET} rows")
    return selected


def _fetch_n8n_page(
    page: int,
    *,
    category: str | None,
    search: str | None = None,
    rows: int = N8N_PAGE_SIZE,
    timeout: float,
) -> tuple[tuple[SourceItem, ...], int]:
    query: list[tuple[str, str]] = [
        ("rows", str(rows)),
        ("page", str(page)),
        ("sort", "trendingScore:desc"),
    ]
    if category is not None:
        query.append(("category", category))
    if search is not None:
        query.append(("search", search))
    document = _request_json(f"{N8N_SEARCH_URL}?{urlencode(query)}", timeout=timeout)
    total = _integer(document.get("totalWorkflows"), "n8n totalWorkflows")
    raw_workflows = _sequence(document.get("workflows"), "n8n workflows")
    projected: list[SourceItem] = []
    for index, value in enumerate(raw_workflows):
        raw = _mapping(value, f"n8n workflows[{index}]")
        upstream_id = str(_integer(raw.get("id"), "n8n id"))
        raw_user = raw.get("user")
        user = raw_user if isinstance(raw_user, dict) else {}
        raw_author = user.get("name") or user.get("username")
        author = (
            raw_author.strip()
            if isinstance(raw_author, str) and raw_author.strip()
            else "n8n community contributor"
        )
        projected.append(
            SourceItem(
                provider="n8n",
                upstream_id=upstream_id,
                title=_text(raw.get("name"), "n8n title"),
                categories=() if category is None else (category,),
                author=author,
                popularity=_popularity(raw.get("totalViews"), "n8n totalViews"),
                popularity_metric="totalViews",
                url=f"https://n8n.io/workflows/{upstream_id}/",
            )
        )
    return tuple(projected), total


def _fetch_n8n_industry_seeds(*, timeout: float) -> tuple[SourceItem, ...]:
    candidates: dict[str, tuple[SourceItem, ...]] = {}

    def fetch_query(industry_key: str, query: str) -> tuple[str, tuple[SourceItem, ...]]:
        industry = INDUSTRY_BY_KEY[industry_key]
        rows, _ = _fetch_n8n_page(
            1,
            category=None,
            search=query,
            rows=N8N_SEED_PAGE_SIZE,
            timeout=timeout,
        )
        matching = tuple(
            item
            for item in rows
            if item.upstream_id not in N8N_LEGACY_SOURCE_IDS
            and _profile_evidence(item.title, industry.keywords)
        )
        return industry_key, matching

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="catalog-seed") as executor:
        futures = {
            executor.submit(fetch_query, industry_key, query): industry_key
            for industry_key, query in INDUSTRY_SEED_QUERIES
        }
        for future in as_completed(futures):
            industry_key, matching = future.result()
            if matching:
                candidates[industry_key] = matching

    selected: list[SourceItem] = []
    seen: set[str] = set(N8N_LEGACY_SOURCE_IDS)
    for industry_key, _ in INDUSTRY_SEED_QUERIES:
        for item in candidates.get(industry_key, ()):
            if item.upstream_id in seen:
                continue
            selected.append(
                replace(
                    item,
                    sampling_layer="industry-seed",
                    seed_industry=industry_key,
                )
            )
            seen.add(item.upstream_id)
            break
        if len(selected) == N8N_INDUSTRY_SEED_TARGET:
            break

    distinct_industries = {item.seed_industry for item in selected}
    if len(distinct_industries) < N8N_MIN_SEED_INDUSTRIES:
        raise CatalogGenerationError(
            f"n8n industry seeds need {N8N_MIN_SEED_INDUSTRIES} title-backed industries; "
            f"got {len(distinct_industries)}"
        )

    while len(selected) < N8N_INDUSTRY_SEED_TARGET:
        added = False
        for industry_key, _ in INDUSTRY_SEED_QUERIES:
            for item in candidates.get(industry_key, ()):
                if item.upstream_id in seen:
                    continue
                selected.append(
                    replace(
                        item,
                        sampling_layer="industry-seed",
                        seed_industry=industry_key,
                    )
                )
                seen.add(item.upstream_id)
                added = True
                break
            if len(selected) == N8N_INDUSTRY_SEED_TARGET:
                break
        if not added:
            raise CatalogGenerationError("n8n industry seed feeds cannot fill 36 unique rows")
    return tuple(selected)


def fetch_n8n_metadata(*, timeout: float) -> tuple[SourceItem, ...]:
    """Select a 500-row core plus 259 category-stratified rows."""

    industry_seeds = _fetch_n8n_industry_seeds(timeout=timeout)
    seen: set[str] = set(N8N_LEGACY_SOURCE_IDS)
    seen.update(item.upstream_id for item in industry_seeds)
    popular: list[SourceItem] = []
    page = 1
    while len(popular) < N8N_POPULAR_TARGET:
        rows, total = _fetch_n8n_page(page, category=None, timeout=timeout)
        if not rows or (page - 1) * N8N_PAGE_SIZE >= total:
            raise CatalogGenerationError("n8n Popular feed ended before core quota")
        for item in rows:
            if item.upstream_id in seen:
                continue
            seen.add(item.upstream_id)
            popular.append(replace(item, sampling_layer="popular"))
            if len(popular) == N8N_POPULAR_TARGET:
                break
        page += 1

    stratified: list[SourceItem] = []
    for category, quota in N8N_STRATA:
        accepted = 0
        page = 1
        while accepted < quota:
            rows, total = _fetch_n8n_page(page, category=category, timeout=timeout)
            if not rows or (page - 1) * N8N_PAGE_SIZE >= total:
                raise CatalogGenerationError(f"n8n {category} feed ended before quota {quota}")
            for item in rows:
                if item.upstream_id in seen:
                    continue
                seen.add(item.upstream_id)
                stratified.append(replace(item, sampling_layer=f"category:{category}"))
                accepted += 1
                if accepted == quota:
                    break
            page += 1

    if (
        len(industry_seeds) != N8N_INDUSTRY_SEED_TARGET
        or len(popular) != N8N_POPULAR_TARGET
        or len(stratified) != N8N_STRATIFIED_TARGET
    ):
        raise CatalogGenerationError("n8n sample size does not match 36 + 464 + 259")
    selected = tuple(popular) + industry_seeds + tuple(stratified)
    if len(selected) != N8N_TARGET or len({item.upstream_id for item in selected}) != N8N_TARGET:
        raise CatalogGenerationError(f"n8n selection must contain {N8N_TARGET} unique rows")
    if N8N_LEGACY_SOURCE_IDS.intersection(item.upstream_id for item in selected):
        raise CatalogGenerationError("n8n legacy source leaked into selection")
    return selected


def _normalized_search_text(item: SourceItem) -> str:
    return " ".join((item.title, *item.categories)).casefold()


def _semantic_tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("q&a", " qa ")
    return tuple(
        TOKEN_ALIASES.get(match.group(0), match.group(0)) for match in _WORD_RE.finditer(normalized)
    )


def _profile_evidence(text: str, keywords: Sequence[str]) -> tuple[str, ...]:
    normalized_text = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", text).casefold(),
    )
    tokens = _semantic_tokens(text)
    evidence: list[str] = []
    for keyword in keywords:
        if _EAST_ASIAN_RE.search(keyword):
            normalized_keyword = re.sub(
                r"\s+",
                "",
                unicodedata.normalize("NFKC", keyword).casefold(),
            )
            if normalized_keyword and normalized_keyword in normalized_text:
                evidence.append(keyword)
            continue
        keyword_tokens = _semantic_tokens(keyword)
        if not keyword_tokens or len(keyword_tokens) > len(tokens):
            continue
        width = len(keyword_tokens)
        if any(
            tokens[index : index + width] == keyword_tokens
            for index in range(len(tokens) - width + 1)
        ):
            evidence.append(keyword)
    return tuple(evidence)


def _profile_score(text: str, keywords: Sequence[str]) -> int:
    score = 0
    for keyword in _profile_evidence(text, keywords):
        if _EAST_ASIAN_RE.search(keyword):
            normalized_keyword = re.sub(
                r"\s+",
                "",
                unicodedata.normalize("NFKC", keyword),
            )
            score += len(normalized_keyword)
        else:
            token_count = len(_semantic_tokens(keyword))
            score += token_count * token_count
    return score


def _first_profile_evidence_position(text: str, keywords: Sequence[str]) -> int | None:
    """Return the first controlled keyword position, preserving title action order."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    compact = re.sub(r"\s+", "", normalized)
    token_text = normalized.replace("q&a", " qa ")
    token_matches = tuple(_WORD_RE.finditer(token_text))
    tokens = tuple(TOKEN_ALIASES.get(match.group(0), match.group(0)) for match in token_matches)
    positions: list[int] = []
    for keyword in _profile_evidence(text, keywords):
        if _EAST_ASIAN_RE.search(keyword):
            normalized_keyword = re.sub(
                r"\s+",
                "",
                unicodedata.normalize("NFKC", keyword).casefold(),
            )
            position = compact.find(normalized_keyword)
            if position >= 0:
                positions.append(position)
            continue
        keyword_tokens = _semantic_tokens(keyword)
        width = len(keyword_tokens)
        for index in range(len(tokens) - width + 1):
            if tokens[index : index + width] == keyword_tokens:
                positions.append(token_matches[index].start())
                break
    return min(positions) if positions else None


def _best_profile(
    item: SourceItem,
    profiles: Sequence[Industry] | Sequence[UseCase],
    *,
    preferred_key: str | None = None,
    title_only: bool = False,
    prefer_title: bool = False,
) -> tuple[Industry | UseCase | None, tuple[str, ...], str]:
    # Marketplace categories describe broad workflow functions, not necessarily
    # the subject industry.  Keep industry assignment anchored to the title;
    # functional categories remain useful evidence for use-case classification.
    title_text = item.title.casefold()
    category_text = " ".join(item.categories).casefold()
    combined_text = _normalized_search_text(item)

    def evidence_for(profile: Industry | UseCase) -> tuple[str, ...]:
        if title_only:
            return _profile_evidence(title_text, profile.keywords)
        if prefer_title:
            return tuple(
                dict.fromkeys(
                    (
                        *_profile_evidence(title_text, profile.keywords),
                        *_profile_evidence(category_text, profile.keywords),
                    )
                )
            )
        return _profile_evidence(combined_text, profile.keywords)

    title_scores = [_profile_score(title_text, profile.keywords) for profile in profiles]
    category_scores = [_profile_score(category_text, profile.keywords) for profile in profiles]
    combined_scores = [_profile_score(combined_text, profile.keywords) for profile in profiles]

    if preferred_key is not None:
        preferred = next(
            (index for index, profile in enumerate(profiles) if profile.key == preferred_key),
            None,
        )
        if preferred is not None and title_scores[preferred] > 0:
            profile = profiles[preferred]
            return profile, evidence_for(profile), "seed"

    if title_only:
        best_score = max(title_scores)
        if best_score == 0:
            return None, (), "zero"
        winners = [index for index, score in enumerate(title_scores) if score == best_score]
        # Research is a generic activity layer.  If a title supplies equally
        # strong evidence for a concrete domain, retain the concrete domain.
        if any(profiles[index].key == "research" for index in winners):
            domain_winners = [index for index in winners if profiles[index].key != "research"]
            if domain_winners:
                winners = domain_winners
    elif prefer_title:
        best_title_score = max(title_scores)
        if best_title_score > 0:
            # Categories may only resolve candidates already supported by the
            # title; they can never introduce an unrelated third profile.
            winners = [
                index for index, score in enumerate(title_scores) if score == best_title_score
            ]
            best_category_score = max(category_scores[index] for index in winners)
            winners = [index for index in winners if category_scores[index] == best_category_score]
            if len(winners) > 1:
                positions = {
                    index: _first_profile_evidence_position(
                        title_text,
                        profiles[index].keywords,
                    )
                    for index in winners
                }
                earliest = min(position for position in positions.values() if position is not None)
                earliest_winners = [index for index in winners if positions[index] == earliest]
                if len(earliest_winners) == 1:
                    winners = earliest_winners
        else:
            best_category_score = max(category_scores)
            if best_category_score == 0:
                return None, (), "zero"
            winners = [
                index for index, score in enumerate(category_scores) if score == best_category_score
            ]
    else:
        best_score = max(combined_scores)
        if best_score == 0:
            return None, (), "zero"
        winners = [index for index, score in enumerate(combined_scores) if score == best_score]

    if len(winners) != 1:
        evidence = tuple(
            sorted({keyword for index in winners for keyword in evidence_for(profiles[index])})
        )
        return None, evidence, "tie"
    profile = profiles[winners[0]]
    evidence = evidence_for(profile)
    if isinstance(profile, Industry) and evidence == ("support",):
        return None, evidence, "ambiguous"
    return profile, evidence, "matched"


def _stable_rank(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _classify_item(item: SourceItem) -> Classification:
    """Classify one item without applying catalog-wide coverage requirements."""

    industry, industry_evidence, industry_status = _best_profile(
        item,
        INDUSTRIES,
        preferred_key=item.seed_industry,
        title_only=True,
    )
    use_case, use_case_evidence, use_case_status = _best_profile(
        item,
        USE_CASES,
        prefer_title=True,
    )
    return Classification(
        industry=CROSS_INDUSTRY if industry is None else industry,
        use_case=GENERAL_USE_CASE if use_case is None else use_case,
        industry_evidence=industry_evidence,
        use_case_evidence=use_case_evidence,
        industry_status=industry_status,
        use_case_status=use_case_status,
    )


def _classify_items(
    items: Sequence[SourceItem],
) -> tuple[Classification, ...]:
    classifications = [_classify_item(item) for item in items]
    forced_without_evidence = sum(
        (classification.industry is not CROSS_INDUSTRY and not classification.industry_evidence)
        or (
            classification.use_case is not GENERAL_USE_CASE and not classification.use_case_evidence
        )
        for classification in classifications
    )
    if forced_without_evidence:
        raise CatalogGenerationError(
            f"classifications without semantic evidence: {forced_without_evidence}"
        )
    semantic_industries = {
        classification.industry.key
        for classification in classifications
        if classification.industry is not CROSS_INDUSTRY
    }
    semantic_use_cases = {
        classification.use_case.key
        for classification in classifications
        if classification.use_case is not GENERAL_USE_CASE
    }
    if len(semantic_industries) < 30 or len(semantic_use_cases) < 20:
        raise CatalogGenerationError(
            "evidence-backed taxonomy coverage must reach 30 industries and 20 use cases; "
            f"got {len(semantic_industries)} and {len(semantic_use_cases)}"
        )
    return tuple(classifications)


def _classification_report(
    items: Sequence[SourceItem],
    classifications: Sequence[Classification],
) -> dict[str, object]:
    industry_status = Counter(item.industry_status for item in classifications)
    use_case_status = Counter(item.use_case_status for item in classifications)
    semantic_industries = {
        item.industry.key for item in classifications if item.industry is not CROSS_INDUSTRY
    }
    semantic_use_cases = {
        item.use_case.key for item in classifications if item.use_case is not GENERAL_USE_CASE
    }
    samples: list[dict[str, object]] = []
    sampled_industries: set[str] = set()
    for source, classification in zip(items, classifications, strict=True):
        if classification.industry is CROSS_INDUSTRY:
            continue
        if classification.industry.key in sampled_industries:
            continue
        sampled_industries.add(classification.industry.key)
        samples.append(
            {
                "source": f"{source.provider}:{source.upstream_id}",
                "title": source.title,
                "industry": classification.industry.key,
                "industry_evidence": list(classification.industry_evidence),
                "use_case": classification.use_case.key,
                "use_case_evidence": list(classification.use_case_evidence),
            }
        )
        if len(samples) == 10:
            break
    return {
        "industry_status": dict(sorted(industry_status.items())),
        "use_case_status": dict(sorted(use_case_status.items())),
        "forced_without_evidence": 0,
        "evidence_backed_industries": len(semantic_industries),
        "evidence_backed_use_cases": len(semantic_use_cases),
        "samples": samples,
    }


def _topic_keywords(title: str, industry: Industry, use_case: UseCase) -> tuple[str, ...]:
    topics: list[str] = []
    for match in _WORD_RE.finditer(title.casefold()):
        token = match.group(0).strip(".-+")
        token = TOKEN_ALIASES.get(token, token)
        if len(token) < 2 or _is_brand_token(token) or token in TOPIC_STOPWORDS or token.isdigit():
            continue
        if token not in topics:
            topics.append(token)
        if len(topics) == 4:
            break
    if not topics:
        topics = [industry.key.replace("-", " "), use_case.key.replace("-", " ")]
    return tuple(topics)


def _is_brand_token(token: str) -> bool:
    head = re.split(r"[+.-]", token, maxsplit=1)[0]
    compact = re.sub(r"[^a-z0-9]", "", token)
    return (
        token in BRAND_TERMS
        or head in BRAND_TERMS
        or any(compact.startswith(prefix) for prefix in VERSIONED_BRAND_PREFIXES)
    )


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return slug or "topic"


def _descriptor_id(
    item: SourceItem, industry: Industry, use_case: UseCase, topics: tuple[str, ...]
) -> str:
    digest = _stable_rank(f"{item.provider}:{item.upstream_id}")[:12]
    prefix = _slug("-".join((item.provider, industry.key, use_case.key, *topics[:2])))
    prefix = prefix[: 63 - len(digest) - 1].rstrip("-") or item.provider
    return f"{prefix}-{digest}"


def _focus_phrase(item: SourceItem, attempt: int = 0) -> str:
    digest = bytes.fromhex(_stable_rank(f"{item.provider}:{item.upstream_id}:{attempt}"))
    adjective = FOCUS_ADJECTIVES[digest[0] % len(FOCUS_ADJECTIVES)]
    noun = FOCUS_NOUNS[digest[1] % len(FOCUS_NOUNS)]
    return f"{adjective} {noun}"


def _unique_name(
    item: SourceItem,
    industry: Industry,
    use_case: UseCase,
    topics: tuple[str, ...],
    used_names: set[str],
) -> tuple[str, str]:
    topic_label = " ".join(topics[:3]).title()
    base = f"{industry.label_en} {use_case.label_en}: {topic_label}"
    if base.casefold() not in used_names:
        used_names.add(base.casefold())
        return base, ""
    for attempt in range(len(FOCUS_ADJECTIVES) * len(FOCUS_NOUNS)):
        focus = _focus_phrase(item, attempt)
        candidate = f"{base} — {focus}"
        if candidate.casefold() not in used_names:
            used_names.add(candidate.casefold())
            return candidate, focus
    raise CatalogGenerationError(
        f"could not make unique name for {item.provider}:{item.upstream_id}"
    )


def _build_descriptor(
    item: SourceItem,
    industry: Industry,
    use_case: UseCase,
    *,
    observed_at: str,
    used_names: set[str],
) -> dict[str, object]:
    topics = _topic_keywords(item.title, industry, use_case)
    topic_phrase = ", ".join(topics)
    name, focus = _unique_name(item, industry, use_case, topics, used_names)
    focus_clause = "" if not focus else f" with a {focus.casefold()} focus"
    description = (
        f"A Spine-authored {use_case.archetype} workflow for "
        f"{industry.label_en.casefold()} teams to {use_case.goal_en}, "
        f"centered on {topic_phrase}{focus_clause}."
    )
    goal = (
        f"Help {industry.label_en.casefold()} teams {use_case.goal_en} for supplied material "
        f"centered on {topic_phrase}{focus_clause}; preserve evidence and mark missing information."
    )
    intents = [
        f"{use_case.intent_en.capitalize()} for {industry.label_en} work about {topic_phrase}.",
        f"在{industry.label_zh}场景中{use_case.intent_zh}，主题关键词为{topic_phrase}。",
    ]
    examples = [
        f"Create a {use_case.label_en.casefold()} workflow for {industry.label_en.casefold()} material about {topic_phrase}.",
        f"为{industry.label_zh}创建{use_case.label_zh}工作流，处理主题关键词{topic_phrase}。",
    ]
    return {
        "id": _descriptor_id(item, industry, use_case, topics),
        "name": name,
        "description": description,
        "categories": [
            f"industry:{industry.key}",
            f"use-case:{use_case.key}",
            f"archetype:{use_case.archetype}",
        ],
        "tags": list(
            dict.fromkeys(
                (
                    industry.key,
                    use_case.key,
                    use_case.archetype,
                    *(f"topic:{topic.casefold()}" for topic in topics),
                )
            )
        ),
        "intents": intents,
        "examples": examples,
        "archetype": use_case.archetype,
        "goal": goal,
        "source": {
            "provider": item.provider,
            "title": item.title,
            "author": item.author,
            "upstream_id": item.upstream_id,
            "upstream_url": item.url,
            "license_status": "unknown-not-redistributed",
            "observed_metric": item.popularity_metric,
            "observed_value": item.popularity,
            "observed_at": observed_at,
        },
    }


def build_catalog(
    dify_items: Sequence[SourceItem],
    n8n_items: Sequence[SourceItem],
    *,
    observed_at: str,
) -> dict[str, object]:
    """Build the deterministic Spine-authored descriptor document."""

    if len(dify_items) != DIFY_TARGET or len(n8n_items) != N8N_TARGET:
        raise CatalogGenerationError("source item counts do not match 234 + 759")
    items = tuple(dify_items) + tuple(n8n_items)
    classifications = _classify_items(items)
    used_names: set[str] = set()
    templates = [
        _build_descriptor(
            item,
            classification.industry,
            classification.use_case,
            observed_at=observed_at,
            used_names=used_names,
        )
        for item, classification in zip(items, classifications, strict=True)
    ]
    document: dict[str, object] = {"schema_version": 1, "templates": templates}
    _validate_catalog(document)
    return document


def _validate_catalog(document: dict[str, object]) -> None:
    templates = _sequence(document.get("templates"), "catalog templates")
    if len(templates) != CATALOG_TARGET:
        raise CatalogGenerationError(f"catalog must contain {CATALOG_TARGET} templates")
    rows = [_mapping(value, "catalog template") for value in templates]
    ids = [_text(row.get("id"), "descriptor id") for row in rows]
    names = [_text(row.get("name"), "descriptor name") for row in rows]
    if (
        len(set(ids)) != CATALOG_TARGET
        or len({name.casefold() for name in names}) != CATALOG_TARGET
    ):
        raise CatalogGenerationError("descriptor ids and names must be unique")

    provider_counts: Counter[str] = Counter()
    industries: set[str] = set()
    use_cases: set[str] = set()
    archetypes: set[str] = set()
    for row in rows:
        source = _mapping(row.get("source"), "descriptor source")
        provider_counts[_text(source.get("provider"), "source provider")] += 1
        categories = _sequence(row.get("categories"), "descriptor categories")
        category_text = {_text(value, "descriptor category") for value in categories}
        industries.update(value for value in category_text if value.startswith("industry:"))
        use_cases.update(value for value in category_text if value.startswith("use-case:"))
        archetypes.add(_text(row.get("archetype"), "descriptor archetype"))
        for field in ("intents", "examples"):
            phrases = _sequence(row.get(field), f"descriptor {field}")
            if not any(_ASCII_LETTER_RE.search(_text(value, field)) for value in phrases):
                raise CatalogGenerationError(f"descriptor {field} needs an English phrase")
            if not any(_CJK_RE.search(_text(value, field)) for value in phrases):
                raise CatalogGenerationError(f"descriptor {field} needs a Chinese phrase")
        topic_tags = [
            _text(value, "descriptor tag")[len("topic:") :]
            for value in _sequence(row.get("tags"), "descriptor tags")
            if isinstance(value, str) and value.startswith("topic:")
        ]
        raw_tags = [_text(value, "descriptor tag") for value in row["tags"]]
        if len(raw_tags) != len(set(raw_tags)):
            raise CatalogGenerationError("descriptor tags must not contain duplicates")
        recall_text = " ".join(
            _text(value, "recall phrase").casefold()
            for field in ("intents", "examples")
            for value in _sequence(row.get(field), f"descriptor {field}")
        )
        if not topic_tags or any(tag.casefold() not in recall_text for tag in topic_tags):
            raise CatalogGenerationError(
                "topic keywords must be present in tags, intents, and examples"
            )

    if provider_counts != Counter({"dify": DIFY_TARGET, "n8n": N8N_TARGET}):
        raise CatalogGenerationError(f"unexpected provider counts: {dict(provider_counts)}")
    if len(industries) < 30 or len(use_cases) < 20 or archetypes != set(ARCHETYPES):
        raise CatalogGenerationError(
            "catalog taxonomy must cover at least 30 industries, 20 use cases, and all archetypes"
        )


def _serialize(document: dict[str, object]) -> bytes:
    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(payload) > MAX_OUTPUT_BYTES:
        raise CatalogGenerationError(
            f"generated catalog is {len(payload)} bytes; limit is {MAX_OUTPUT_BYTES}"
        )
    return payload


def _atomic_write(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_observed_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--observed-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--observed-at must include a UTC offset")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the metadata-only Spine workflow descriptor catalog."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--observed-at",
        type=_validate_observed_at,
        default=DEFAULT_OBSERVED_AT,
        help="Fixed ISO-8601 snapshot timestamp; explicit values make builds reproducible.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        raise CatalogGenerationError("--timeout must be positive")
    dify_items = fetch_dify_metadata(timeout=args.timeout)
    n8n_items = fetch_n8n_metadata(timeout=args.timeout)
    source_items = tuple(dify_items) + tuple(n8n_items)
    classifications = _classify_items(source_items)
    classification_report = _classification_report(source_items, classifications)
    document = build_catalog(dify_items, n8n_items, observed_at=args.observed_at)
    payload = _serialize(document)
    if payload != _serialize(document):
        raise CatalogGenerationError("serialization is not deterministic")
    _atomic_write(args.output, payload)

    templates = _sequence(document["templates"], "catalog templates")
    industries = {
        category
        for value in templates
        for category in _mapping(value, "catalog template")["categories"]
        if isinstance(category, str) and category.startswith("industry:")
    }
    use_cases = {
        category
        for value in templates
        for category in _mapping(value, "catalog template")["categories"]
        if isinstance(category, str) and category.startswith("use-case:")
    }
    print(
        json.dumps(
            {
                "output": str(args.output),
                "templates": len(templates),
                "dify": DIFY_TARGET,
                "n8n": N8N_TARGET,
                "n8n_core": N8N_CORE_TARGET,
                "n8n_industry_seeds": N8N_INDUSTRY_SEED_TARGET,
                "n8n_seed_industries": len(
                    {item.seed_industry for item in n8n_items if item.seed_industry}
                ),
                "n8n_popular": N8N_POPULAR_TARGET,
                "n8n_stratified": N8N_STRATIFIED_TARGET,
                "industries": len(industries),
                "use_cases": len(use_cases),
                "archetypes": len(ARCHETYPES),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "classification": classification_report,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
