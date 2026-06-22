"""GAP-B 红灯规格：竞品/外部实体误归因防护 + "所属公司"配置化。

TDD 红灯阶段——本文件只写【失败的】测试，不含任何实现。覆盖契约 EXT-01..EXT-12：
外部实体黑名单（最长匹配 + 命中遮蔽防碰撞）、命中即拒答并提议改查 home 等价口径、
绝不输出 home 数字；同时把 home 公司身份泛化为配置（config/company.toml +
src/company_profile.CompanyProfile / load_company_profile），代码里不再硬编码 "ACME"。

红灯应因下列正确原因失败（实现尚未落地）：
- ImportError：ragspine.common.company_profile / load_company_profile 不存在；
- ImportError：ragspine.common.glossary.EXTERNAL_ENTITY_SYNONYMS / resolve_external_entity 不存在；
- ImportError：ragspine.agent.intent.CLARIFY_OUT_OF_SCOPE_ENTITY 不存在；
- AttributeError：ParsedIntent.external_entity 字段不存在；
- 断言失败：竞品问题被静默回填 ACME_GROUP 并答出 home 数字（如 4500）；
- 断言失败：'中国竞安' 被误判为 ACME_CN（碰撞）。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.storage.fact_store import Fact, FactStore

REF = date(2026, 6, 12)

# home 事实（竞品 case 绝不应把这些数字当成竞品答案输出）
REVENUE_HK_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
    period_type="FY", period="2025", value=1702.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx",
    source_locator="slide=5,table=1,row=2,col=3",
)
REVENUE_GROUP_FY2025 = Fact(
    metric_code="REVENUE", entity="ACME_GROUP", geography="ASIA", channel="TOTAL",
    period_type="FY", period="2025", value=4500.0, unit="USD_M",
    source_doc_id="ACME_FY2025_Results.pptx", source_locator="slide=3,table=1",
)


@pytest.fixture
def store(tmp_db_path):
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    fs.upsert_facts([REVENUE_HK_FY2025, REVENUE_GROUP_FY2025])
    yield fs
    fs.close()


class SentinelProvider:
    """一旦被调用即失败：用于断言外部实体拒答路径绝不触发 LLM / tool use。"""

    def chat(self, messages, *, tools=None):
        raise AssertionError("外部实体拒答不应调用 provider（不调 tool/检索/LLM）")


class FakeRetriever:
    """duck-typed NarrativeRetriever：记录调用，断言竞品叙事问法不走静默检索。"""

    def __init__(self, snippets: list[dict] | None = None):
        self.snippets = snippets if snippets is not None else [{
            "text": "占位片段，不应被检索到。",
            "doc_id": "SHOULD_NOT_BE_USED.pptx", "locator": "slide=1",
        }]
        self.calls: list[dict] = []

    def retrieve(self, query: str, *, filters: dict | None = None, top_k: int = 50):
        self.calls.append({"query": query, "filters": filters, "top_k": top_k})
        return self.snippets


# 任何 home 数字都绝不能出现在竞品拒答里（兜底白名单断言用）
_HOME_NUMBERS = ("1702", "4500")


# ---------------------------------------------------------------------------
# EXT-01：竞品（中文）→ 拒答 + 提议改查 ACME，回答不含 home 数字，不调 LLM/tool
# ---------------------------------------------------------------------------

def test_competitor_chinese_refuses_no_home_number(store):
    """user story：高管问"竞安去年REVENUE多少"——竞安是竞品，系统没有其数据。
    必须拒答并提议改查 ACME 等价口径，绝不把 ACME 的 REVENUE（4500/1702）冒充竞安答出，
    且不调用 LLM / tool / 检索（SentinelProvider 不被触碰）。"""
    from ragspine.agent.intent import CLARIFY_OUT_OF_SCOPE_ENTITY

    result = answer_question(
        "竞安去年REVENUE多少", store, SentinelProvider(), reference_date=REF
    )
    assert isinstance(result, AgentResult)
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    assert "竞安" in result.answer  # 明确指出命中的外部实体
    assert "ACME" in result.answer  # 提议改查 home 公司
    assert result.tool_results == []  # 绝不调 tool
    for num in _HOME_NUMBERS:
        assert num not in result.answer  # 绝不泄露 home 数字


# ---------------------------------------------------------------------------
# EXT-02：竞品（英文）→ 大小写不敏感，拒答
# ---------------------------------------------------------------------------

def test_competitor_english_case_insensitive_refuses():
    """user story：英文问法"what is Jingcheng's REVENUE" / "jingan revenue"
    同样应命中外部实体（大小写不敏感），entity 为 None，澄清网关给出 OUT_OF_SCOPE。"""
    from ragspine.agent.intent import (
        CLARIFY_OUT_OF_SCOPE_ENTITY,
        clarify_scope,
        parse_intent,
    )

    pi_pru = parse_intent("what is Jingcheng's REVENUE", reference_date=REF)
    assert pi_pru.external_entity == "Jingcheng"
    assert pi_pru.entity is None
    assert clarify_scope(pi_pru, reference_date=REF).mode == CLARIFY_OUT_OF_SCOPE_ENTITY

    pi_pa = parse_intent("jingan revenue", reference_date=REF)
    assert pi_pa.external_entity == "竞安(Jingan)"
    assert pi_pa.entity is None
    assert clarify_scope(pi_pa, reference_date=REF).mode == CLARIFY_OUT_OF_SCOPE_ENTITY


# ---------------------------------------------------------------------------
# EXT-03：碰撞——"中国竞安"最长匹配命中外部"竞安"，遮蔽后"中国"不泄露成 ACME_CN
# ---------------------------------------------------------------------------

def test_collision_zhongguo_pingan_not_acme_cn():
    """user story（最关键碰撞）："中国竞安的营收"——"中国竞安"是竞品全称，
    必须最长匹配整体吃掉，遮蔽后剩余文本不再含可泄露为 ACME_CN 的"中国"。
    期望：external_entity=='竞安(Jingan)'、entity is None、metric=='REVENUE'、拒答。"""
    from ragspine.agent.intent import (
        CLARIFY_OUT_OF_SCOPE_ENTITY,
        clarify_scope,
        parse_intent,
    )

    pi = parse_intent("中国竞安的营收", reference_date=REF)
    assert pi.external_entity == "竞安(Jingan)"
    assert pi.entity is None  # 遮蔽后"中国"不得泄露成 ACME_CN
    assert "ACME_CN" not in pi.entities
    assert pi.metric == "REVENUE"
    assert clarify_scope(pi, reference_date=REF).mode == CLARIFY_OUT_OF_SCOPE_ENTITY


# ---------------------------------------------------------------------------
# EXT-04：竞品 + home 同现 → 仍拒答（缺竞品数据），只提议查 home，不进 composite
# ---------------------------------------------------------------------------

def test_competitor_and_home_cooccur_refuses_offers_home_only(store):
    """user story："竞安和ACME的REVENUE对比"——同时提到竞品与 home。因没有竞安数据，
    不能做对比；命中外部实体即拒答，提议只查 ACME，绝不输出 home 数字，
    也绝不走多子任务/composite 路径（不调 LLM/tool）。"""
    from ragspine.agent.intent import CLARIFY_OUT_OF_SCOPE_ENTITY

    result = answer_question(
        "竞安和ACME的REVENUE对比", store, SentinelProvider(), reference_date=REF
    )
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    assert "ACME" in result.answer  # 提议只查 home
    assert result.tool_results == []
    for num in _HOME_NUMBERS:
        assert num not in result.answer


# ---------------------------------------------------------------------------
# EXT-05：竞品 + 叙事措辞 → 外部检查先于 narrative 早返回，不走静默检索
# ---------------------------------------------------------------------------

def test_competitor_narrative_phrasing_still_refuses(store):
    """user story："竞安最近表现怎么样"——叙事措辞，但主体是竞品。
    外部实体检查必须排在 ROUTE_NARRATIVE 早返回之前，拒答；注入的 retriever 不被调用
    （retriever.calls 为空），不静默检索竞品资料。"""
    from ragspine.agent.intent import CLARIFY_OUT_OF_SCOPE_ENTITY

    retriever = FakeRetriever()
    result = answer_question(
        "竞安最近表现怎么样", store, SentinelProvider(),
        reference_date=REF, narrative_retriever=retriever,
    )
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    assert retriever.calls == []  # 不走静默叙事检索


# ---------------------------------------------------------------------------
# EXT-06：回归——无任何实体 "去年REVENUE多少" 行为不变（默认 ACME_GROUP + 诚实假设）
# ---------------------------------------------------------------------------

def test_no_entity_regression_default_home_with_assumptions(store):
    """回归守护：用户根本没提实体时，默认 home 集团口径仍合理——行为零变更。
    "去年REVENUE多少" 应走 CLARIFY_ANSWER_WITH_ASSUMPTIONS，假设 entity==ACME_GROUP，
    回答含"假设"与数字 4500。这是"没提实体"与"提了竞品"两种情况必须区分的红线。"""
    from ragspine.agent.intent import CLARIFY_ANSWER_WITH_ASSUMPTIONS
    from ragspine.agent.llm_provider import MockProvider

    result = answer_question(
        "去年REVENUE多少", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_ANSWER_WITH_ASSUMPTIONS
    assert result.clarification.assumed_slots.get("entity") == "ACME_GROUP"
    assert "假设" in result.answer
    assert "4500" in result.answer


# ---------------------------------------------------------------------------
# EXT-07：回归——home 实体 "香港去年REVENUE" 不变 ACME_HK，端到端命中 1702
# ---------------------------------------------------------------------------

def test_home_entity_regression_acme_hk_unchanged(store):
    """回归守护：home 实体解析与端到端命中血缘不得受外部实体机制影响。
    "香港去年REVENUE" → entity=='ACME_HK'、external_entity is None；
    answer_question 命中 1702 且来源血缘不变。"""
    from ragspine.agent.intent import parse_intent
    from ragspine.agent.llm_provider import MockProvider

    pi = parse_intent("香港去年REVENUE", reference_date=REF)
    assert pi.entity == "ACME_HK"
    assert pi.external_entity is None

    result = answer_question(
        "香港去年REVENUE", store, MockProvider(reference_date=REF), reference_date=REF
    )
    assert "1702" in result.answer
    assert "ACME_FY2025_Results.pptx" in result.answer


# ---------------------------------------------------------------------------
# EXT-08：泛化证明——临时 profile（ACME/Globex）无 ACME 硬编码
# ---------------------------------------------------------------------------

def test_profile_generalization_no_acme_hardcode(tmp_path):
    """user story：项目是通用管理 copilot，不专属 ACME。给一份 home='YourCo'、
    external 含 'globex'->'Globex' 的临时 profile，应能：把 'Globex' 判为外部、
    把 'YourCo' 当 home 实体——证明 resolve_external_entity 与 home 匹配均走 profile，
    代码里没有写死的 'ACME'（demo home 标识绝不泄漏进换公司后的 profile）。"""
    from ragspine.common.company_profile import CompanyProfile, load_company_profile

    toml_text = (
        "[home]\n"
        'company_name = "YourCo"\n'
        'entity_code = "YOURCO_GROUP"\n'
        "\n"
        "[home.synonyms]\n"
        'yourco = "YOURCO_GROUP"\n'
        'yourco_group = "YOURCO_GROUP"\n'
        "\n"
        "[home.geography]\n"
        'YOURCO_GROUP = "GLOBAL"\n'
        "\n"
        "[external_entities]\n"
        'globex = "Globex"\n'
    )
    path = tmp_path / "company.toml"
    path.write_text(toml_text, encoding="utf-8")

    profile = load_company_profile(path)
    assert isinstance(profile, CompanyProfile)
    assert profile.home_company_name == "YourCo"
    assert profile.home_entity_code == "YOURCO_GROUP"
    # external 命中走 profile
    assert profile.external_entities.get("globex") == "Globex"
    # home 同义词走 profile，且不含任何 ACME 硬编码
    assert profile.home_entity_synonyms.get("yourco") == "YOURCO_GROUP"
    assert all("ACME" not in code for code in profile.home_entity_synonyms.values())


# ---------------------------------------------------------------------------
# EXT-09：profile 缺失文件 → 静默回退内置默认（=现有 ACME 值），零行为变更
# ---------------------------------------------------------------------------

def test_profile_missing_file_falls_back_to_acme_defaults(tmp_path):
    """回归守护：指向不存在的路径时 load_company_profile 必须静默回退内置默认 profile
    （不抛错/不打印），其值与现有硬编码 ACME 值字节级等价——保证 import 期零副作用、
    既有行为不破。"""
    from ragspine.common.company_profile import load_company_profile

    missing = tmp_path / "does_not_exist.toml"
    profile = load_company_profile(missing)
    assert profile.home_entity_code == "ACME_GROUP"
    # home 同义词与现有 glossary.ENTITY_SYNONYMS 等值
    from ragspine.common.glossary import ENTITY_SYNONYMS

    assert profile.home_entity_synonyms == ENTITY_SYNONYMS
    # 默认外部清单含主要竞品
    assert "竞安" in profile.external_entities
    assert "jingcheng" in profile.external_entities


# ---------------------------------------------------------------------------
# EXT-10：config/company.example.toml 通用模板——无 ACME specifics，可被成功解析
# ---------------------------------------------------------------------------

def test_example_toml_has_no_acme_specifics():
    """user story：为未来开源干净仓库准备的模板必须用虚构占位（ACME/Globex），
    不含 'ACME'/'ACME'/'竞安' 等本部署 specifics，且能被 load_company_profile
    成功解析为合法 CompanyProfile。"""
    from ragspine.common.company_profile import CompanyProfile, load_company_profile

    example = ROOT_DIR / "config" / "company.example.toml"
    assert example.exists(), "config/company.example.toml 应作为通用模板存在"

    text = example.read_text(encoding="utf-8")
    for forbidden in ("ACME", "ACME", "竞安"):
        assert forbidden not in text, f"模板不应含本部署 specifics：{forbidden!r}"

    profile = load_company_profile(example)
    assert isinstance(profile, CompanyProfile)
    assert profile.home_company_name  # 非空 home 身份
    assert profile.home_entity_code


# ---------------------------------------------------------------------------
# EXT-11：回归——glossary.ENTITY_SYNONYMS / ENTITY_GEOGRAPHY 值逐项不变
# ---------------------------------------------------------------------------

def test_glossary_entity_synonyms_values_unchanged():
    """回归守护（最重要）：ENTITY_SYNONYMS/ENTITY_GEOGRAPHY 改为由默认 profile 构建后，
    公共 API 与值必须逐字与现有一致——retrieval/extractors/query_tools 等下游导入方零回归。"""
    from ragspine.common.glossary import ENTITY_GEOGRAPHY, ENTITY_SYNONYMS

    expected_synonyms = {
        "acme group": "ACME_GROUP",
        "acme": "ACME_GROUP",
        "group": "ACME_GROUP",
        "ACME集团": "ACME_GROUP",
        "ACME": "ACME_GROUP",
        "集团": "ACME_GROUP",
        "acme hong kong": "ACME_HK",
        "acme hk": "ACME_HK",
        "hong kong": "ACME_HK",
        "hk": "ACME_HK",
        "香港": "ACME_HK",
        "ACME香港": "ACME_HK",
        "acme china": "ACME_CN",
        "acme cn": "ACME_CN",
        "china": "ACME_CN",
        "cn": "ACME_CN",
        "中国": "ACME_CN",
        "中国内地": "ACME_CN",
        "ACME中国": "ACME_CN",
        "ACME人寿": "ACME_CN",
        "acme_group": "ACME_GROUP",
        "acme_hk": "ACME_HK",
        "acme_cn": "ACME_CN",
    }
    assert ENTITY_SYNONYMS == expected_synonyms
    assert ENTITY_GEOGRAPHY == {
        "ACME_GROUP": "ASIA",
        "ACME_HK": "HK",
        "ACME_CN": "CN",
    }


# ---------------------------------------------------------------------------
# EXT-12：resolve_external_entity 最长匹配 + 大小写不敏感；home 不误判为外部
# ---------------------------------------------------------------------------

def test_resolve_external_entity_longest_match_masking_helper():
    """user story：外部实体解析助手——'中国竞安'/'竞安'/'jingan'/'竞诚'/'JINGCHENG'
    返回对应展示名（最长匹配、大小写不敏感）；home 词 'ACME' 返回 None
    （home 不得被误判为外部）。"""
    from ragspine.common.glossary import resolve_external_entity

    assert resolve_external_entity("中国竞安") == "竞安(Jingan)"
    assert resolve_external_entity("竞安") == "竞安(Jingan)"
    assert resolve_external_entity("jingan") == "竞安(Jingan)"
    assert resolve_external_entity("竞诚") == "Jingcheng"
    assert resolve_external_entity("JINGCHENG") == "Jingcheng"
    assert resolve_external_entity("ACME") is None  # home 不误判为外部


# ---------------------------------------------------------------------------
# CP-01：config/company.toml 默认 profile = ACME（本部署 profile 落档校验）
# ---------------------------------------------------------------------------

def test_load_company_profile_default_is_acme():
    """user story：本部署的 config/company.toml 必须存在且 load_company_profile()
    解析为 ACME profile——home_company_name 含 'ACME'、home_entity_code=='ACME_GROUP'、
    external_entities 含 '竞安'->'竞安(Jingan)' 与 'jingcheng'->'Jingcheng'。"""
    from ragspine.common.company_profile import load_company_profile

    profile = load_company_profile()
    assert "ACME" in profile.home_company_name
    assert profile.home_entity_code == "ACME_GROUP"
    assert profile.external_entities.get("竞安") == "竞安(Jingan)"
    assert profile.external_entities.get("jingcheng") == "Jingcheng"


# ---------------------------------------------------------------------------
# EXT-13：回归红线（SPEC 标注「本任务最重要」）——standalone "中国" 绝不被外部机制波及
# ---------------------------------------------------------------------------

def test_standalone_zhongguo_not_masked_stays_acme_cn():
    """user story（最重要回归红线）：标准 home 词"中国"单独出现时，绝不能被外部
    遮蔽机制误伤——必须仍解析为 ACME_CN，external_entity is None。

    golden set 有 4 条 standalone "中国" case（num-013/ref-001/ref-005/nar-003），
    一个过度贪心的遮蔽实现（把外部"竞安"做成子串匹配后误伤、或把"中国"本身错放进
    外部清单/前缀匹配）会让 13 条专项红测试全绿、只在更下游 qa_eval golden 跑里炸。
    这里在 parse_intent / resolve_external_entity 层直接钉死正向命题。"""
    from ragspine.common.glossary import resolve_external_entity
    from ragspine.agent.intent import parse_intent

    pi = parse_intent("中国FY2025的REVENUE是多少", reference_date=REF)
    assert pi.external_entity is None  # 外部机制绝不波及 home 词
    assert pi.entity == "ACME_CN"
    assert "ACME_CN" in pi.entities
    assert pi.metric == "REVENUE"

    # 解析助手层：home 短语不得被任何外部键命中
    assert resolve_external_entity("中国") is None
    assert resolve_external_entity("中国内地") is None


# ---------------------------------------------------------------------------
# EXT-14：碰撞数据完整性——其他"中国"前缀竞品（中国竞寿/中国竞平）也须整体吞掉
# ---------------------------------------------------------------------------

def test_collision_other_china_prefixed_competitors_no_acme_cn_leak():
    """user story：SPEC collision_handling_note 要求外部清单把"中国竞安""中国竞寿"
    "中国竞平"等以"中国"开头的竞品作为完整键，否则遮蔽短别名后残留"中国"泄露成 ACME_CN。
    现有测试只覆盖了"中国竞安"一个变体——抽样第二/三个"中国"前缀竞品钉死最长匹配
    整体吞掉"中国"的行为（黑名单数据完整性契约）。"""
    from ragspine.agent.intent import (
        CLARIFY_OUT_OF_SCOPE_ENTITY,
        clarify_scope,
        parse_intent,
    )

    pi_life = parse_intent("中国竞寿去年REVENUE", reference_date=REF)
    assert pi_life.external_entity == "Jingshou"
    assert pi_life.entity is None  # 遮蔽后"中国"不得泄露成 ACME_CN
    assert "ACME_CN" not in pi_life.entities
    assert clarify_scope(pi_life, reference_date=REF).mode == CLARIFY_OUT_OF_SCOPE_ENTITY

    pi_taiping = parse_intent("中国竞平的营收", reference_date=REF)
    assert pi_taiping.external_entity == "Jingping"
    assert pi_taiping.entity is None
    assert "ACME_CN" not in pi_taiping.entities
    assert (
        clarify_scope(pi_taiping, reference_date=REF).mode
        == CLARIFY_OUT_OF_SCOPE_ENTITY
    )


# ---------------------------------------------------------------------------
# EXT-15：拒答 message 含 metric + home_company_name；narrowing_options 给出可一键收窄项
# ---------------------------------------------------------------------------

def test_out_of_scope_message_contains_metric_and_home_name_when_metric_present():
    """user story：SPEC clarify_scope 契约要求拒答 message『若解析到 metric 则带上
    metric』，且 narrowing_options 给出『改查 {home_company_name} 的对应数字』。
    现有 EXT-01/04 只断言 answer 含 '竞安'/'ACME'——从不验证 ClarificationResult 自身的
    question/message 含 metric、narrowing_options 非空且用泛化 home_company_name。
    一个只返回模式常量、message 为空/无收窄项的实现会过现有断言但违背契约
    （用户拿不到可一键收窄的"改查 ACME REVENUE"提议）。"""
    from ragspine.common.company_profile import load_company_profile
    from ragspine.agent.intent import (
        CLARIFY_OUT_OF_SCOPE_ENTITY,
        clarify_scope,
        parse_intent,
    )

    home_name = load_company_profile().home_company_name

    clar = clarify_scope(parse_intent("竞安去年REVENUE多少", reference_date=REF))
    assert clar.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    # 拒答提示（question 即对外 message）须同时含命中的外部实体、metric、home 名
    message = clar.question or ""
    assert "竞安" in message
    assert "REVENUE" in message
    assert home_name in message
    # 一键收窄项非空，且文案用泛化的 home_company_name 而非空/硬编码
    assert clar.narrowing_options
    assert any(home_name in opt for opt in clar.narrowing_options)


# ---------------------------------------------------------------------------
# EXT-16：早返回顺序——无期间竞品问法不掉进假设回填（不泄露 home 口径）
# ---------------------------------------------------------------------------

def test_out_of_scope_with_period_does_not_emit_home_assumption_note(store):
    """user story：外部实体检查必须排在 metric-缺失与默认假设回填之前。竞品+缺期间的
    问法（"竞安的REVENUE"，无期间）若先走到 ANSWER_WITH_ASSUMPTIONS，会把"期间默认 FYxxxx /
    实体默认 ACME Group"的假设说明拼进 answer——变相把 home 口径泄露给竞品问句。
    EXT-01 的"竞安去年REVENUE"恰好带期间遮蔽了这条风险；用无期间 case 证明 OUT_OF_SCOPE
    真正最前置、不掉进假设回填，也不调 provider。"""
    from ragspine.agent.intent import CLARIFY_OUT_OF_SCOPE_ENTITY

    result = answer_question(
        "竞安的REVENUE", store, SentinelProvider(), reference_date=REF
    )
    assert result.clarification is not None
    assert result.clarification.mode == CLARIFY_OUT_OF_SCOPE_ENTITY
    # 绝不出现 home 口径假设回填说明
    for leak in ("假设", "默认按集团", "ACME Group 口径", "最近完整财年"):
        assert leak not in result.answer
    assert result.tool_results == []  # 不走假设回填后的工具执行
    for num in _HOME_NUMBERS:
        assert num not in result.answer  # 绝不泄露 home 数字


# ---------------------------------------------------------------------------
# EXT-17：qa_eval 双 runner 把 OUT_OF_SCOPE 显式置 refused（关键接线点，易漏）
# ---------------------------------------------------------------------------

def test_qa_eval_runners_mark_out_of_scope_as_refused_both_modes(store):
    """user story：SPEC 把 qa_eval 双模式接线列为『关键接线点(易漏)』——
    run_case_tool_direct 自推答案、run_case_agent 经 answer_question，两处都须对
    CLARIFY_OUT_OF_SCOPE_ENTITY 显式置 refused=True，否则未来加竞品 golden 时
    tool/agent 两模式答案不一致（假绿/假红）。现有红测试集完全没触达 qa_eval 这两个
    runner 的 out-of-scope 分支——这块接线在 TDD 下无测试守护，绿阶段极易漏接。"""
    from datetime import date as _date

    from ragspine.agent.intent import CLARIFY_OUT_OF_SCOPE_ENTITY
    from ragspine.eval.qa_eval import (
        GoldenCase,
        detect_fabricated_numbers,
        run_case_agent,
        run_case_tool_direct,
    )

    case = GoldenCase(
        id="ext-001",
        question="竞安去年REVENUE多少",
        case_type="refusal",
        expected={"clarification": "none", "refuse": True},
        tags={"topic": "FIN", "scope": "competitor", "qtype": "numeric"},
        reference_date=_date(2026, 6, 12),
    )
    retriever = FakeRetriever()

    out_tool = run_case_tool_direct(case, store, retriever)
    out_agent = run_case_agent(case, store, retriever)

    for out in (out_tool, out_agent):
        assert out.refused is True
        assert out.found_value is None
        assert out.clarification_mode == CLARIFY_OUT_OF_SCOPE_ENTITY
        assert detect_fabricated_numbers(out.answer) == []  # 拒答文本不含 home 数字


# ---------------------------------------------------------------------------
# EXT-18：外部清单不得污染 retrieval 的 home query 改写词典
# ---------------------------------------------------------------------------

def test_external_entity_does_not_inject_home_query_rewrite_in_retrieval():
    """user story：SPEC collision_handling_note 第 2 点要求 EXTERNAL_ENTITY_SYNONYMS
    不得注入 retrieval 的 query 改写器（外部实体不应触发 home query 改写）。EXT-05 只
    验证了竞品叙事问法在 agent 层不调 retriever；这里守护『外部清单泄漏进 home 改写
    词典』这一回归——若实现把 external 别名并入改写词典，竞品词会污染检索召回。"""
    from ragspine.common.glossary import ENTITY_SYNONYMS, EXTERNAL_ENTITY_SYNONYMS
    from ragspine.retrieval.lexical.retrieval import GlossaryQueryRewriter

    # ENTITY_SYNONYMS（home 改写来源）不含任何 external 别名键
    for ext_key in EXTERNAL_ENTITY_SYNONYMS:
        assert ext_key not in ENTITY_SYNONYMS

    # 用竞品词触发 query 改写：不得映射成任何 ACME_* home 实体改写项
    rewriter = GlossaryQueryRewriter()
    variants = rewriter.rewrite("竞安REVENUE")
    for variant in variants:
        assert "ACME_GROUP" not in variant
        assert "ACME_HK" not in variant
        assert "ACME_CN" not in variant
