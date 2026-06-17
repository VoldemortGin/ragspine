"""SecurityGate 规格（TDD，ADR 0010）：确定性越权/竞品拒答 + 命中遮蔽。

安全门是不可插拔、零-LLM 的前门守卫：它独立于（可替换的）意图解析器，
仅凭配置（外部/竞品实体清单 + home 公司名）做确定性判定。本模块从单元层
钉死 gate 的三件事：
    1) 最长匹配 + 等长空格遮蔽（防"中国竞安"遮蔽后残留"中国"泄露成 ACME_CN）；
    2) 命中即拒答，文案含【外部主体 + 指标 + home 公司名】，并给出收窄项；
    3) 完全配置驱动——换一份外部清单/公司名，检测与文案随之变化，无任何硬编码竞品。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.security_gate import (
    SECURITY_ALLOW,
    SECURITY_REFUSE_OUT_OF_SCOPE,
    SecurityGate,
)
from ragspine.common.company_profile import load_company_profile
from ragspine.common.glossary import EXTERNAL_ENTITY_SYNONYMS


def _default_gate() -> SecurityGate:
    """以生产同源的配置（glossary 外部清单 + 默认 home 名）构建 gate。"""
    return SecurityGate(
        EXTERNAL_ENTITY_SYNONYMS,
        load_company_profile().home_company_name,
    )


# --------------------------------------------------------------------------
# 1) 检测 + 遮蔽
# --------------------------------------------------------------------------

def test_detect_longest_match_masks_whole_competitor():
    """"中国竞安"整体命中（长于"竞安"），遮蔽后既无"竞安"也无"中国"残留。"""
    scr = _default_gate().detect("中国竞安的营收")
    assert scr.external_entity == "竞安(Jingan)"
    assert "竞安" not in scr.masked_text
    assert "中国" not in scr.masked_text  # red-line：遮蔽后不得残留 home 词"中国"


def test_detect_standalone_china_is_not_external():
    """裸"中国"无外部命中：external 为 None，文本不被遮蔽（留给 home 实体解析）。"""
    scr = _default_gate().detect("中国FY2025的REVENUE是多少")
    assert scr.external_entity is None
    assert "中国" in scr.masked_text


def test_detect_case_insensitive_english_alias():
    scr = _default_gate().detect("JINGCHENG revenue")
    assert scr.external_entity == "Jingcheng"


def test_detect_other_china_prefixed_competitors_masked_whole():
    """中国竞寿 / 中国竞平 同样整键遮蔽，不泄露成 ACME_CN。"""
    gate = _default_gate()
    a = gate.detect("中国竞寿去年REVENUE")
    assert a.external_entity == "Jingshou"
    assert "中国" not in a.masked_text
    b = gate.detect("中国竞平的营收")
    assert b.external_entity == "Jingping"
    assert "中国" not in b.masked_text


# --------------------------------------------------------------------------
# 2) 拒答决策（screen）
# --------------------------------------------------------------------------

def test_screen_refuses_competitor_with_full_message():
    home = load_company_profile().home_company_name
    verdict = _default_gate().screen(raw_question="竞安去年REVENUE多少", metric="REVENUE")
    assert verdict.decision == SECURITY_REFUSE_OUT_OF_SCOPE
    assert verdict.external_entity == "竞安(Jingan)"
    assert "竞安" in verdict.message
    assert "REVENUE" in verdict.message
    assert home in verdict.message
    assert verdict.narrowing_options
    assert any(home in opt for opt in verdict.narrowing_options)


def test_screen_allows_home_entity():
    verdict = _default_gate().screen(raw_question="香港去年REVENUE多少", metric="REVENUE")
    assert verdict.decision == SECURITY_ALLOW
    assert verdict.external_entity is None
    assert verdict.message is None


def test_screen_without_metric_uses_generic_clause():
    """缺指标时拒答文案用"的对应数字"占位（不硬编码任何指标）。"""
    verdict = _default_gate().screen(raw_question="竞安最近怎么样", metric=None)
    assert verdict.decision == SECURITY_REFUSE_OUT_OF_SCOPE
    assert "的对应数字" in verdict.message


def test_screen_redetects_from_raw_question_not_a_parser_field():
    """screen 只吃 raw_question + metric：不依赖任何解析器产出的 external 字段，
    这是"安全确定、意图灵活"解耦的根基——换 parser 也无法绕过。"""
    verdict = _default_gate().screen(raw_question="中国竞安的营收", metric=None)
    assert verdict.decision == SECURITY_REFUSE_OUT_OF_SCOPE
    assert verdict.external_entity == "竞安(Jingan)"


# --------------------------------------------------------------------------
# 3) 完全配置驱动（无硬编码竞品）
# --------------------------------------------------------------------------

def test_gate_is_config_driven_no_hardcoded_competitor():
    """换一份外部清单 + 公司名：检测与文案随之变化，原 ACME 竞品不再被识别。"""
    gate = SecurityGate({"globex": "Globex Corp", "initech": "Initech"}, "YourCo")
    assert gate.detect("globex sales").external_entity == "Globex Corp"
    assert gate.detect("竞安的营收").external_entity is None  # 非本配置竞品 → 不识别

    verdict = gate.screen(raw_question="initech 的营收", metric="REVENUE")
    assert verdict.decision == SECURITY_REFUSE_OUT_OF_SCOPE
    assert "Initech" in verdict.message
    assert "YourCo" in verdict.message
    assert "ACME" not in verdict.message
