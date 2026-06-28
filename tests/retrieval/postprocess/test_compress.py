"""W8 抽取式上下文压缩单测：句级相关性过滤；只 trim text、保血缘、不删片段；确定性。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.postprocess.compress import (
    CompressionPostprocessor,
    ExtractiveCompressor,
)


def test_extractive_keeps_relevant_sentences_drops_rest():
    """保留含 query 内容词的句子，丢与 query 无重叠的句子。"""
    text = "香港 收入 下降 主要 因 银保。今天 天气 很 好。代理人 渠道 保持 稳定。"
    out = ExtractiveCompressor().compress("香港 收入", text)
    assert "香港 收入 下降" in out
    assert "天气" not in out, "无关句应被丢弃"


def test_empty_query_returns_text_unchanged():
    """query 无内容词 -> 原样返回（无从过滤）。"""
    text = "香港 收入 下降。"
    assert ExtractiveCompressor().compress("", text) == text
    assert ExtractiveCompressor().compress("！？。", text) == text


def test_no_sentence_qualifies_keeps_original():
    """无句达标 -> 返回原文（只 trim 不盲删，守召回）。"""
    text = "新加坡 利润 增长。代理人 扩张。"
    out = ExtractiveCompressor().compress("巴西 监管 处罚", text)
    assert out == text


def test_min_overlap_threshold():
    """min_overlap=2：句须含 >=2 个 query 内容词元才保留（CJK 为字级 uni-gram 词元）。"""
    # query 词元（字级）= {revenue, 香, 港}。句2 与之交集为空（晴/天/出/游/愉/快），不达 2。
    text = "REVENUE 香港 下降。晴天 出游 愉快。"
    out = ExtractiveCompressor(min_overlap=2).compress("REVENUE 香港", text)
    assert "REVENUE 香港 下降" in out
    assert "晴天" not in out  # 与 query 零交集 -> 不达 2，被丢弃


def test_invalid_min_overlap_raises():
    with pytest.raises(ValueError):
        ExtractiveCompressor(min_overlap=0)


def test_postprocessor_preserves_lineage_and_count():
    """CompressionPostprocessor：只改 text，doc_id/locator/scores/sensitivity 原样；片段数不变。"""
    snippets = [
        {
            "chunk_id": "a",
            "text": "香港 收入 下降。无关 句子 内容。",
            "doc_id": "HK.pdf",
            "source_locator": "p1",
            "scores": {"fused": 0.9},
            "sensitivity": "INTERNAL",
        },
        {
            "chunk_id": "b",
            "text": "完全 无关 的 内容 句。",
            "doc_id": "X.pdf",
            "source_locator": "p2",
            "scores": {"fused": 0.5},
            "sensitivity": "INTERNAL",
        },
    ]
    out = CompressionPostprocessor().postprocess("香港 收入", snippets)
    assert len(out) == 2, "压缩绝不删片段"
    # a 被 trim（去掉无关句）。
    assert out[0]["doc_id"] == "HK.pdf"
    assert out[0]["source_locator"] == "p1"
    assert out[0]["scores"] == {"fused": 0.9}
    assert out[0]["sensitivity"] == "INTERNAL"
    assert "无关" not in out[0]["text"]
    assert "香港 收入 下降" in out[0]["text"]
    # b 无句达标 -> 原文保留（不盲删整片段）。
    assert out[1]["text"] == "完全 无关 的 内容 句。"
    # 原 dict 不被就地改（浅拷贝语义）。
    assert "无关" in snippets[0]["text"]


def test_deterministic():
    snippets = [{"chunk_id": "a", "text": "香港 收入 下降。无关 内容。"}]
    cp = CompressionPostprocessor()
    assert cp.postprocess("香港", snippets) == cp.postprocess("香港", snippets)
