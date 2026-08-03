"""LightRAG 形状适配器的公开契约测试。

钉住「从 LightRAG 迁过来的代码零改动跑起来」这一承诺，同时钉住【不能为了迁就别人的签名而
丢掉家族不变量】：query() 按 LightRAG 返回裸字符串，但 query_with_sources() 必须仍能拿到
带血缘的完整结果；insert() 的裸文本必须落成可溯源的文档，而非无来源的悬空块。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.compat.lightrag import VALID_MODES, LightRAG, QueryParam


@pytest.fixture
def rag(tmp_path):
    with LightRAG(working_dir=tmp_path / "ws") as instance:
        yield instance


def test_import_surface_matches_lightrag(rag) -> None:
    """迁移者写的 `from ... import LightRAG, QueryParam` 与三个核心方法都在。"""
    assert callable(rag.insert)
    assert callable(rag.query)
    assert callable(rag.ainsert)
    assert callable(rag.aquery)
    assert QueryParam().mode == "mix"


def test_constructor_tolerates_lightrag_only_kwargs(tmp_path) -> None:
    """LightRAG 特有的构造参数（llm_model_func 等）被容忍并忽略，不炸。"""
    with LightRAG(
        working_dir=tmp_path / "ws",
        llm_model_func=lambda *a, **k: "",
        embedding_func=lambda *a, **k: [],
        chunk_token_size=1200,
    ) as instance:
        assert instance.working_dir.exists()


def test_insert_text_then_query_returns_plain_string(rag) -> None:
    rag.insert("ACME 在 2024 财年扩大了渠道覆盖，交付周期同时变长。")

    answer = rag.query("渠道覆盖发生了什么变化？")

    assert isinstance(answer, str)
    assert answer


def test_insert_accepts_a_list_like_lightrag(rag) -> None:
    rag.insert(["第一段材料：渠道扩张。", "第二段材料：交付风险上升。"])

    assert len(rag.inserted_documents()) == 2


def test_inserted_text_is_traceable_to_a_document(rag) -> None:
    """裸文本不得变成无来源的悬空内容——必须落成带 doc_id 的可溯源文档。"""
    rag.insert("渠道扩张带动了增长。")

    documents = rag.inserted_documents()
    assert len(documents) == 1
    assert documents[0].suffix == ".txt"
    assert documents[0].exists()


def test_insert_is_idempotent_for_identical_text(rag) -> None:
    rag.insert("完全相同的一段文本。")
    rag.insert("完全相同的一段文本。")

    assert len(rag.inserted_documents()) == 1


def test_query_with_sources_preserves_provenance(rag) -> None:
    """LightRAG 的 query() 签名会吃掉来源；家族不变量经此出口保住。"""
    rag.insert("渠道扩张带动了增长。")

    result = rag.query_with_sources("渠道扩张带来了什么？")

    assert isinstance(result.answer, str)
    assert isinstance(result.sources, list)
    assert result.route


def test_all_lightrag_modes_are_accepted(rag) -> None:
    rag.insert("一段用于检索的材料。")

    for mode in VALID_MODES:
        answer = rag.query("材料讲了什么？", param=QueryParam(mode=mode))
        assert isinstance(answer, str)


def test_unknown_mode_is_rejected(rag) -> None:
    with pytest.raises(ValueError, match="mode"):
        rag.query("随便问问", param=QueryParam(mode="telepathy"))


def test_async_variants_mirror_sync(rag) -> None:
    import asyncio

    asyncio.run(rag.initialize_storages())
    asyncio.run(rag.ainsert("异步插入的一段材料。"))
    answer = asyncio.run(rag.aquery("材料讲了什么？"))

    assert isinstance(answer, str)
    assert len(rag.inserted_documents()) == 1


def test_query_is_deterministic_across_calls(rag) -> None:
    rag.insert("确定性检查用的材料。")

    assert rag.query("材料讲了什么？") == rag.query("材料讲了什么？")
