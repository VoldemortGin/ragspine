"""页级父子开关接线：ServiceConfig（RAGSPINE_PAGE_PARENT）与本地 facade（RetrievalPreset.page_parent）。

默认 page+child（原为 off；依据见 CHANGELOG Unreleased）：.md（按页切块）的检索结果每页只出现一次、
代表块带整页上下文（prompt_text）与页级 locator（parent_locator）。显式 off 时不做按页去重。
"""

from pathlib import Path

import pytest

from ragspine.agent.llm_provider import MockProvider

# 一页两段：默认切块（480 字）下第 1 页切成多个块，第 2 页一个块。
_MD = (
    "# Results\n\n"
    + "\n\n".join(
        f"Agency channel growth paragraph {i} lifted new business value." * 3 for i in range(6)
    )
    + "\n\n<!-- PageBreak -->\n\n# Outlook\n\nAgency channel outlook remains positive.\n"
)


def _capture_snippets(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, object]]]:
    import ragspine.session as session_module
    from ragspine.agent.agent import AgentResult

    captured: list[list[dict[str, object]]] = []

    def fake_answer(question, store, provider, *, reference_date, narrative_retriever):
        captured.append(narrative_retriever.retrieve(question))
        return AgentResult(answer="", route="narrative", sources=[])

    monkeypatch.setattr(session_module, "answer_question", fake_answer)
    return captured


def test_service_config_defaults_page_child_and_reads_env():
    from ragspine.service.config import ServiceConfig

    assert ServiceConfig(db_path="x.db").page_parent == "page+child"
    assert ServiceConfig.from_env({}).page_parent == "page+child"
    assert ServiceConfig.from_env({"RAGSPINE_PAGE_PARENT": "off"}).page_parent == "off"
    assert ServiceConfig.from_env({"RAGSPINE_PAGE_PARENT": "dedup"}).page_parent == "dedup"
    assert (
        ServiceConfig.from_env({"RAGSPINE_PAGE_PARENT": "page+child"}).page_parent == "page+child"
    )


@pytest.mark.parametrize("mode", ["off", "dedup", "page+child"])
def test_open_narrative_retriever_threads_the_switch(tmp_path, mode):
    from ragspine.service.config import ServiceConfig, open_narrative_retriever

    db = str(tmp_path / "k.db")
    config = ServiceConfig(db_path=db, chunk_db_path=db, embedding="none", page_parent=mode)
    with open_narrative_retriever(config, MockProvider()) as retriever:
        # 默认 query_transform / corrective 都返回 base 本身。
        assert retriever.index.page_parent == mode


def test_facade_defaults_page_child(tmp_path):
    from ragspine import RAGSpine
    from ragspine.service.config import RetrievalProfile, make_retrieval_preset

    for profile in RetrievalProfile:
        assert make_retrieval_preset(profile).page_parent == "page+child"
    rag = RAGSpine.local(tmp_path / "ws")
    assert rag.retrieval.page_parent == "page+child"
    assert rag._service_config().page_parent == "page+child"
    explicit_off = RAGSpine.local(
        tmp_path / "ws", retrieval=make_retrieval_preset(page_parent="off")
    )
    assert explicit_off._service_config().page_parent == "off"


def test_build_narrative_retriever_defaults_page_child(tmp_path):
    from ragspine.retrieval.link.narrative_link import build_narrative_retriever

    retriever, store = build_narrative_retriever(tmp_path / "k.db")
    try:
        assert retriever.index.page_parent == "page+child"
    finally:
        store.close()
    retriever, store = build_narrative_retriever(tmp_path / "k.db", page_parent="off")
    try:
        assert retriever.index.page_parent == "off"
    finally:
        store.close()


def test_facade_switch_dedups_pages(tmp_path: Path, monkeypatch):
    from ragspine import RAGSpine

    doc = tmp_path / "deck.md"
    doc.write_text(_MD, encoding="utf-8")
    captured = _capture_snippets(monkeypatch)

    from ragspine.service.config import make_retrieval_preset

    off = RAGSpine.local(tmp_path / "ws", retrieval=make_retrieval_preset(page_parent="off"))
    off.ingest(doc)
    off.ask("agency channel new business value")
    off_pages = [str(s["source_locator"]).split("#")[0] for s in captured[-1]]
    assert len(off_pages) > len(set(off_pages)), "off 下同一页会被多个块命中"

    rag = RAGSpine.local(tmp_path / "ws", retrieval=make_retrieval_preset(page_parent="dedup"))
    assert rag._service_config().page_parent == "dedup"
    rag.ask("agency channel new business value")
    snippets = captured[-1]
    pages = [s["parent_locator"] for s in snippets]
    assert pages == list(dict.fromkeys(pages))
    assert set(pages) == {"deck.md@page=1", "deck.md@page=2"}
    first = snippets[pages.index("deck.md@page=1")]
    assert str(first["source_locator"]).startswith("deck.md@page=1#para")
    assert str(first["text"]) in str(first["prompt_text"])
    assert len(str(first["prompt_text"])) > len(str(first["text"]))
