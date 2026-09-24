"""页级父子 + 按页去重（RAGSPINE_PAGE_PARENT=off|dedup|page+child）。

钉死：
    - 页标识从 locator 解析：'{doc}@page=N#...' → (doc_id, N)；没有页码的块原样保留、不去重；
    - dedup：同一页只返回一次，代表块 = 该页排名最好的块（首次出现），top-k 按页计数；
      开了 rerank（judge）也保持按页去重；
    - 父块展开：prompt_text = 同页全部块按 seq 拼接、去掉段落回带重叠；text / source_locator 仍是
      命中的代表块，parent_locator = 页级 locator '{doc}@page=N'；整页过长时截断，命中块一定在窗口内；
    - page+child：子块页排名（dedup）与整页 BM25 排名两路 RRF；
    - trace 只记计数。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.lexical import retrieval as retrieval_mod
from ragspine.retrieval.lexical.retrieval import (
    DEFAULT_RRF_K,
    NarrativeIndex,
    bm25_scores,
    rrf_fuse,
    tokenize,
)
from ragspine.retrieval.link.narrative_link import (
    NarrativeIndexRetriever,
    build_narrative_retriever,
)
from ragspine.retrieval.page_parent.pages import make_page_parent_mode, page_key, page_locator
from ragspine.retrieval.page_parent.window import page_window

from .conftest import SECRET, page_corpus

PAGE1_FULL = (
    "Singapore revenue grew 10 percent.\n"
    "VONB rose strongly in Singapore.\n"
    "Margins improved in Hong Kong.\n"
    "Outlook\n"
    "We expect Singapore growth to continue."
)


def _deck():
    return page_corpus()["deck.md"]


def _retriever(store: ChunkStore, mode: str, **kwargs) -> NarrativeIndexRetriever:
    return NarrativeIndexRetriever(NarrativeIndex(store, page_parent=mode, **kwargs))


def _key(snippet: dict) -> tuple[str, str]:
    return snippet["doc_id"], snippet.get("parent_locator") or snippet["source_locator"]


# ---------------------------------------------------------------------------
# 开关解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (None, "off"),
        ("", "off"),
        ("off", "off"),
        ("none", "off"),
        ("dedup", "dedup"),
        ("page+child", "page+child"),
        (" Page+Child ", "page+child"),
    ],
)
def test_make_page_parent_mode(spec, expected):
    assert make_page_parent_mode(spec) == expected


def test_unknown_mode_is_rejected(page_store):
    with pytest.raises(ValueError, match="page_parent"):
        make_page_parent_mode("pages")
    with pytest.raises(ValueError, match="page_parent"):
        NarrativeIndex(page_store, page_parent="pages")


# ---------------------------------------------------------------------------
# 页标识
# ---------------------------------------------------------------------------


def test_page_key_parses_segment_locator():
    deck = _deck()
    assert page_key(deck[0]) == ("deck.md", 1)
    assert page_key(deck[3]) == ("deck.md", 2)
    assert page_locator(deck[3]) == "deck.md@page=2"


def test_chunk_without_page_has_no_key():
    legacy = page_corpus()["legacy.pdf"][0]
    assert page_key(legacy) is None
    assert page_locator(legacy) == ""


def test_page_key_uses_structured_doc_id_and_ignores_non_page_segments():
    chunk = _deck()[0]
    from dataclasses import replace

    assert page_key(replace(chunk, source_locator="deck.md@page=12")) == ("deck.md", 12)
    assert page_key(replace(chunk, source_locator="deck.md@slide=2#para1")) is None


# ---------------------------------------------------------------------------
# 父块展开：拼接 + 去 overlap + 截断
# ---------------------------------------------------------------------------


def test_page_window_concatenates_in_seq_order_and_drops_overlap():
    deck = _deck()
    siblings = [deck[2], deck[0], deck[1]]  # 乱序给入，按 seq 拼
    assert page_window(siblings, deck[1]) == PAGE1_FULL
    assert PAGE1_FULL.count("VONB rose strongly") == 1


def test_page_window_truncates_but_keeps_the_hit_chunk():
    deck = _deck()
    siblings = deck[:3]
    for hit in siblings:
        window = page_window(siblings, hit, max_chars=len(hit.text))
        assert hit.text in window
        assert len(window) <= len(hit.text)
    # 预算够两块：命中块 + 一个邻块，仍含完整命中块。
    window = page_window(siblings, deck[2], max_chars=len(PAGE1_FULL) - 5)
    assert deck[2].text in window
    assert len(window) <= len(PAGE1_FULL) - 5


def test_page_window_skips_restricted_siblings():
    deck = _deck()
    window = page_window([deck[3], deck[4]], deck[3])
    assert SECRET not in window
    assert window == deck[3].text


# ---------------------------------------------------------------------------
# 按页去重（端到端经 link 出口）
# ---------------------------------------------------------------------------


def test_dedup_returns_each_page_once_with_the_best_chunk(page_store):
    off = _retriever(page_store, "off").retrieve("Singapore VONB", top_k=50)
    dedup = _retriever(page_store, "dedup").retrieve("Singapore VONB", top_k=50)

    keys = [_key(s) for s in dedup]
    assert len(keys) == len(set(keys))
    # 代表块 = off 排名里该页第一次出现的块；页序 = 首次出现顺序。
    first_seen: dict[tuple[str, str], str] = {}
    for s in off:
        pk = page_locator_of(s)
        first_seen.setdefault(pk, s["chunk_id"])
    assert [s["chunk_id"] for s in dedup] == list(first_seen.values())


def page_locator_of(snippet: dict) -> tuple[str, str]:
    loc = snippet["source_locator"]
    return (snippet["doc_id"], loc.split("#", 1)[0]) if "@page=" in loc else ("", loc)


def test_dedup_counts_top_k_by_page(page_store):
    dedup = _retriever(page_store, "dedup").retrieve("Singapore", top_k=2)
    assert len(dedup) == 2
    assert len({_key(s) for s in dedup}) == 2


def test_chunks_without_page_are_kept_and_not_deduped(page_store):
    dedup = _retriever(page_store, "dedup").retrieve("Singapore legacy", top_k=50)
    legacy = [s for s in dedup if s["doc_id"] == "legacy.pdf"]
    assert [s["source_locator"] for s in legacy] == ["legacy.pdf#para1", "legacy.pdf#para2"]
    assert all("prompt_text" not in s and "parent_locator" not in s for s in legacy)


def test_representative_carries_page_context_and_exact_provenance(page_store):
    dedup = _retriever(page_store, "dedup").retrieve("Margins Hong Kong", top_k=50)
    page1 = next(s for s in dedup if s.get("parent_locator") == "deck.md@page=1")
    assert page1["chunk_id"] == "deck.md#c1"
    assert page1["text"] == _deck()[1].text  # 命中块本身，引用诚实
    assert page1["source_locator"] == "deck.md@page=1#para2-3"  # 代表块的精确 locator
    assert page1["doc_id"] == "deck.md"
    assert page1["prompt_text"] == PAGE1_FULL  # 生成上下文 = 整页


def test_dedup_holds_with_rerank(page_store):
    class ReverseJudge:
        def judge(self, query, candidates):  # noqa: ANN001
            return list(reversed(range(len(candidates))))

    dedup = _retriever(page_store, "dedup", judge=ReverseJudge()).retrieve(
        "Singapore VONB", top_k=50
    )
    keys = [_key(s) for s in dedup]
    assert keys and len(keys) == len(set(keys))
    assert all(s["doc_id"] != "secret.md" for s in dedup)


def test_page_window_budget_is_configurable(page_store):
    index = NarrativeIndex(page_store, page_parent="dedup", page_window_chars=40)
    snippets = NarrativeIndexRetriever(index).retrieve("Margins Hong Kong", top_k=50)
    page1 = next(s for s in snippets if s.get("parent_locator") == "deck.md@page=1")
    assert page1["text"] in page1["prompt_text"]
    assert page1["prompt_text"] != PAGE1_FULL


# ---------------------------------------------------------------------------
# page+child：子块页排名与整页排名两路 RRF
# ---------------------------------------------------------------------------


def test_page_plus_child_fuses_child_and_page_rankings(page_store):
    query = "Singapore growth outlook"
    dedup = _retriever(page_store, "dedup").retrieve(query, top_k=50)
    fused = _retriever(page_store, "page+child").retrieve(query, top_k=50)

    child_rank = [page_locator_of(s) for s in dedup]
    # 整页单元：同页非 RESTRICTED 块去重叠后的整页文本；BM25 排名（得分 > 0）。
    pages = {
        ("deck.md", "deck.md@page=1"): PAGE1_FULL,
        ("deck.md", "deck.md@page=2"): _deck()[3].text,
        ("deck.md", "deck.md@page=3"): _deck()[5].text,
    }
    units = list(pages)
    scores = bm25_scores(tokenize(query), [tokenize(pages[u]) for u in units])
    page_rank = [
        u for s, u in sorted(zip(scores, units, strict=True), key=lambda p: (-p[0], p[1])) if s > 0
    ]
    ids = {u: f"{i}" for i, u in enumerate(dict.fromkeys(child_rank + page_rank))}
    back = {v: k for k, v in ids.items()}
    rrf = rrf_fuse([[ids[u] for u in child_rank], [ids[u] for u in page_rank]], DEFAULT_RRF_K)
    expected = [back[i] for i, _ in sorted(rrf.items(), key=lambda p: (-p[1], int(p[0])))]

    assert [page_locator_of(s) for s in fused] == expected
    assert len(fused) == len({_key(s) for s in fused})


def test_page_plus_child_page_unit_never_contains_restricted_text(page_store):
    # 只有 RESTRICTED 块含这些词：整页单元若混进 RESTRICTED 文本，第 2 页会经整页通道被召回。
    for mode in ("dedup", "page+child"):
        assert _retriever(page_store, mode).retrieve("Falcon codename", top_k=50) == []


# ---------------------------------------------------------------------------
# 装配与 trace
# ---------------------------------------------------------------------------


def test_build_narrative_retriever_threads_the_switch(tmp_path):
    retriever, store = build_narrative_retriever(tmp_path / "c.db", page_parent="page+child")
    try:
        assert retriever.index.page_parent == "page+child"
    finally:
        store.close()
    retriever, store = build_narrative_retriever(tmp_path / "c.db")
    try:
        assert retriever.index.page_parent == "off"
    finally:
        store.close()


def test_trace_records_counts_only(page_store, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(retrieval_mod, "emit_trace", lambda *a, **f: captured.append(f))
    _retriever(page_store, "dedup").retrieve("Singapore VONB", top_k=50)
    (fields,) = [f for f in captured if f.get("op") == "narrative.page_parent"]
    assert fields["page_parent"] == "dedup"
    assert fields["n_chunks"] > fields["n_units"] >= fields["n_returned"] > 0
    assert fields["n_pages_expanded"] >= 1
    assert all(isinstance(v, int | str) for v in fields.values())
    assert SECRET not in repr(fields) and "Singapore" not in repr(fields)


def test_off_mode_emits_no_page_trace(page_store, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(retrieval_mod, "emit_trace", lambda *a, **f: captured.append(f))
    _retriever(page_store, "off").retrieve("Singapore VONB", top_k=50)
    assert not [f for f in captured if f.get("op") == "narrative.page_parent"]
