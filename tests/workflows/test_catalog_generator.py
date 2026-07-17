"""Offline safety and regression checks for the marketplace catalog generator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from urllib.request import Request

import pytest

from ragspine.workflows.generated_catalog import (
    build_workflow_templates,
    load_workflow_descriptors,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = PROJECT_ROOT / "scripts" / "generate_workflow_catalog.py"
CATALOG_PATH = (
    PROJECT_ROOT / "src" / "ragspine" / "workflows" / "templates" / "generated-catalog.json"
)


def _load_generator() -> ModuleType:
    name = "_ragspine_workflow_catalog_generator"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _source_item(
    title: str,
    *,
    categories: tuple[str, ...] = (),
    seed_industry: str | None = None,
) -> object:
    return GENERATOR.SourceItem(
        provider="n8n",
        upstream_id="test-source",
        title=title,
        categories=categories,
        author="Test Author",
        popularity=1,
        popularity_metric="totalViews",
        url="https://n8n.io/workflows/test-source/",
        seed_industry=seed_industry,
    )


def test_metadata_urls_and_redirects_stay_on_official_https_hosts() -> None:
    GENERATOR._validate_metadata_url(GENERATOR.DIFY_SEARCH_URL)
    GENERATOR._validate_metadata_url(GENERATOR.N8N_SEARCH_URL)

    for url in (
        "http://api.n8n.io/api/templates/search",
        "https://api.n8n.io.evil.example/api/templates/search",
        "https://marketplace.dify.ai@evil.example/api/v1/templates/search/advanced",
        "https://marketplace.dify.ai:444/api/v1/templates/search/advanced",
    ):
        with pytest.raises(GENERATOR.CatalogGenerationError):
            GENERATOR._validate_metadata_url(url)

    request = Request(GENERATOR.N8N_SEARCH_URL)
    handler = GENERATOR._MetadataRedirectHandler()
    with pytest.raises(GENERATOR.CatalogGenerationError):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://evil.example/collect",
        )


def test_api_projection_ignores_non_allowlisted_workflow_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison = "DO-NOT-COPY-PRIVATE-WORKFLOW-CONTENT"
    forbidden = {
        "description": poison,
        "overview": poison,
        "readme": poison,
        "nodes": [{"prompt": poison, "credentials": poison}],
        "config": {"api_key": poison},
    }

    monkeypatch.setattr(GENERATOR, "DIFY_EXPECTED_TOTAL", 1)
    monkeypatch.setattr(GENERATOR, "DIFY_TARGET", 1)
    monkeypatch.setattr(GENERATOR, "DIFY_LEGACY_SOURCE_IDS", frozenset())
    monkeypatch.setattr(
        GENERATOR,
        "_request_json",
        lambda *args, **kwargs: {
            "data": {
                "total": 1,
                "templates": [
                    {
                        "id": "dify-1",
                        "template_name": "Invoice Review",
                        "categories": ["Finance"],
                        "publisher_handle": "Publisher",
                        "usage_count": 12,
                        **forbidden,
                    }
                ],
            }
        },
    )
    dify_item = GENERATOR.fetch_dify_metadata(timeout=1.0)[0]

    monkeypatch.setattr(
        GENERATOR,
        "_request_json",
        lambda *args, **kwargs: {
            "totalWorkflows": 1,
            "workflows": [
                {
                    "id": 9,
                    "name": "Weather Brief",
                    "user": {"name": "Contributor"},
                    "totalViews": 34,
                    **forbidden,
                }
            ],
        },
    )
    n8n_item = GENERATOR._fetch_n8n_page(
        1,
        category="Other",
        timeout=1.0,
    )[0][0]

    expected_fields = {
        "provider",
        "upstream_id",
        "title",
        "categories",
        "author",
        "popularity",
        "popularity_metric",
        "url",
        "sampling_layer",
        "seed_industry",
    }
    assert set(dify_item.__dict__) == expected_fields
    assert set(n8n_item.__dict__) == expected_fields
    assert poison not in repr(dify_item)
    assert poison not in repr(n8n_item)


@pytest.mark.parametrize(
    ("text", "keyword"),
    (
        ("marketing", "market"),
        ("card calendar", "car"),
        ("ChatGPT assistant", "chat"),
        ("Postgres database", "post"),
        ("plant monitor", "plan"),
    ),
)
def test_semantic_matching_uses_token_boundaries(text: str, keyword: str) -> None:
    assert GENERATOR._profile_evidence(text, (keyword,)) == ()


def test_generic_or_unproven_seed_items_are_not_forced_into_an_industry() -> None:
    generic = GENERATOR._classify_item(
        _source_item("Quarterly coordination workspace", categories=("Marketing",))
    )
    unproven_seed = GENERATOR._classify_item(
        _source_item("Quarterly coordination workspace", seed_industry="banking")
    )

    assert generic.industry is GENERATOR.CROSS_INDUSTRY
    assert generic.industry_status == "zero"
    assert unproven_seed.industry is GENERATOR.CROSS_INDUSTRY
    assert unproven_seed.industry_status == "zero"


def test_industry_seed_requires_and_preserves_semantic_evidence() -> None:
    classification = GENERATOR._classify_item(
        _source_item(
            "Reconcile bank transactions and generate reports",
            seed_industry="banking",
        )
    )

    assert classification.industry.key == "banking"
    assert classification.industry_status == "seed"
    assert classification.industry_evidence == ("bank",)


@pytest.mark.parametrize(
    ("title", "industry"),
    (
        ("Daily AI News Digest", "media"),
        ("Weather Forecast", "environment"),
    ),
)
def test_known_titles_keep_their_semantic_industry(title: str, industry: str) -> None:
    classification = GENERATOR._classify_item(_source_item(title))

    assert classification.industry.key == industry
    assert classification.industry_status == "matched"
    assert classification.industry_evidence


@pytest.mark.parametrize(
    ("title", "categories", "use_case"),
    (
        ("Daily AI News Digest", ("knowledge",), "summarization"),
        ("Extract conversation information", ("support",), "extraction"),
    ),
)
def test_title_use_case_evidence_outranks_broad_categories(
    title: str,
    categories: tuple[str, ...],
    use_case: str,
) -> None:
    classification = GENERATOR._classify_item(_source_item(title, categories=categories))

    assert classification.use_case.key == use_case
    assert classification.use_case_status == "matched"
    assert classification.use_case_evidence


def test_weather_without_use_case_evidence_remains_general() -> None:
    classification = GENERATOR._classify_item(_source_item("Weather Forecast"))

    assert classification.industry.key == "environment"
    assert classification.use_case is GENERATOR.GENERAL_USE_CASE
    assert classification.use_case_status == "zero"


@pytest.mark.parametrize(
    ("title", "use_case"),
    (
        ("智票通 - 批量发票智能解析", "invoice-processing"),
        ("Jina Reader 总结网站内容", "summarization"),
        ("爆款短视频脚本生成", "content-creation"),
        ("AWS SNS告警", "alerting"),
        ("HR简历评估Agent", "analysis"),
        ("代码转换器", "translation"),
        ("テキストの感情分析", "analysis"),
        ("コード変換機", "translation"),
    ),
)
def test_controlled_east_asian_phrases_classify_by_normalized_substring(
    title: str,
    use_case: str,
) -> None:
    classification = GENERATOR._classify_item(_source_item(title))

    assert classification.use_case.key == use_case
    assert classification.use_case_status == "matched"
    assert classification.use_case_evidence


@pytest.mark.parametrize(
    ("title", "industry"),
    (
        ("智能法律助手", "legal"),
        ("患者受付チャットボット", "healthcare"),
        ("美股投资分析助手", "finance"),
        ("批量发票智能解析", "accounting"),
        ("跨境电商售后助手", "ecommerce"),
        ("物流方案助手", "logistics"),
        ("广东高考志愿填报顾问", "education"),
        ("科研工程化选题与文献平台", "research"),
        ("AIニュース自動配信", "media"),
        ("营销品牌策略引擎", "marketing"),
        ("HR简历评估Agent", "recruitment"),
        ("酒店预订助手", "hospitality"),
        ("旅行规划助手", "travel"),
        ("餐厅菜单生成器", "food-service"),
        ("政务服务工作流", "government"),
        ("汽车维修助手", "automotive"),
        ("农场作物监测", "agriculture"),
        ("制药临床试验审查", "pharmaceuticals"),
        ("环境天气监控", "environment"),
        ("体育比赛简报", "sports"),
        ("电影音乐推荐", "entertainment"),
        ("投資分析レポート", "finance"),
        ("採用面接アシスタント", "recruitment"),
    ),
)
def test_controlled_east_asian_industry_phrases_are_evidence_backed(
    title: str,
    industry: str,
) -> None:
    classification = GENERATOR._classify_item(_source_item(title))

    assert classification.industry.key == industry
    assert classification.industry_status == "matched"
    assert classification.industry_evidence


@pytest.mark.parametrize(
    ("title", "use_case"),
    (
        ("Visual Presentation Generator", "report-generation"),
        ("Agent YML Generator", "content-creation"),
        ("Daily Action Executor Agent", "execution-planning"),
        ("Code Interpreter", "execution-planning"),
        ("Plugin and Webhook Trigger Demo", "execution-planning"),
        ("Human Input: Writing Assistant", "content-creation"),
        ("Code: UTM Link Builder", "content-creation"),
        ("Code Converter", "translation"),
        ("Daily Video Tracker", "monitoring"),
        ("Market Trend Scanner", "monitoring"),
        ("Brand Intelligence Analyzer", "analysis"),
        ("Listing Optimizer", "recommendation"),
        ("Enterprise Project Planner", "recommendation"),
        ("SQL Creator", "content-creation"),
    ),
)
def test_common_english_action_nouns_map_to_controlled_use_cases(
    title: str,
    use_case: str,
) -> None:
    classification = GENERATOR._classify_item(_source_item(title))

    assert classification.use_case.key == use_case
    assert classification.use_case_status == "matched"
    assert classification.use_case_evidence


def test_category_cannot_replace_title_candidates_with_a_third_use_case() -> None:
    classification = GENERATOR._classify_item(
        _source_item(
            "Doc Extractor: AI Summarizer",
            categories=("knowledge",),
        )
    )

    assert classification.use_case.key == "extraction"
    assert classification.use_case_status == "matched"
    assert "extract" in classification.use_case_evidence


def test_earliest_explicit_title_action_breaks_a_true_title_score_tie() -> None:
    classification = GENERATOR._classify_item(
        _source_item(
            "Qdrant Upsert with Google Drive Trigger",
            categories=("knowledge",),
        )
    )

    assert classification.use_case.key == "data-synchronization"
    assert classification.use_case_status == "matched"
    assert "upsert" in classification.use_case_evidence


def test_aggregator_does_not_inherit_unrelated_support_category() -> None:
    classification = GENERATOR._classify_item(
        _source_item("Open-Box Deals Aggregator", categories=("support",))
    )

    assert classification.use_case is GENERATOR.GENERAL_USE_CASE
    assert classification.use_case_status == "tie"
    assert "aggregator" in classification.use_case_evidence


def test_poster_is_controlled_content_creation() -> None:
    classification = GENERATOR._classify_item(
        _source_item("Novel Deconstruction Poster", categories=("knowledge",))
    )

    assert classification.use_case.key == "content-creation"
    assert classification.use_case_status == "matched"
    assert "poster" in classification.use_case_evidence


def test_concrete_industry_wins_an_equal_research_industry_tie() -> None:
    classification = GENERATOR._classify_item(_source_item("Legal Research Agent"))

    assert classification.industry.key == "legal"
    assert classification.industry_status == "matched"
    assert classification.industry_evidence == ("legal",)


@pytest.mark.parametrize(
    "title",
    (
        "Generate and Auto-post AI Videos to Social Media",
        "Edit and post TikTok videos",
        "Automated Content Creation with Scheduled Posts",
    ),
)
def test_explicit_social_posting_phrases_outrank_generic_content(title: str) -> None:
    classification = GENERATOR._classify_item(_source_item(title))

    assert classification.use_case.key == "social-publishing"
    assert classification.use_case_status == "matched"


def test_question_classifier_phrase_is_classification_not_generic_qa() -> None:
    classification = GENERATOR._classify_item(
        _source_item("Question Classifier & Knowledge & Chatbot")
    )

    assert classification.use_case.key == "classification"
    assert classification.use_case_status == "matched"


def test_auto_reply_is_outreach_and_calendar_does_not_force_an_industry() -> None:
    classification = GENERATOR._classify_item(
        _source_item("Send job application auto-replies with Calendar")
    )

    assert classification.industry is GENERATOR.CROSS_INDUSTRY
    assert classification.use_case.key == "outreach"
    assert classification.use_case_status == "matched"


def test_generated_993_catalog_loads_and_builds() -> None:
    descriptors = load_workflow_descriptors(
        CATALOG_PATH.read_bytes(),
        expected_count=993,
    )
    templates = build_workflow_templates(descriptors, expected_count=993)

    assert len({descriptor.id for descriptor in descriptors}) == 993
    assert len({descriptor.name for descriptor in descriptors}) == 993
    assert len({template.sha256 for template in templates}) == 993
