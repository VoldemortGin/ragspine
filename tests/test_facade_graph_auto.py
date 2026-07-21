"""Public contracts for the high-level ``graph="auto"`` experience.

The low-level graph tests cover extraction and store mechanics.  These tests pin the
user-facing promise: enabling graph mode is one facade option, graph knowledge survives
workspace reopen, and model-supplied provenance can never escape as an answer source.
"""

import json
from pathlib import Path
from typing import Any

from corespine import ChatCompletion, Choice, ResponseMessage


def _completion(text: str) -> ChatCompletion:
    message = ResponseMessage(role="assistant", content=text)
    return ChatCompletion(choices=(Choice(index=0, message=message, finish_reason="stop"),))


class GraphAutoProvider:
    """Offline provider whose responses are selected by the public prompt purpose."""

    def __init__(self) -> None:
        self.graph_extract_calls = 0
        self.graph_answer_calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion:
        system = str(messages[0].get("content", "")) if messages else ""
        if "知识图谱抽取器" in system:
            self.graph_extract_calls += 1
            return _completion(
                json.dumps(
                    {
                        "entities": [
                            {
                                "name": "渠道扩张",
                                "type": "driver",
                                "source_doc_id": "invented-secret.pdf",
                                "source_locator": "invented-secret.pdf#page=99",
                            },
                            {"name": "交付风险", "type": "risk"},
                        ],
                        "relations": [
                            {
                                "source": "渠道扩张",
                                "target": "交付风险",
                                "kind": "increases",
                                "source_doc_id": "invented-secret.pdf",
                                "source_locator": "invented-secret.pdf#page=99",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        if "社区主题综述器" in system:
            return _completion("渠道扩张与交付风险构成共同的运营主题。")

        # Graph answer synthesis is deliberately textual.  The facade remains
        # responsible for attaching code-verified provenance to the result.
        self.graph_answer_calls += 1
        return _completion("共同主题是渠道扩张带来的交付风险。")


def test_default_facade_remains_graph_free_and_backward_compatible(tmp_path: Path) -> None:
    """Omitting ``graph`` preserves the lean facade's existing observable result."""
    from ragspine import RAGSpine

    source = tmp_path / "review.txt"
    source.write_text("营收增长来自渠道扩张。", encoding="utf-8")

    with RAGSpine.local(tmp_path / "workspace") as rag:
        report = rag.ingest(source)

    assert not report.failed
    assert report.narrative_report is not None
    assert report.narrative_report.files[0].n_chunks == 1


def test_graph_auto_ingest_is_queryable_after_workspace_reopen(tmp_path: Path) -> None:
    """One opt-in builds durable graph knowledge and auto-routes a thematic ask."""
    from ragspine import RAGSpine

    workspace = tmp_path / "workspace"
    source = tmp_path / "operating-review.txt"
    source.write_text("渠道快速扩张使多个区域共同面临交付风险。", encoding="utf-8")
    ingest_provider = GraphAutoProvider()

    with RAGSpine.local(workspace, provider=ingest_provider, graph="auto") as rag:
        report = rag.ingest(source)

    assert not report.failed
    assert ingest_provider.graph_extract_calls == 1

    ask_provider = GraphAutoProvider()
    with RAGSpine.local(workspace, provider=ask_provider, graph="auto") as rag:
        result = rag.ask("这些业务共同面临的主要风险是什么？")

    assert result.route == "graph"
    assert "交付风险" in result.answer
    assert ask_provider.graph_answer_calls == 1
    assert result.sources
    assert {source["doc"] for source in result.sources} == {"operating-review.txt"}


def test_graph_auto_never_trusts_model_reported_source_lineage(tmp_path: Path) -> None:
    """Graph answers expose only caller-stamped source lineage, never model claims."""
    from ragspine import RAGSpine

    workspace = tmp_path / "workspace"
    source = tmp_path / "real-review.txt"
    source.write_text("渠道扩张与交付风险相关。", encoding="utf-8")

    with RAGSpine.local(workspace, provider=GraphAutoProvider(), graph="auto") as rag:
        rag.ingest(source)
        result = rag.ask("整体有哪些共同风险？")

    assert result.route == "graph"
    assert result.sources
    assert {item["doc"] for item in result.sources} == {"real-review.txt"}
    assert all("invented-secret.pdf" not in json.dumps(item) for item in result.sources)


def test_graph_auto_keeps_deterministic_external_entity_refusal(tmp_path: Path) -> None:
    """The graph route cannot bypass the always-on competitor security gate."""
    from ragspine import RAGSpine

    workspace = tmp_path / "workspace"
    source = tmp_path / "review.txt"
    source.write_text("渠道扩张与交付风险相关。", encoding="utf-8")
    provider = GraphAutoProvider()

    with RAGSpine.local(workspace, provider=provider, graph="auto") as rag:
        rag.ingest(source)
        calls_before_ask = provider.graph_answer_calls
        result = rag.ask("竞安集团整体有哪些共同风险？")

    assert result.route == "graph"
    assert "不掌握" in result.answer
    assert result.sources == []
    assert provider.graph_answer_calls == calls_before_ask


def test_graph_auto_does_not_capture_structured_numeric_questions(tmp_path: Path) -> None:
    """A global cue cannot bypass the structured channel's anti-fabrication path."""
    from ragspine import RAGSpine

    workspace = tmp_path / "workspace"
    source = tmp_path / "review.txt"
    source.write_text("渠道扩张与交付风险相关。", encoding="utf-8")

    with RAGSpine.local(workspace, provider=GraphAutoProvider(), graph="auto") as rag:
        rag.ingest(source)
        result = rag.ask("整体营收是多少？")

    assert result.route != "graph"
