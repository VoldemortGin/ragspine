"""跨语言查询翻译开关接线：RAGSPINE_QUERY_TRANSLATION / ServiceConfig / RetrievalPreset → open_narrative_retriever。"""

from pathlib import Path

import pytest

from ragspine.retrieval.chunking.chunk_store import ChunkStore

from ..retrieval.contextual_index.conftest import load_heading_corpus
from ..retrieval.query_translation.conftest import TranslatingProvider

_ZH_Q = "分销渠道 占比"


def _store(tmp_path: Path) -> Path:
    db = tmp_path / "chunks.db"
    store = ChunkStore(db)
    try:
        load_heading_corpus(store)
    finally:
        store.close()
    return db


def test_env_switch_parses():
    from ragspine.service.config import ServiceConfig

    assert ServiceConfig.from_env({}).query_translation == "auto"
    assert ServiceConfig.from_env({"RAGSPINE_QUERY_TRANSLATION": "off"}).query_translation == "off"
    assert (
        ServiceConfig.from_env({"RAGSPINE_QUERY_TRANSLATION": "auto"}).query_translation == "auto"
    )


def test_preset_override():
    from ragspine.service.config import make_retrieval_preset

    for profile in ("economy", "balanced", "quality"):
        assert make_retrieval_preset(profile).query_translation == "auto"
    assert make_retrieval_preset("balanced", query_translation="off").query_translation == "off"
    preset = make_retrieval_preset("balanced").with_overrides(query_translation="off")
    assert preset.query_translation == "off"


@pytest.mark.parametrize(("mode", "calls"), [("off", 0), ("auto", 1)])
def test_open_narrative_retriever_uses_the_provider(tmp_path, mode, calls):
    from ragspine.service.config import ServiceConfig, open_narrative_retriever

    db = _store(tmp_path)
    provider = TranslatingProvider({_ZH_Q: "Agency Partnerships share"})
    config = ServiceConfig(
        db_path=str(db), chunk_db_path=str(db), embedding="none", query_translation=mode
    )
    with open_narrative_retriever(config, provider) as retriever:
        hits = retriever.retrieve(_ZH_Q)
    translations = [m for m in provider.calls if m[0]["content"].startswith("You translate")]
    assert len(translations) == calls
    # 精排（provider listwise）拿到的仍是原问题
    assert all(_ZH_Q in m[-1]["content"] for m in provider.calls if m not in translations)
    assert bool(hits) is (mode == "auto")


def test_invalid_mode_raises(tmp_path):
    from ragspine.service.config import ServiceConfig, open_narrative_retriever

    db = _store(tmp_path)
    config = ServiceConfig(db_path=str(db), chunk_db_path=str(db), query_translation="always")
    with pytest.raises(ValueError):
        with open_narrative_retriever(config, TranslatingProvider()):
            pass
