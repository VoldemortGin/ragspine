"""旧 W8 入口复用现代后处理器，不引入同名 package 遮蔽。"""

import pytest

from ragspine.retrieval import postprocess


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("diversity", postprocess.MMRPostprocessor),
        ("reorder", postprocess.LostInTheMiddlePostprocessor),
        ("long_context", postprocess.LostInTheMiddlePostprocessor),
        ("lost_in_the_middle", postprocess.LostInTheMiddlePostprocessor),
        ("extractive", postprocess.CompressionPostprocessor),
    ],
)
def test_branch_aliases_reuse_current_implementations(spec, expected):
    assert isinstance(postprocess.make_postprocessor(spec), expected)


@pytest.mark.parametrize("spec", ["recommended", "all", "default"])
def test_branch_presets_keep_current_provenance(spec):
    original = {"chunk_id": "a", "text": "Revenue grew. unrelated!", "source_locator": "d#p1"}
    chain = postprocess.make_postprocessor(spec)
    assert isinstance(chain, postprocess.ChainPostprocessor)
    result = chain.postprocess("Revenue", [original])
    assert result[0]["text"] == original["text"]
    assert result[0]["source_locator"] == original["source_locator"]
    assert "prompt_text" in result[0]
    assert "prompt_text" not in original


def test_wrapper_passes_query_filters_limit_and_processes_once(monkeypatch):
    from ragspine.retrieval.postprocess import make_postprocessing_retriever

    class Base:
        calls = []

        def retrieve(self, query, *, filters=None, top_k=50):
            self.calls.append((query, filters, top_k))
            return [{"chunk_id": str(i), "text": str(i)} for i in range(top_k)]

    base = Base()
    monkeypatch.delenv("RAGSPINE_POSTPROCESSOR", raising=False)
    assert make_postprocessing_retriever(base) is base
    wrapped = make_postprocessing_retriever(base, "reorder")
    actual = wrapped.retrieve("q", filters={"entity": "ACME"}, top_k=4)
    assert base.calls == [("q", {"entity": "ACME"}, 4)]
    expected = postprocess.LostInTheMiddlePostprocessor().postprocess(
        "q", [{"chunk_id": str(i), "text": str(i)} for i in range(4)]
    )
    assert actual == expected


def test_current_module_not_shadowed_by_legacy_package():
    assert postprocess.__file__.endswith("postprocess.py")
