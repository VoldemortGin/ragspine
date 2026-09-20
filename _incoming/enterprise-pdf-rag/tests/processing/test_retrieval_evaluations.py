"""A later rerank evaluation never overwrites an earlier failed observation."""

from pathlib import Path

from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.retrieval_evaluations import save_evaluation


def test_evaluation_records_are_versioned_and_preserve_legacy_failure(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path)
    legacy = store.root / "runs" / ("a" * 64)
    legacy.mkdir(parents=True)
    (legacy / "retrieval-example.json").write_bytes(b'{"failed":true}')
    (legacy / "retrieval-validation.json").write_bytes(b'{"all_scores":1}')
    first = save_evaluation(store, "a" * 64, b'{"hit":1}', b'{"model_config":"v1"}')
    later = save_evaluation(store, "a" * 64, b'{"hit":2}', b'{"model_config":"v2"}')
    assert first != later
    assert first[0].read_bytes() == b'{"hit":1}'
    assert later[1].read_bytes() == b'{"model_config":"v2"}'
    assert (legacy / "retrieval-example.json").read_bytes() == b'{"failed":true}'
    assert (
        save_evaluation(store, "a" * 64, b'{"hit":2}', b'{"model_config":"v2"}')
        == later
    )
