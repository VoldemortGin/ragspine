"""nl-gold 判分器 v3：繁体中文拒答识别（先把拒答相关常用字简繁归一再匹配）。

被测规格：
- 繁体拒答（如 a05 一次运行里的“資料中沒有給出。”）判为拒答。
- 原来“不应判为拒答”的反例写成繁体后仍判为不拒答（开头已作答 / 猜测式作答 / 括号附注 / 组合子项）。
- 简体结果完全不变：归一对简体文本是恒等变换，v2 的正反例判定不变。
- 判分口径变了 → JUDGE_VERSION 升到 v3。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.nl_gold_ragspine import (
    JUDGE_VERSION,
    REFUSAL_MARKERS,
    _to_simplified,
    is_refusal,
)
from tests.eval.test_nl_gold_judge_v2 import ANSWERS, REFUSALS

TRADITIONAL_REFUSALS = [
    # a05（2026-09-27-post-pull，B-narrative 第 1 次运行）的真实开头
    "資料中沒有給出。\n\n檢索片段中沒有提及 AIA 在越南的代理人（tied agents）數量。"
    "片段裡與越南相關的內容僅限於：\n\n- 越南屬於 Other Markets",
    "**所提供的片段中沒有 AIA 越南專屬代理人（tied agents）數量的資訊。**\n\n片段中提到越南的只有兩處。",
    "# 結論：片段中沒有 AIA 2027 全年 VONB 預測\n\n檢索片段均來自 2026 年中期業績演示。",
    "**片段無法明確確定 Foundation 之後是哪個階段。**",
    "無法確定 Foundation 之後的階段。",
    "資料未說明 Foundation 之後的階段。",
    "文件中並沒有相關資料。",
    "檢索結果裡沒有越南代理人數量。",
    "查不到：資料中沒有能回答該問題的依據，不提供任何推測數字。",
    "未檢索到與該問題相關的資料。",
    "無法從提供的資料中得知越南的代理人數量。",
    "材料沒有給出 2027 年的預測。",
]

TRADITIONAL_ANSWERS = [
    "# 代理人渠道佔 VONB 比重（1H26）\n\n**集團整體：代理人渠道佔 VONB 的 72%，合作夥伴渠道佔 28%。**\n\n"
    "| 市場 | Agency | Partnership |\n|---|---|---|\n| 友邦中國 | 87% | 未披露 |",
    "**1H26 的中期股息為每股 53.90 港仙，同比增長 10%。**\n\n注：片段未提供全年股息資料。",
    "**根據片段，Foundation 之後的階段很可能是 Growth，但片段沒有明確說明各階段的先後順序，因此這只是推斷。**",
    "Foundation 之後的階段推斷為 Growth，資料沒有明確說明先後順序。",
    "根據片段，代理人科技投入分為以下三個層次（片段未明確稱其為「階段」，但按遞進順序排列）：\n\n"
    "1. **基礎（Foundation）：100% 數字化代理人（100% Digitalised Agency）**",
    "- REVENUE：120（來源 fact#1）\n- PROFIT：查不到（未在事實表中找到，不提供推測數字）",
]


@pytest.mark.parametrize("answer", TRADITIONAL_REFUSALS)
def test_traditional_refusals_are_recognized(answer: str) -> None:
    assert is_refusal(answer)


@pytest.mark.parametrize("answer", TRADITIONAL_ANSWERS)
def test_traditional_real_answers_are_not_refusals(answer: str) -> None:
    assert not is_refusal(answer)


@pytest.mark.parametrize("text", [*REFUSAL_MARKERS, *REFUSALS, *ANSWERS])
def test_folding_is_identity_on_simplified_text(text: str) -> None:
    assert _to_simplified(text) == text


def test_v2_simplified_judgements_are_unchanged() -> None:
    assert all(is_refusal(a) for a in REFUSALS)
    assert not any(is_refusal(a) for a in ANSWERS)


def test_folding_maps_refusal_characters() -> None:
    assert (
        _to_simplified("資料中沒有給出，無法確定，並未說明") == "资料中没有给出，无法确定，并未说明"
    )


def test_judge_version_is_bumped() -> None:
    assert JUDGE_VERSION == "nl-gold-judge-v3"
