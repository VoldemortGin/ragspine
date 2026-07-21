"""Durable narrative GraphRAG runtime for the high-level workspace facade."""

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ragspine.agent.agent import AgentResult
from ragspine.agent.llm_provider import LLMProvider, ProviderError
from ragspine.agent.security_gate import SECURITY_ALLOW, SecurityGate
from ragspine.common.company_profile import load_company_profile
from ragspine.graph.config import GraphMode
from ragspine.graph.narrative import (
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
    make_narrative_graph,
)
from ragspine.retrieval.chunking.chunk_store import StoredChunk

_GLOBAL_CUES = (
    "共同",
    "整体",
    "全局",
    "主题",
    "跨文档",
    "cross-cutting",
    "overall",
    "common risk",
    "common theme",
)
_ANSWER_SYSTEM = (
    "你是 GraphRAG 全局问答器。根据带来源的社区合成摘要回答问题。"
    "摘要是合成材料而非权威事实；不得添加材料中没有的事实或数字。"
    "回答应简洁，并明确这是跨文档图谱合成。"
)


def wants_global_graph(question: str) -> bool:
    """Route only questions with strong global/thematic cues into GraphRAG."""
    folded = question.casefold()
    return any(cue in folded for cue in _GLOBAL_CUES)


@dataclass(frozen=True)
class _Source:
    doc_id: str
    locator: str


class WorkspaceGraphRuntime:
    """Persist extracted graph records and answer global thematic questions.

    Model output never supplies lineage: extraction receives the chunk's caller-owned
    document id and locator, and only those stamped values are persisted and exposed.
    """

    def __init__(
        self, path: str | Path, provider: LLMProvider, *, max_communities: int = 20
    ) -> None:
        self.path = str(path)
        self.provider = provider
        if not 1 <= max_communities <= 100:
            raise ValueError("max_communities must be between 1 and 100")
        self.max_communities = max_communities
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS graph_entity (
                    doc_id TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    PRIMARY KEY (doc_id, locator, name, entity_type)
                );
                CREATE TABLE IF NOT EXISTS graph_relation (
                    doc_id TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (doc_id, locator, source, target, kind)
                );
                CREATE INDEX IF NOT EXISTS ix_graph_relation_source
                    ON graph_relation (source, target);
                """
            )

    def index_chunks(self, chunks: Iterable[StoredChunk], *, doc_ids: set[str]) -> None:
        """Replace graph records for selected documents from their active chunks."""
        pipeline = make_narrative_graph("llm", provider=self.provider)
        if pipeline is None:
            return
        selected = sorted(
            (
                chunk
                for chunk in chunks
                if chunk.doc_id in doc_ids and chunk.sensitivity.upper() != "RESTRICTED"
            ),
            key=lambda chunk: (chunk.doc_id, chunk.seq, chunk.source_locator),
        )
        extracted = [
            pipeline.extractor.extract(
                chunk.text,
                source_doc_id=chunk.doc_id,
                source_locator=chunk.source_locator,
            )
            for chunk in selected
        ]
        has_output = any(graph.entities or graph.relations for graph in extracted)
        with closing(self._connect()) as connection, connection:
            if not has_output and doc_ids:
                placeholders = ",".join("?" for _ in doc_ids)
                previous = connection.execute(
                    f"SELECT 1 FROM graph_relation WHERE doc_id IN ({placeholders}) LIMIT 1",
                    tuple(sorted(doc_ids)),
                ).fetchone()
                if previous is not None:
                    return
            for doc_id in sorted(doc_ids):
                connection.execute("DELETE FROM graph_entity WHERE doc_id = ?", (doc_id,))
                connection.execute("DELETE FROM graph_relation WHERE doc_id = ?", (doc_id,))
            for graph in extracted:
                connection.executemany(
                    "INSERT OR REPLACE INTO graph_entity VALUES (?, ?, ?, ?)",
                    [
                        (entity.source_doc_id, entity.source_locator, entity.name, entity.type)
                        for entity in graph.entities
                    ],
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO graph_relation VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            relation.source_doc_id,
                            relation.source_locator,
                            relation.source,
                            relation.target,
                            relation.kind,
                        )
                        for relation in graph.relations
                    ],
                )

    def answer_global(self, question: str) -> AgentResult | None:
        """Return a provenance-bound global graph synthesis, or ``None`` if empty."""
        profile = load_company_profile()
        verdict = SecurityGate(profile.external_entities, profile.home_company_name).screen(
            raw_question=question, metric=None
        )
        if verdict.decision != SECURITY_ALLOW:
            answer = verdict.message or ""
            return AgentResult(answer=answer, answer_plain=answer, route="graph")
        graph = self._load_graph()
        if not graph.relations:
            return None
        pipeline = make_narrative_graph("llm", provider=self.provider)
        if pipeline is None:
            return None
        communities = tuple(
            sorted(
                pipeline.detect_communities(graph),
                key=lambda item: (-item.relation_count, item.id),
            )[: self.max_communities]
        )
        summaries = tuple(pipeline.summarizer.summarize(item, graph) for item in communities)
        if not summaries:
            return None
        source_map = self._sources_by_doc()
        source_ids = sorted({doc for summary in summaries for doc in summary.source_doc_ids})
        sources: list[dict[str, object]] = [
            {"doc": source.doc_id, "locator": source.locator, "kind": "graph_synthesis"}
            for doc_id in source_ids
            for source in source_map.get(doc_id, ())
        ]
        context = "\n".join(
            f"[{summary.community_id}; 来源={','.join(summary.source_doc_ids)}] {summary.text}"
            for summary in summaries
        )
        try:
            response = self.provider.chat(
                [
                    {"role": "system", "content": _ANSWER_SYSTEM},
                    {"role": "user", "content": f"问题：{question}\n社区摘要：\n{context}"},
                ]
            )
            answer = (response.choices[0].message.content or "").strip()
        except ProviderError:
            answer = ""
        if not answer:
            answer = "\n".join(summary.text for summary in summaries)
        if any(character.isdigit() for character in answer):
            answer = (
                "图谱合成检测到未经结构化事实验证的数字，已拒绝输出；"
                "请改用结构化问题查询数字，或改问不含数字的跨文档主题。"
            )
            sources = []
        return AgentResult(
            answer=answer,
            answer_plain=answer,
            route="graph",
            sources=sources,
        )

    def _load_graph(self) -> ExtractedGraph:
        with closing(self._connect()) as connection, connection:
            entity_rows = connection.execute(
                "SELECT * FROM graph_entity ORDER BY doc_id, locator, name, entity_type"
            ).fetchall()
            relation_rows = connection.execute(
                "SELECT * FROM graph_relation ORDER BY doc_id, locator, source, target, kind"
            ).fetchall()
        return ExtractedGraph(
            entities=tuple(
                ExtractedEntity(
                    name=row["name"],
                    type=row["entity_type"],
                    source_doc_id=row["doc_id"],
                    source_locator=row["locator"],
                )
                for row in entity_rows
            ),
            relations=tuple(
                ExtractedRelation(
                    source=row["source"],
                    target=row["target"],
                    kind=row["kind"],
                    source_doc_id=row["doc_id"],
                    source_locator=row["locator"],
                )
                for row in relation_rows
            ),
        )

    def _sources_by_doc(self) -> dict[str, tuple[_Source, ...]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT DISTINCT doc_id, locator FROM graph_relation
                ORDER BY doc_id, locator
                """
            ).fetchall()
        grouped: dict[str, list[_Source]] = {}
        for row in rows:
            source = _Source(doc_id=row["doc_id"], locator=row["locator"])
            grouped.setdefault(source.doc_id, []).append(source)
        return {doc_id: tuple(items) for doc_id, items in grouped.items()}


__all__ = [
    "GraphMode",
    "WorkspaceGraphRuntime",
    "wants_global_graph",
]
