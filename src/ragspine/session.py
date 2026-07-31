"""High-level, offline-first facade for a local RAGSpine workspace."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.intent import ROUTE_NARRATIVE, RuleIntentParser
from ragspine.agent.llm_provider import AnthropicProvider, LLMProvider, MockProvider
from ragspine.config import EffectivePlan, RAGSpineConfig, SourceEntry, resolve_config
from ragspine.config.resolution import PresetName
from ragspine.config.workspace import WorkspaceIndexMetadata
from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.graph.config import GraphMode, normalize_graph_mode
from ragspine.ingestion.narrative.narrative_ingest import (
    NarrativeIngestReport,
    ingest_narrative,
)
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.ingestion.structured.ingestion import IngestReport, ingest_file
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunker import make_chunker
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


def _replace_plan_config(
    plan: EffectivePlan,
    config: RAGSpineConfig,
    *,
    prefix: str | None = None,
    paths: set[str] | None = None,
) -> EffectivePlan:
    """Reflect an explicit legacy runtime override in an immutable effective plan."""
    overridden = paths or set()
    sources = tuple(
        SourceEntry(
            path=entry.path,
            source=(
                "config"
                if entry.path in overridden or (prefix is not None and entry.path.startswith(prefix))
                else entry.source
            ),
        )
        for entry in plan.sources
    )
    return EffectivePlan(
        config=config,
        sources=sources,
        index_fingerprint=plan.index_fingerprint,
    )


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
        graph: GraphMode | str = "off",
        _config: RAGSpineConfig | None = None,
        _effective_plan: EffectivePlan | None = None,
    ):
        resolved = _config or RAGSpineConfig()
        storage = resolved.storage
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / storage.knowledge_db
        self.mapping_db_path = self.workspace / storage.mapping_db
        self.review_db_path = self.workspace / storage.review_db
        self.graph_db_path = self.workspace / storage.graph_db
        self.graph_max_communities = resolved.graph.max_communities
        self.allowed_upload_root = (
            Path(resolved.security.allowed_upload_root).resolve()
            if resolved.security.allowed_upload_root is not None
            else None
        )
        self.reference_date = resolved.generation.reference_date
        self.indexing = resolved.indexing
        self.provider = provider or MockProvider()
        self.retrieval = retrieval or make_retrieval_preset()
        self.graph = normalize_graph_mode(graph)
        self._effective_plan = _effective_plan or resolve_config(config=resolved)
        self._index_metadata = WorkspaceIndexMetadata(self.db_path, self.workspace)
        self._closed = False
        self._initialize_workspace()

    @classmethod
    def local(
        cls,
        workspace: str | Path = ".ragspine",
        *,
        provider: LLMProvider | None = None,
        preset: str | None = None,
        profile: RetrievalProfile | str | None = None,
        retrieval: RetrievalPreset | None = None,
        graph: GraphMode | str | None = None,
        config: RAGSpineConfig | Mapping[str, object] | None = None,
    ) -> "RAGSpine":
        """Open an offline workspace from strict config plus optional legacy overrides."""
        if preset is not None and profile is not None:
            raise ValueError("preset and legacy profile cannot be supplied together")

        resolution_input: RAGSpineConfig | Mapping[str, object] | None = config
        if profile is not None and config is not None:
            # Legacy ``profile=`` historically replaced config-level retrieval
            # overrides wholesale.  Keep that boundary contract while the new
            # ``preset=`` spelling follows resolve_config's layered semantics.
            payload = (
                config.model_dump(mode="python", exclude_unset=True)
                if isinstance(config, RAGSpineConfig)
                else dict(config)
            )
            payload.pop("profile", None)
            payload.pop("retrieval", None)
            resolution_input = payload
        plan = resolve_config(
            preset=cast("PresetName | None", preset),
            profile=cast(
                "PresetName | None",
                profile.value if isinstance(profile, RetrievalProfile) else profile,
            ),
            config=resolution_input,
        )
        resolved = plan.config
        assembled_retrieval = make_retrieval_preset(
            resolved.profile,
            retrieval_mode=resolved.retrieval.retrieval_mode,
            embedding=resolved.retrieval.embedding,
            vector_store=resolved.retrieval.vector_store,
            reranker=resolved.retrieval.reranker,
            postprocessor=resolved.retrieval.postprocessor,
        )
        if retrieval is not None:
            resolved = resolved.model_copy(
                update={"retrieval": resolved.retrieval.model_copy(update={
                    "retrieval_mode": retrieval.retrieval_mode,
                    "embedding": retrieval.embedding,
                    "vector_store": retrieval.vector_store,
                    "reranker": retrieval.reranker,
                    "postprocessor": retrieval.postprocessor,
                })}
            )
            plan = _replace_plan_config(plan, resolved, prefix="retrieval.")
            assembled_retrieval = retrieval
        if graph is not None:
            resolved = resolved.model_copy(
                update={"graph": resolved.graph.model_copy(update={"mode": normalize_graph_mode(graph)})}
            )
            plan = _replace_plan_config(plan, resolved, paths={"graph.mode"})
        selected_provider = provider
        if selected_provider is None:
            generation = resolved.generation
            if generation.provider_type == "anthropic":
                selected_provider = AnthropicProvider(
                    model=generation.model, base_url=generation.base_url
                )
            else:
                selected_provider = MockProvider(reference_date=generation.reference_date)
        return cls(
            workspace,
            provider=selected_provider,
            retrieval=assembled_retrieval,
            graph=resolved.graph.mode,
            _config=resolved,
            _effective_plan=plan,
        )

    @property
    def effective_plan(self) -> EffectivePlan:
        """Return the immutable configuration plan used to assemble this facade."""
        return self._effective_plan

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
        self._index_metadata.assert_compatible(self._effective_plan)
        if self.graph == "auto":
            from ragspine.graph.runtime import WorkspaceGraphRuntime, wants_global_graph

        if (
            self.graph == "auto"
            and wants_global_graph(question)
            and RuleIntentParser().parse(question).route == ROUTE_NARRATIVE
        ):
            graph_result = WorkspaceGraphRuntime(
                self.graph_db_path,
                self.provider,
                max_communities=self.graph_max_communities,
            ).answer_global(question)
            if graph_result is not None:
                return graph_result
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
                    reference_date=self.reference_date,
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

        # The fingerprint describes only the narrative chunk index.  Purely
        # structured ingestion is independent of that contract, while mixed
        # inputs must still fail before either channel can mutate its store.
        if narrative_paths:
            if dry_run:
                self._index_metadata.assert_compatible(self._effective_plan)
            else:
                self._index_metadata.claim(self._effective_plan)

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
                    list[str | Path](narrative_paths),
                    chunk_store,
                    dry_run=dry_run,
                    chunker=make_chunker(self.indexing.chunker),
                    max_chars=self.indexing.max_chars,
                    overlap_chars=self.indexing.overlap_chars,
                )
                if not dry_run and self.graph == "auto":
                    from ragspine.graph.runtime import WorkspaceGraphRuntime

                    WorkspaceGraphRuntime(self.graph_db_path, self.provider).index_chunks(
                        chunk_store.iter_chunks(),
                        doc_ids={path.name for path in narrative_paths},
                    )
            finally:
                chunk_store.close()
        return IngestResult(tuple(structured_reports), narrative_report)

    def _resolve_sources(self, source: str | Path) -> list[Path]:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"source does not exist: {path}")
        if self.allowed_upload_root is not None:
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(self.allowed_upload_root):
                raise ValueError(
                    f"source is outside allowed_upload_root: {self.allowed_upload_root}"
                )
        raw_candidates = (
            [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
        )
        candidates: list[Path] = []
        for candidate in raw_candidates:
            resolved_candidate = candidate.resolve(strict=True)
            if self.allowed_upload_root is not None and not resolved_candidate.is_relative_to(
                self.allowed_upload_root
            ):
                raise ValueError(
                    f"source is outside allowed_upload_root: {self.allowed_upload_root}"
                )
            candidates.append(resolved_candidate)
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
            self._index_metadata.init_schema()
            registry.init_schema()
            queue.init_schema()
        finally:
            queue.close()
            registry.close()
            store.close()


__all__ = ["IngestResult", "RAGSpine"]
