"""High-level, offline-first facade for a local RAGSpine workspace."""

from dataclasses import dataclass
from pathlib import Path

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.llm_provider import LLMProvider, MockProvider
from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.ingestion.narrative.narrative_ingest import (
    NarrativeIngestReport,
    ingest_narrative,
)
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.ingestion.structured.ingestion import IngestReport, ingest_file
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.service.config import (
    RetrievalPreset,
    RetrievalProfile,
    ServiceConfig,
    make_retrieval_preset,
    open_narrative_retriever,
)
from ragspine.storage.fact_store import SqliteFactStore

_STRUCTURED_SUFFIXES = frozenset({".xlsx", ".xlsm", ".pptx", ".pdf", ".docx", ".docm"})
_NARRATIVE_SUFFIXES = frozenset({".pptx", ".pdf", ".docx", ".docm", ".txt"})
_SUPPORTED_SUFFIXES = _STRUCTURED_SUFFIXES | _NARRATIVE_SUFFIXES


@dataclass(frozen=True)
class IngestResult:
    """One stable result for ingestion across both knowledge channels."""

    structured_reports: tuple[IngestReport, ...] = ()
    narrative_report: NarrativeIngestReport | None = None

    @property
    def failed(self) -> bool:
        """Whether any attempted channel reported a hard failure."""
        structured_failed = any(report.status == "failed" for report in self.structured_reports)
        narrative_failed = (
            self.narrative_report is not None and self.narrative_report.counts()["failed"] > 0
        )
        return structured_failed or narrative_failed

    @property
    def summary(self) -> str:
        """Return a concise, deterministic human-readable summary."""
        facts = sum(report.n_facts_ingested for report in self.structured_reports)
        chunks = 0
        if self.narrative_report is not None:
            chunks = sum(report.n_chunks for report in self.narrative_report.files)
        status = "failed" if self.failed else "ok"
        return f"status={status} files={self.file_count} facts={facts} chunks={chunks}"

    @property
    def file_count(self) -> int:
        """Count distinct source paths represented in the result."""
        paths = {report.source_path for report in self.structured_reports}
        if self.narrative_report is not None:
            paths.update(report.path for report in self.narrative_report.files)
        return len(paths)


class RAGSpine:
    """A small high-level facade over RAGSpine's composable domain APIs.

    Use :meth:`local` for the common offline workspace. Advanced users can keep
    using the underlying stores, providers, and ``answer_question`` directly.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        provider: LLMProvider | None = None,
        retrieval: RetrievalPreset | None = None,
    ):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "knowledge.db"
        self.mapping_db_path = self.workspace / "mapping.db"
        self.review_db_path = self.workspace / "review.db"
        self.provider = provider or MockProvider()
        self.retrieval = retrieval or make_retrieval_preset()
        self._closed = False
        self._initialize_workspace()

    @classmethod
    def local(
        cls,
        workspace: str | Path = ".ragspine",
        *,
        provider: LLMProvider | None = None,
        profile: RetrievalProfile | str = RetrievalProfile.ECONOMY,
        retrieval: RetrievalPreset | None = None,
    ) -> "RAGSpine":
        """Open an offline workspace using a named or explicitly supplied preset."""
        return cls(
            workspace,
            provider=provider,
            retrieval=retrieval or make_retrieval_preset(profile),
        )

    def __enter__(self) -> "RAGSpine":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the facade; per-operation resources are already deterministic."""
        self._closed = True

    def ask(self, question: str) -> AgentResult:
        """Answer from this workspace using the existing guarded agent path."""
        self._ensure_open()
        store = SqliteFactStore(self.db_path)
        store.init_schema()
        config = ServiceConfig(
            db_path=str(self.db_path),
            chunk_db_path=str(self.db_path),
            retrieval_mode=self.retrieval.retrieval_mode,
            embedding=self.retrieval.embedding,
            vector_store=self.retrieval.vector_store,
            reranker=self.retrieval.reranker,
            postprocessor=self.retrieval.postprocessor,
        )
        try:
            with open_narrative_retriever(config, self.provider) as retriever:
                return answer_question(
                    question,
                    store,
                    self.provider,
                    narrative_retriever=retriever,
                )
        finally:
            store.close()

    def ingest(
        self,
        source: str | Path,
        *,
        dry_run: bool = False,
        valid_as_of: str | None = None,
    ) -> IngestResult:
        """Ingest a file or directory into every applicable knowledge channel."""
        self._ensure_open()
        paths = self._resolve_sources(source)
        structured_paths = [path for path in paths if path.suffix.lower() in _STRUCTURED_SUFFIXES]
        narrative_paths = [path for path in paths if path.suffix.lower() in _NARRATIVE_SUFFIXES]

        structured_reports: list[IngestReport] = []
        if structured_paths:
            store = SqliteFactStore(self.db_path)
            registry = MappingRegistry(self.mapping_db_path)
            queue = ReviewQueue(self.review_db_path)
            store.init_schema()
            registry.init_schema()
            queue.init_schema()
            try:
                structured_reports = [
                    ingest_file(
                        path,
                        store,
                        registry,
                        queue,
                        dry_run=dry_run,
                        valid_as_of=valid_as_of,
                    )
                    for path in structured_paths
                ]
            finally:
                queue.close()
                registry.close()
                store.close()

        narrative_report = None
        if narrative_paths:
            chunk_store = ChunkStore(self.db_path)
            chunk_store.init_schema()
            try:
                narrative_report = ingest_narrative(
                    list[str | Path](narrative_paths), chunk_store, dry_run=dry_run
                )
            finally:
                chunk_store.close()

        return IngestResult(tuple(structured_reports), narrative_report)

    def _resolve_sources(self, source: str | Path) -> list[Path]:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"source does not exist: {path}")
        candidates = (
            [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
        )
        supported = [item for item in candidates if item.suffix.lower() in _SUPPORTED_SUFFIXES]
        if path.is_file() and not supported:
            supported_text = ", ".join(sorted(_SUPPORTED_SUFFIXES))
            raise ValueError(
                f"unsupported source type {path.suffix or '(none)'}; expected {supported_text}"
            )
        if not supported:
            raise ValueError(f"no supported documents found in: {path}")
        return supported

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("RAGSpine workspace is closed")

    def _initialize_workspace(self) -> None:
        """Create the three durable local resources eagerly and close them."""
        store = SqliteFactStore(self.db_path)
        registry = MappingRegistry(self.mapping_db_path)
        queue = ReviewQueue(self.review_db_path)
        try:
            store.init_schema()
            registry.init_schema()
            queue.init_schema()
        finally:
            queue.close()
            registry.close()
            store.close()


__all__ = ["IngestResult", "RAGSpine"]
