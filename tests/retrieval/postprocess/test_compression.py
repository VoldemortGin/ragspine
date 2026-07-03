"""W8 上下文压缩 postprocessor：确定性抽取式去噪 + provenance 绝不丢失 + LLM 缝 opt-in。"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess import (
    PROMPT_TEXT_KEY,
    CompressionPostprocessor,
)

QUERY = "香港 REVENUE 下降 原因"

# 一句强相关（含大量 query 词）+ 一句噪声（几乎无 query 词）。
RELEVANT_SENT = "香港 REVENUE 下降 主要 因 MCV 客群 收缩。"
NOISE_SENT = "此外 团队 上周 举办 了 一次 内部 团建 活动。"


def _snip() -> dict[str, object]:
    return {
        "text": RELEVANT_SENT + NOISE_SENT,
        "doc_id": "HK_QBR.pptx",
        "source_locator": "slide-3",
        "chunk_id": "c1",
        "title": "香港 QBR",
        "sensitivity": "INTERNAL",
        "scores": {"bm25": 1.0, "vector": 0.0, "fused": 0.5},
    }


def test_compression_denoises_into_prompt_text_only():
    """抽取式压缩把去噪文本写进 prompt_text，且更短（省 token）。"""
    out = CompressionPostprocessor(threshold=0.3).postprocess(QUERY, [_snip()])
    s = out[0]
    assert PROMPT_TEXT_KEY in s
    compressed = str(s[PROMPT_TEXT_KEY])
    assert "MCV" in compressed           # 相关句保留
    assert "团建" not in compressed       # 噪声句被裁掉
    assert len(compressed) < len(RELEVANT_SENT + NOISE_SENT)


def test_compression_never_touches_provenance_fields():
    """引用字段（text/source_locator/doc_id/chunk_id/title/scores/sensitivity）逐字不动。"""
    original = _snip()
    out = CompressionPostprocessor(threshold=0.3).postprocess(QUERY, [original])
    s = out[0]
    assert s["text"] == RELEVANT_SENT + NOISE_SENT   # 原文 citable 文本不变
    assert s["source_locator"] == "slide-3"
    assert s["doc_id"] == "HK_QBR.pptx"
    assert s["chunk_id"] == "c1"
    assert s["title"] == "香港 QBR"
    assert s["scores"] == {"bm25": 1.0, "vector": 0.0, "fused": 0.5}
    assert s["sensitivity"] == "INTERNAL"
    # 未篡改调用方原对象（返回新 dict）。
    assert PROMPT_TEXT_KEY not in original


def test_compression_prompt_text_is_verbatim_extract_of_original():
    """prompt_text 由原文句子逐字拼接（可追溯，非改写编造）。"""
    out = CompressionPostprocessor(threshold=0.3).postprocess(QUERY, [_snip()])
    compressed = str(out[0][PROMPT_TEXT_KEY])
    for piece in compressed.replace("。", "。\n").splitlines():
        piece = piece.strip()
        if piece:
            assert piece in (RELEVANT_SENT + NOISE_SENT)


def test_compression_keeps_best_sentence_when_all_below_threshold():
    """没有句子达标时保留最相关的一句，绝不产出空 prompt_text（不丢内容）。"""
    snip = {"text": "完全 无关 的 一句 话。另 一句 也 无关。", "chunk_id": "c1"}
    out = CompressionPostprocessor(threshold=0.99).postprocess(QUERY, [snip])
    assert str(out[0][PROMPT_TEXT_KEY]).strip()  # 非空


def test_compression_deterministic():
    o1 = CompressionPostprocessor(threshold=0.3).postprocess(QUERY, [_snip()])
    o2 = CompressionPostprocessor(threshold=0.3).postprocess(QUERY, [_snip()])
    assert o1[0][PROMPT_TEXT_KEY] == o2[0][PROMPT_TEXT_KEY]


def test_compression_llm_seam_is_opt_in():
    """opt-in compressor 缝：注入时用其产出，缺省走确定性抽取（默认离线零模型）。"""
    calls: list[tuple[str, str]] = []

    def fake_llm_compressor(query: str, text: str) -> str:
        calls.append((query, text))
        return "LLM_COMPRESSED"

    out = CompressionPostprocessor(compressor=fake_llm_compressor).postprocess(QUERY, [_snip()])
    assert out[0][PROMPT_TEXT_KEY] == "LLM_COMPRESSED"
    assert calls and calls[0][0] == QUERY
    # 引用字段仍不动
    assert out[0]["source_locator"] == "slide-3"
