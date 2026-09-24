"""页级父子（dedup / page+child）的 RESTRICTED 隔离不变量绑定（conformance）。

钉死：
    - 父块展开时，同一页里的 RESTRICTED 块绝不被拼进上下文（prompt_text）；
    - 整页单元（page+child 的整页 BM25 通道）不含 RESTRICTED 文本；
    - 两个出口不被绕过：RESTRICTED 块本身照常在 link 出口被剔除、绝不进 judge（rerank 出口）；
    - 反向证明：同一 RESTRICTED 块在索引层确实被命中（测试有牙齿），只是被出口挡下。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

SECRET = "Falcon acquisition Singapore VONB board plan."
MODES = ("dedup", "page+child")


def _chunk(seq: int, text: str, para: int, sensitivity: str = "INTERNAL") -> Chunk:
    return Chunk(
        chunk_id=f"deck.md#c{seq}",
        doc_id="deck.md",
        seq=seq,
        text=text,
        source_locator=f"deck.md@page=4#para{para}",
        para_start=para,
        para_end=para,
        sensitivity=sensitivity,
    )


@pytest.fixture
def store(tmp_path):
    s = ChunkStore(tmp_path / "chunks.db")
    s.init_schema()
    # 同一页：RESTRICTED 块夹在两块公开块中间，且与查询最相关（排第一）。
    s.replace_doc_chunks(
        "deck.md",
        [
            _chunk(0, "Singapore VONB overview.", 1),
            _chunk(1, SECRET, 2, sensitivity="RESTRICTED"),
            _chunk(2, "Singapore margins stable.", 3),
        ],
    )
    yield s
    s.close()


class _RecordingJudge:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        self.seen.extend(candidates)
        return list(range(len(candidates)))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("with_judge", [False, True])
def test_restricted_sibling_never_reaches_page_context(store, mode, with_judge):
    judge = _RecordingJudge() if with_judge else None
    index = NarrativeIndex(store, page_parent=mode, judge=judge)
    snippets = NarrativeIndexRetriever(index).retrieve("Falcon acquisition Singapore VONB")

    assert snippets, "公开的同页块应仍被返回"
    for s in snippets:
        assert s["sensitivity"] != "RESTRICTED"
        for field in ("text", "prompt_text"):
            assert "Falcon" not in str(s.get(field, ""))
    page = next(s for s in snippets if s.get("parent_locator") == "deck.md@page=4")
    assert page["prompt_text"] == "Singapore VONB overview.\nSingapore margins stable."
    if judge is not None:
        assert all("Falcon" not in c for c in judge.seen)


@pytest.mark.parametrize("mode", MODES)
def test_restricted_only_terms_recall_nothing(store, mode):
    index = NarrativeIndex(store, page_parent=mode)
    assert NarrativeIndexRetriever(index).retrieve("Falcon board plan") == []


@pytest.mark.parametrize("mode", MODES)
def test_reverse_proof_restricted_chunk_is_a_raw_hit(store, mode):
    """反向证明：RESTRICTED 块在索引层确实被命中，挡住它的是出口而不是没召回。"""
    raw = NarrativeIndex(store, page_parent=mode).retrieve("Falcon board plan")
    assert [r.chunk.chunk_id for r in raw] == ["deck.md#c1"]
    assert raw[0].chunk.sensitivity == "RESTRICTED"
    assert not getattr(raw[0].chunk, "window_text", "")
