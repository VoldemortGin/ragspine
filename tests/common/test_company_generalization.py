"""GAP-B 表现层红灯规格："所属公司"泛化的最后一公里。

上一批已把数据/识别层配置化（CompanyProfile + config/company.toml + 竞品越权拒答），
但表现层仍硬编码 ACME：换 profile（如 ACME）后用户可见文案 / 喂给 LLM 的 prompt 仍说
ACME，与激活配置自相矛盾。本批堵死它。

TDD 红灯阶段——本文件只写【失败的】测试，不含任何实现。覆盖契约 G1..G5：
- G1 默认(ACME) profile：clarify_scope 的 entity 缺失 assumption_note / narrowing_options
  由 profile labels 派生（而非写死），ACME 默认下文案仍含 ACME 字样；
- G2 ACME profile 激活（经支持的运行期换 profile 机制）：clarify_scope / agent 系统 prompt /
  query_tools tool schema 全部说 ACME、不含 ACME；
- G3 env-var RAGSPINE_COMPANY_CONFIG：缺省 path 时优先读它，未设时回默认 ACME；
- G4 CompanyProfile.home_entity_labels 字段：从 [home.labels] 读，缺省回退默认 ACME labels；
- G5 回归守护：默认 profile 下 agent 系统 prompt 仍含 "ACME"（换 profile 才变）。

红灯应因下列正确原因失败（表现层实现尚未泛化）：
- AttributeError：CompanyProfile.home_entity_labels 字段不存在（G1/G2/G4）；
- 断言失败：ACME profile 激活后 clarify_scope assumption_note 仍写死 "ACME Group"、
  narrowing_options 仍写死 "改查 ACME Hong Kong/China"（G2）；
- 断言失败：ACME profile 激活后 agent 系统 prompt 仍写死 "你是 ACME 管理层…"（G2）；
- 断言失败：ACME profile 激活后 query_tools tool schema description 仍写死
  "ACME Hong Kong/ACME China"（G2）；
- 断言失败：RAGSPINE_COMPANY_CONFIG 未被 load_company_profile(path=None) 消费（G3）。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import ragspine.agent.agent as agent_mod
import ragspine.agent.intent as intent_mod
import ragspine.agent.query_tools as query_tools_mod
import ragspine.eval.qa_eval as qa_eval_mod
from ragspine.common.company_profile import CompanyProfile, load_company_profile
from ragspine.agent.intent import clarify_scope, parse_intent

REF = date(2026, 6, 12)


# ---------------------------------------------------------------------------
# 运行期换 profile 机制（契约 F：测试用 monkeypatch 各模块 module-level _PROFILE 演示）
# ---------------------------------------------------------------------------
# 用 monkeypatch.setattr 直接重绑各模块的 _PROFILE（而非 importlib.reload）：
# reload 会生成新的 CompanyProfile/ParsedIntent/... 类对象，让别处持有旧类引用的
# isinstance 检查失败（跨测试污染）。monkeypatch 复用同一 CompanyProfile 类、用例结束
# 自动还原，零污染。契约 F 明确支持"monkeypatch 该 _PROFILE 或设 RAGSPINE_COMPANY_CONFIG"。

# 承载 module-level _PROFILE、需随 profile 切换重绑的模块（表现层三处 + 默认实体 +
# 反编造检查 eval.qa_eval，使期间白名单随 profile 切换——见 ADR 0004 STEP 11）。
_PROFILE_BOUND_MODULES = (intent_mod, query_tools_mod, agent_mod, qa_eval_mod)


def _acme_profile() -> CompanyProfile:
    """临时 YourCo profile：home=YourCo + labels {YOURCO_GROUP/YOURCO_NORTH} + external globex。

    契约 G2 的固定形态——切换到一个与 demo home（ACME）【不同】的公司，证明表现层文案
    随 profile 切换。home_entity_labels 用 YOURCO_GROUP→"YourCo Group"、
    YOURCO_NORTH→"YourCo North"，默认实体为 YOURCO_GROUP（labels 中非默认项即 YourCo North）。
    用 CompanyProfile(**kwargs) 直接构造，含 home_entity_labels 字段——该字段不存在时
    （契约 A/G4 未实现）TypeError，正是 G2 期望的红灯原因之一。
    """
    return CompanyProfile(
        home_company_name="YourCo Group",
        home_entity_code="YOURCO_GROUP",
        home_entity_synonyms={
            "yourco": "YOURCO_GROUP",
            "yourco group": "YOURCO_GROUP",
            "yourco north": "YOURCO_NORTH",
            "north": "YOURCO_NORTH",
            "yourco_group": "YOURCO_GROUP",
            "yourco_north": "YOURCO_NORTH",
        },
        entity_geography={"YOURCO_GROUP": "GLOBAL", "YOURCO_NORTH": "NORTH"},
        external_entities={"globex": "Globex"},
        home_entity_labels={"YOURCO_GROUP": "YourCo Group", "YOURCO_NORTH": "YourCo North"},
    )


def _activate_acme(monkeypatch, profile: CompanyProfile) -> None:
    """经【支持的运行期换 profile 机制】把表现层三模块切到 ACME profile。

    机制 = monkeypatch 各模块 module-level _PROFILE（及 intent 的派生默认实体）。
    用例结束 monkeypatch 自动还原，绝不污染其他测试。
    """
    for mod in _PROFILE_BOUND_MODULES:
        monkeypatch.setattr(mod, "_PROFILE", profile, raising=False)
    # intent 层把默认实体缓存在 module-level _DEFAULT_ENTITY，一并随 profile 切换。
    monkeypatch.setattr(
        intent_mod, "_DEFAULT_ENTITY", profile.home_entity_code, raising=False
    )


def _write_acme_toml(tmp_path) -> os.PathLike:
    """临时 YourCo profile 的磁盘 toml（含 [home.labels] 段），供 load/env-var 路径用。

    切换到与 demo home（ACME）不同的 YourCo，证明 load/env-var 路径真读取该文件。
    """
    toml_text = (
        "[home]\n"
        'company_name = "YourCo Group"\n'
        'entity_code = "YOURCO_GROUP"\n'
        "\n"
        "[home.synonyms]\n"
        '"yourco" = "YOURCO_GROUP"\n'
        '"yourco group" = "YOURCO_GROUP"\n'
        '"yourco north" = "YOURCO_NORTH"\n'
        '"north" = "YOURCO_NORTH"\n'
        '"yourco_group" = "YOURCO_GROUP"\n'
        '"yourco_north" = "YOURCO_NORTH"\n'
        "\n"
        "[home.geography]\n"
        'YOURCO_GROUP = "GLOBAL"\n'
        'YOURCO_NORTH = "NORTH"\n'
        "\n"
        "[home.labels]\n"
        'YOURCO_GROUP = "YourCo Group"\n'
        'YOURCO_NORTH = "YourCo North"\n'
        "\n"
        "[external_entities]\n"
        'globex = "Globex"\n'
    )
    path = tmp_path / "company_yourco.toml"
    path.write_text(toml_text, encoding="utf-8")
    return path


def _current_anthropic_tool() -> dict:
    """取当前生效的 Anthropic query_metric tool schema（反映已 monkeypatch 的 _PROFILE）。

    绿阶段可能把 schema 改为按 _PROFILE 动态生成的构造函数（如 build_*_tool），
    也可能保留 module-level 常量但令其随 _PROFILE 重算——这里优先用动态构造入口，
    否则回退常量，不预设绿实现的具体函数名。
    """
    for builder_name in (
        "build_query_metric_tool_anthropic",
        "build_anthropic_tool",
        "make_query_metric_tool_anthropic",
        "query_metric_tool_anthropic",
    ):
        builder = getattr(query_tools_mod, builder_name, None)
        if callable(builder):
            return builder()
    return query_tools_mod.QUERY_METRIC_TOOL_ANTHROPIC


# ===========================================================================
# G4：CompanyProfile.home_entity_labels 字段
# ===========================================================================

def test_home_entity_labels_field_default_acme():
    """user story：CompanyProfile 必须有 home_entity_labels 字段（entity_code→展示名）。
    缺省（本部署 config/company.toml 或缺失回退）应得默认 ACME labels：
    ACME_GROUP→"ACME Group"、ACME_HK→"ACME Hong Kong"、ACME_CN→"ACME China"。"""
    from ragspine.common.company_profile import load_company_profile

    profile = load_company_profile()
    assert hasattr(profile, "home_entity_labels"), "CompanyProfile 应有 home_entity_labels 字段"
    labels = profile.home_entity_labels
    assert labels.get("ACME_GROUP") == "ACME Group"
    assert labels.get("ACME_HK") == "ACME Hong Kong"
    assert labels.get("ACME_CN") == "ACME China"


def test_home_entity_labels_missing_file_falls_back_to_acme_defaults(tmp_path):
    """user story：指向不存在路径时，home_entity_labels 静默回退内置默认 ACME labels
    （与缺失文件回退其他字段一致，保证既有行为零变更）。"""
    from ragspine.common.company_profile import load_company_profile

    profile = load_company_profile(tmp_path / "no_such.toml")
    assert profile.home_entity_labels == {
        "ACME_GROUP": "ACME Group",
        "ACME_HK": "ACME Hong Kong",
        "ACME_CN": "ACME China",
    }


def test_home_entity_labels_read_from_toml(tmp_path):
    """user story：[home.labels] 段必须被正确读取——YourCo 临时 toml 的 labels
    应原样进 home_entity_labels（不掺任何 ACME 默认）。"""
    from ragspine.common.company_profile import load_company_profile

    path = _write_acme_toml(tmp_path)
    profile = load_company_profile(path)
    assert profile.home_entity_labels == {
        "YOURCO_GROUP": "YourCo Group",
        "YOURCO_NORTH": "YourCo North",
    }
    assert all("ACME" not in label for label in profile.home_entity_labels.values())


# ===========================================================================
# G3：env-var RAGSPINE_COMPANY_CONFIG → load_company_profile(path=None) 优先读它
# ===========================================================================

def test_env_var_overrides_default_config_path(tmp_path, monkeypatch):
    """user story：运行期换公司不应靠就地编辑文件。设 RAGSPINE_COMPANY_CONFIG 指向临时 ACME
    toml 后，load_company_profile()（path=None）必须返回 ACME profile。"""
    path = _write_acme_toml(tmp_path)
    monkeypatch.setenv("RAGSPINE_COMPANY_CONFIG", str(path))
    profile = load_company_profile()
    assert profile.home_company_name == "YourCo Group"
    assert profile.home_entity_code == "YOURCO_GROUP"
    assert "ACME" not in profile.home_company_name


def test_env_var_unset_falls_back_to_default_acme(monkeypatch):
    """user story：未设 RAGSPINE_COMPANY_CONFIG 时，load_company_profile()（path=None）
    回默认 ACME（本部署 config/company.toml），保证既有部署零变更。"""
    monkeypatch.delenv("RAGSPINE_COMPANY_CONFIG", raising=False)
    profile = load_company_profile()
    assert "ACME" in profile.home_company_name
    assert profile.home_entity_code == "ACME_GROUP"


def test_explicit_path_takes_precedence_over_env_var(tmp_path, monkeypatch):
    """user story：显式传 path 时优先于 env-var（env-var 只在 path 缺省时生效），
    保证既有显式调用方（测试/脚本）行为不被 env-var 偷换。"""
    acme = _write_acme_toml(tmp_path)
    monkeypatch.setenv("RAGSPINE_COMPANY_CONFIG", str(acme))
    # 显式指向不存在路径：应回退内置默认 ACME，而非读 env-var 的 ACME
    profile = load_company_profile(tmp_path / "explicit_missing.toml")
    assert profile.home_entity_code == "ACME_GROUP"


# ===========================================================================
# G1：默认(ACME) profile — clarify_scope 文案由 labels 派生（而非写死）
# ===========================================================================

def test_g1_default_assumption_note_derived_contains_acme_group():
    """user story：默认 ACME profile 下，缺实体的 assumption_note 必须含默认实体展示名
    "ACME Group"（由 home_entity_labels[home_entity_code] 派生，非写死）。"""
    intent = parse_intent("REVENUE多少", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    assert clar.assumption_note is not None
    assert "ACME Group" in clar.assumption_note


def test_g1_default_narrowing_options_derived_contain_hk_and_cn():
    """user story：默认 ACME profile 下，缺实体的 narrowing_options 必须含
    "改查 ACME Hong Kong" 与 "改查 ACME China"（由 labels 中非默认实体派生）。"""
    intent = parse_intent("REVENUE多少", reference_date=REF)
    clar = clarify_scope(intent, reference_date=REF)
    opts = clar.narrowing_options
    assert "改查 ACME Hong Kong" in opts
    assert "改查 ACME China" in opts
    # 默认实体（ACME Group）不应出现在收窄项里（收窄 = 改查非默认实体）
    assert not any(opt == "改查 ACME Group" for opt in opts)


def test_g1_period_narrowing_unchanged_no_company_name():
    """user story：period 相关文案不含公司名（契约 C：period 部分保持不变）。
    缺期间的 assumption_note / narrowing_options 不应掺入公司名 "ACME"。"""
    intent = parse_intent("香港REVENUE多少", reference_date=REF)  # 缺期间、有实体
    clar = clarify_scope(intent, reference_date=REF)
    # 只缺期间时不应触发实体收窄项；period 文案不带公司名
    assert clar.assumption_note is not None
    period_opts = [o for o in clar.narrowing_options if "改查 ACME" not in o]
    assert period_opts, "缺期间应给出期间收窄项"
    for opt in period_opts:
        assert "ACME" not in opt


# ===========================================================================
# G2：ACME profile 激活 — clarify_scope / agent / query_tools 全说 ACME、不含 ACME
# ===========================================================================

def test_g2_clarify_scope_uses_acme_labels(monkeypatch):
    """user story：换上 YourCo profile（经支持的运行期换 profile 机制）后，缺实体的
    assumption_note 必须含 "YourCo Group"、【不含】"ACME"；narrowing_options 必须含
    "改查 YourCo North"、不含 "改查 ACME Hong Kong"。"""
    _activate_acme(monkeypatch, _acme_profile())

    intent = intent_mod.parse_intent("REVENUE多少", reference_date=REF)
    clar = intent_mod.clarify_scope(intent, reference_date=REF)

    assert clar.assumption_note is not None
    assert "YourCo Group" in clar.assumption_note
    assert "ACME" not in clar.assumption_note

    opts = clar.narrowing_options
    assert "改查 YourCo North" in opts
    assert "改查 ACME Hong Kong" not in opts
    assert not any("ACME" in opt for opt in opts)


def test_g2_agent_system_prompt_uses_acme(monkeypatch):
    """user story：换上 YourCo profile 后，喂给 LLM 的 agent 系统 prompt 必须含 "YourCo"、
    【不含】"ACME"——否则换公司后模型仍自称 ACME 助手，与激活配置自相矛盾。"""
    _activate_acme(monkeypatch, _acme_profile())

    prompt = agent_mod._system_prompt(REF)
    assert "YourCo" in prompt
    assert "ACME" not in prompt


def test_g2_query_tools_description_uses_acme(monkeypatch):
    """user story：换上 ACME profile 后，function-calling tool schema 的 description
    （直接进 LLM prompt）必须含 ACME 实体名、【不含】"ACME Hong Kong"——否则 LLM 仍被
    ACME 实体示例引导。

    用 query_tools 模块的入口读取 tool schema（绿阶段须改为按 _PROFILE 动态生成）：
    优先调动态构造入口（若绿阶段提供 build/make_*_tool 函数），否则读 module-level
    常量——两种合理绿设计都被覆盖，不预设具体实现名。"""
    _activate_acme(monkeypatch, _acme_profile())

    tool = _current_anthropic_tool()
    blob = tool["description"] + tool["input_schema"]["properties"]["entity"]["description"]
    assert "YourCo" in blob
    assert "ACME Hong Kong" not in blob
    assert "ACME China" not in blob


# ===========================================================================
# G5：回归守护 — 默认 profile 下文案仍含 ACME（换 profile 才变）
# ===========================================================================

def test_g5_default_agent_system_prompt_still_contains_acme():
    """回归守护：默认 ACME profile 下，agent 系统 prompt（由 profile 派生）必须仍含
    "ACME"——保证不换 profile 时 LLM 行为零变更，qa_eval agent 模式不退化。"""
    import ragspine.agent.agent as agent_mod

    prompt = agent_mod._system_prompt(REF)
    assert "ACME" in prompt


def test_g5_default_query_tools_description_still_contains_acme():
    """回归守护：默认 ACME profile 下，tool schema description 必须仍体现 ACME 实体名——
    保证 qa_eval agent 模式 function-calling 召回不退化。"""
    tool = _current_anthropic_tool()
    blob = tool["description"] + tool["input_schema"]["properties"]["entity"]["description"]
    assert "ACME" in blob
