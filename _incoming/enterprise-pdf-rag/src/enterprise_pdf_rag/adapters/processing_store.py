"""Content-addressed processing outcomes with a local atomic discovery pointer."""

import os
import re
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.processing_schemas import (
    ProcessingEnvelope,
    StageEnvelope,
)
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.documents.models import AssetRef
from enterprise_pdf_rag.processing.models import (
    ProcessingManifest,
    RetrievalPublication,
    StageOutcome,
    StageState,
)
from enterprise_pdf_rag.processing.retrieval import (
    RetrievalEmbedding,
    RetrievalIndex,
    RetrievalPlan,
    retrieval_dependencies,
)


class ProcessingStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.assets = LocalDocumentStore(root, activate_on_publish=False)

    def _cache_path(self, fingerprint: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("Stage cache requires a SHA-256 input fingerprint")
        return self.root / "stage-cache" / fingerprint

    def cached(self, fingerprint: str) -> StageOutcome | None:
        path = self._cache_path(fingerprint)
        if not path.exists():
            return None
        outcome = StageEnvelope.model_validate_json(
            self.assets.read_content(path.read_text().strip())
        ).outcome
        if outcome.input_fingerprint != fingerprint or outcome.artifact is None:
            raise ValueError("Cached stage binding does not match its input")
        self.assets.get(outcome.artifact)
        return outcome

    def cache(self, outcome: StageOutcome) -> None:
        if outcome.state is not StageState.SUCCEEDED or outcome.artifact is None:
            raise ValueError("Only successful stages can be reused as output cache")
        self.assets.get(outcome.artifact)
        existing = self.cached(outcome.input_fingerprint)
        if existing is not None:
            if existing != outcome:
                raise ValueError(
                    "Stage fingerprint already names another actual output"
                )
            return
        ref = self.assets.put(
            StageEnvelope(outcome=outcome).model_dump_json().encode(),
            media_type="application/json",
        )
        target = self._cache_path(outcome.input_fingerprint)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_pointer(target, ref.sha256, immutable=True)

    def publish(
        self, manifest: ProcessingManifest, *, sources: LocalDocumentStore
    ) -> str:
        digest = self.save_draft(manifest, sources=sources)
        self._write_pointer(self.root / "current-processing", digest)
        return digest

    def save_draft(
        self, manifest: ProcessingManifest, *, sources: LocalDocumentStore
    ) -> str:
        """Validate an immutable release without changing active discovery state."""
        for ref in processing_assets(manifest):
            self.assets.get(ref)
        ref = self.assets.put(
            ProcessingEnvelope(manifest=manifest).model_dump_json().encode(),
            media_type="application/json",
        )
        self.load(ref.sha256)
        plan = (
            None
            if manifest.retrieval is None
            else self.load_retrieval(manifest.retrieval)[0]
        )
        validate_processing_source(
            sources=sources, artifacts=self.assets, manifest=manifest, plan=plan
        )
        return ref.sha256

    def load(self, snapshot_id: str) -> ProcessingManifest:
        manifest = ProcessingEnvelope.model_validate_json(
            self.assets.read_content(snapshot_id)
        ).manifest
        for ref in processing_assets(manifest):
            self.assets.get(ref)
        if manifest.retrieval is not None:
            plan, _ = self.load_retrieval(manifest.retrieval)
            if plan.scope != manifest.scope:
                raise ValueError("Retrieval plan belongs to another processing scope")
            records = {
                (page.page_index, item.object_id): item
                for page in manifest.pages
                for item in page.objects
            }
            for member in plan.members:
                record = records.get((member.page_index, member.object_id))
                if record is None or record.kind is not member.kind:
                    raise ValueError(
                        "Retrieval member is absent from the processing manifest"
                    )
                stages = {stage.stage: stage.artifact for stage in record.stages}
                if (
                    stages.get("qualified_ir", stages.get("ir")),
                    stages.get("qualified_description", stages.get("description")),
                    stages.get("qualification"),
                    stages.get("svg"),
                ) != (
                    member.ir,
                    member.description,
                    member.qualification,
                    member.source_svg,
                ):
                    raise ValueError(
                        "Retrieval member refers to artifacts outside the processing object"
                    )
                if not set(member.lineage_refs).issubset(set(stages.values())):
                    raise ValueError(
                        "Retrieval lineage is outside the processing object stages"
                    )
        return manifest

    def load_retrieval(
        self, publication: RetrievalPublication
    ) -> tuple[RetrievalPlan, RetrievalIndex]:
        plan = TypeAdapter(RetrievalPlan).validate_json(
            self.assets.get(publication.plan)
        )
        index = TypeAdapter(RetrievalIndex).validate_json(
            self.assets.get(publication.index)
        )
        if (
            plan.snapshot_id != publication.snapshot_id
            or index.snapshot_id != plan.snapshot_id
        ):
            raise ValueError("Retrieval artifacts belong to different snapshots")
        if (
            index.index_version != plan.index_version
            or publication.dependencies != retrieval_dependencies(plan)
        ):
            raise ValueError("Retrieval publication has a different dependency closure")
        if tuple(entry.member_id for entry in index.entries) != tuple(
            sorted(member.member_id for member in plan.members)
        ):
            raise ValueError(
                "Retrieval index readiness does not cover the exact members"
            )
        indexed = {entry.member_id: entry.vector for entry in index.entries}
        for ref in publication.dependencies:
            self.assets.get(ref)
        for member in plan.members:
            embedding = TypeAdapter(RetrievalEmbedding).validate_json(
                self.assets.get(member.embedding)
            )
            if (
                embedding.description_sha256,
                embedding.fingerprint,
                len(embedding.vector),
                embedding.vector,
            ) != (
                member.description.sha256,
                member.embedding_fingerprint,
                member.embedding_dimensions,
                indexed[member.member_id],
            ):
                raise ValueError(
                    "Index vector does not match its actual embedding artifact"
                )
        return plan, index

    def load_current(self) -> tuple[str, ProcessingManifest]:
        snapshot_id = (self.root / "current-processing").read_text().strip()
        return snapshot_id, self.load(snapshot_id)

    @staticmethod
    def _write_pointer(target: Path, digest: str, *, immutable: bool = False) -> None:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, delete=False, mode="w"
        ) as stream:
            temporary = Path(stream.name)
            stream.write(digest + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if immutable:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if target.read_text().strip() != digest:
                        raise ValueError(
                            "Conflicting immutable stage cache entry"
                        ) from None
            else:
                os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def processing_assets(manifest: ProcessingManifest) -> tuple[AssetRef, ...]:
    outcomes = [
        stage for page in manifest.pages for stage in (page.canonical, page.partition)
    ]
    outcomes.extend(
        page.raw_partition for page in manifest.pages if page.raw_partition is not None
    )
    outcomes.extend(
        stage
        for page in manifest.pages
        for item in page.objects
        for stage in item.stages
    )
    refs = tuple(
        outcome.artifact for outcome in outcomes if outcome.artifact is not None
    )
    if manifest.retrieval is not None:
        refs += (
            manifest.retrieval.plan,
            manifest.retrieval.index,
            *manifest.retrieval.dependencies,
        )
    return refs
