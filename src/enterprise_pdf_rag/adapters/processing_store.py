"""Content-addressed processing outcomes with a local atomic discovery pointer."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import (
    INLINE_ARTIFACT_LIMIT,
    LocalDocumentStore,
    split_stage_pointer,
)
from enterprise_pdf_rag.adapters.http.processing_schemas import (
    DocumentTreeRecord,
    ProcessingEnvelope,
    StageEnvelope,
)
from enterprise_pdf_rag.adapters.source_publication import validate_processing_source
from enterprise_pdf_rag.processing.index_text import (
    UNIT_INDEX_VERSION,
    IndexTextOptions,
    PageIndexContext,
)
from enterprise_pdf_rag.processing.retrieval import (
    RetrievalEmbedding,
    RetrievalIndex,
    RetrievalPlan,
    RetrievalUnitEmbeddings,
    retrieval_dependencies,
)
from ragspine.common.evidence.file_placement import (
    link_new_file,
    note_repair,
    read_stored,
    sharded_path,
)
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.metadata.document_metadata import summarize_document
from ragspine.extraction.evidence.metadata.document_tree import DocumentTree
from ragspine.extraction.evidence.metadata.page_metadata import PageMetadata
from ragspine.extraction.evidence.page.models import (
    ProcessingManifest,
    RetrievalPublication,
    StageOutcome,
    StageState,
)


class ProcessingStore:
    """Reads are validated; a validated retrieval parse is reused while its bytes are pinned.

    ``verify_every_request`` turns that reuse off, so every read re-parses and re-checks the
    whole published index from disk — the auditable behaviour, and what this store did
    unconditionally before. It also turns off the assets' verification cache, which
    ``verify_every_load`` controls on its own (see ``LocalDocumentStore``). A manifest object
    itself is re-read and re-hashed on every ``load``: it is the one file that names all the
    rest, and the mount's drift guard falls through to ``load`` to refuse a changed one.
    ``load``'s sweep of the assets it names may be skipped by a persisted receipt (ADR 0034;
    ``persisted_receipts`` / ``record_receipts``, see ``LocalDocumentStore``).
    """

    def __init__(
        self,
        root: Path,
        *,
        verify_every_request: bool = False,
        verify_every_load: bool | None = None,
        persisted_receipts: bool | None = None,
        record_receipts: bool = True,
    ) -> None:
        self.root = root
        self.assets = LocalDocumentStore(
            root,
            activate_on_publish=False,
            verify_every_load=True if verify_every_request else verify_every_load,
            persisted_receipts=persisted_receipts,
            record_receipts=record_receipts,
        )
        self._verify_every_request = verify_every_request
        self._verify_every_load = verify_every_load
        # A publication names its plan and index by content, so one parse stands for those
        # exact bytes for as long as this process lives; a republished snapshot names other
        # objects and misses. Re-reading 200-odd embedding artifacts per request cost more
        # than everything else an answer does.
        self._retrieval: dict[tuple[str, str], tuple[RetrievalPlan, RetrievalIndex]] = {}
        # Stage-cache pointers this instance found damaged, so each is counted once.
        self._damaged_seen: set[Path] = set()

    def auditing(self) -> "ProcessingStore":
        """The same store whose assets are re-verified on every call (no verification cache)."""
        return ProcessingStore(
            self.root, verify_every_request=self._verify_every_request, verify_every_load=True
        )

    def _cache_path(self, fingerprint: str) -> Path:
        """Where a stage-cache pointer is written: the sharded layout (ADR 0029)."""
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("Stage cache requires a SHA-256 input fingerprint")
        return sharded_path(self.root / "stage-cache", fingerprint)

    def _lookup(self, fingerprint: str) -> tuple[StageOutcome | None, bool]:
        """(the cached outcome, whether an entry exists but is damaged).

        A pointer's first line is the digest of its ``StageEnvelope``. Since ADR 0029
        Amendment 1 the envelope follows inline and must hash to that digest; a pointer that
        holds the digest alone (written before) names an envelope object of this store. Since
        Amendment 2 a small output may follow the envelope: it must be exactly the artifact the
        envelope names (length and digest); otherwise the output is an object of this store.

        Damaged = the pointer is unreadable or does not start with a digest, its inline
        envelope or output is not those bytes, or the envelope object or the output object it
        names is missing or not its digest (an asynchronous flush that failed after the write
        returned). Such an entry is a miss: the stage is recomputed (its model calls replay
        from the model cache) and ``cache`` / ``cache_output`` replaces the pointer (ADR 0029).
        """
        target = self._cache_path(fingerprint)
        try:
            _, data = read_stored(self.root / "stage-cache", fingerprint)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, self._damaged(target)
        try:
            payload, output = self._envelope(data)
        except (OSError, ValueError):
            return None, self._damaged(target)
        outcome = StageEnvelope.model_validate_json(payload).outcome
        if outcome.input_fingerprint != fingerprint or outcome.artifact is None:
            raise ValueError("Cached stage binding does not match its input")
        artifact = outcome.artifact
        if output is not None:
            if (len(output), hashlib.sha256(output).hexdigest()) != (
                artifact.byte_length,
                artifact.sha256,
            ):
                return None, self._damaged(target)
            self.assets.note_inline_verified(artifact.sha256, fingerprint, len(output))
            return outcome, False
        try:
            # The entry names an object: a copy inline elsewhere does not make it whole.
            self.assets.get_object(artifact)
        except (OSError, ValueError):
            return None, self._damaged(target)
        return outcome, False

    def _envelope(self, pointer: bytes) -> tuple[bytes, bytes | None]:
        """(the envelope bytes a pointer names, verified against its digest line; the output
        it carries inline, not yet verified)."""
        digest, envelope, output = split_stage_pointer(pointer)
        if envelope is None:
            return self.assets.read_object(digest), None
        if hashlib.sha256(envelope).hexdigest() != digest:
            raise ValueError("damaged pointer")
        return envelope, output

    def _damaged(self, target: Path) -> bool:
        if target not in self._damaged_seen:
            self._damaged_seen.add(target)
            note_repair("stage_cache")
        return True

    def cached(self, fingerprint: str) -> StageOutcome | None:
        """The verified cached outcome; None when absent or damaged (then recomputed)."""
        return self._lookup(fingerprint)[0]

    def cache(self, outcome: StageOutcome) -> None:
        if outcome.state is not StageState.SUCCEEDED or outcome.artifact is None:
            raise ValueError("Only successful stages can be reused as output cache")
        self.assets.verify(outcome.artifact)
        self._cache(outcome)

    def cache_output(
        self,
        stage: str,
        fingerprint: str,
        producer: str,
        payload: bytes,
        *,
        media_type: str = "application/json",
    ) -> StageOutcome:
        """Store a succeeded stage's output and cache it under ``fingerprint``; the outcome.

        An output of 1 to ``INLINE_ARTIFACT_LIMIT`` bytes is written inside the pointer, after
        the envelope, and never as an object (ADR 0029 Amendment 2); a larger one is put as an
        object, exactly as ``put`` + ``cache`` did. Same outcome, same digest either way.
        """
        if not 0 < len(payload) <= INLINE_ARTIFACT_LIMIT:
            ref = self.assets.put(payload, media_type=media_type)
            outcome = StageOutcome(stage, fingerprint, StageState.SUCCEEDED, producer, ref)
            self.cache(outcome)
            return outcome
        ref = AssetRef(hashlib.sha256(payload).hexdigest(), media_type, len(payload))
        outcome = StageOutcome(stage, fingerprint, StageState.SUCCEEDED, producer, ref)
        self._cache(outcome, output=payload)
        return outcome

    def _cache(self, outcome: StageOutcome, *, output: bytes = b"") -> None:
        existing, damaged = self._lookup(outcome.input_fingerprint)
        if existing is not None:
            if existing != outcome:
                raise ValueError("Stage fingerprint already names another actual output")
            return
        # The envelope is written inline, after its digest: one file per entry, not two
        # (ADR 0029 Amendment 1); a small output follows it (Amendment 2).
        envelope = StageEnvelope(outcome=outcome).model_dump_json().encode()
        target = self._cache_path(outcome.input_fingerprint)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A damaged entry names nothing usable, so it is replaced rather than conflicted with.
        self._write_pointer(
            target,
            hashlib.sha256(envelope).hexdigest(),
            immutable=not damaged,
            inline=envelope,
            artifact=output,
        )
        if output:
            assert outcome.artifact is not None
            self.assets.note_inline(outcome.artifact.sha256, outcome.input_fingerprint)

    def publish(self, manifest: ProcessingManifest, *, sources: LocalDocumentStore) -> str:
        digest = self.save_draft(manifest, sources=sources)
        self._write_pointer(self.root / "current-processing", digest)
        return digest

    def save_draft(self, manifest: ProcessingManifest, *, sources: LocalDocumentStore) -> str:
        """Validate an immutable release without changing active discovery state."""
        for ref in processing_assets(manifest):
            self.assets.verify(ref)
        ref = self.assets.put(
            ProcessingEnvelope(manifest=manifest).model_dump_json().encode(),
            media_type="application/json",
        )
        self.load(ref.sha256)
        plan = None if manifest.retrieval is None else self.load_retrieval(manifest.retrieval)[0]
        validate_processing_source(
            sources=sources, artifacts=self.assets, manifest=manifest, plan=plan
        )
        return ref.sha256

    def load(self, snapshot_id: str) -> ProcessingManifest:
        manifest = ProcessingEnvelope.model_validate_json(
            self.assets.read_content(snapshot_id)
        ).manifest
        self.assets.verify_snapshot(snapshot_id, processing_assets(manifest))
        pages = self.load_page_metadata(manifest)
        if manifest.document_metadata != summarize_document(tuple(pages.values())):
            raise ValueError("Document metadata differs from its page metadata stages")
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
                    raise ValueError("Retrieval member is absent from the processing manifest")
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
                    raise ValueError("Retrieval lineage is outside the processing object stages")
        return manifest

    def load_retrieval(
        self, publication: RetrievalPublication
    ) -> tuple[RetrievalPlan, RetrievalIndex]:
        if self._verify_every_request:
            return self._read_retrieval(publication)
        key = (publication.plan.sha256, publication.index.sha256)
        loaded = self._retrieval.get(key)
        if loaded is None:
            loaded = self._read_retrieval(publication)
            self._retrieval[key] = loaded
        return loaded

    def _read_retrieval(
        self, publication: RetrievalPublication
    ) -> tuple[RetrievalPlan, RetrievalIndex]:
        plan = TypeAdapter(RetrievalPlan).validate_json(self.assets.get(publication.plan))
        index = TypeAdapter(RetrievalIndex).validate_json(self.assets.get(publication.index))
        if plan.snapshot_id != publication.snapshot_id or index.snapshot_id != plan.snapshot_id:
            raise ValueError("Retrieval artifacts belong to different snapshots")
        if (
            index.index_version != plan.index_version
            or publication.dependencies != retrieval_dependencies(plan)
        ):
            raise ValueError("Retrieval publication has a different dependency closure")
        if plan.index_version.startswith(UNIT_INDEX_VERSION + ":"):
            for ref in publication.dependencies:
                self.assets.verify(ref, receipt=True)
            self._check_unit_index(plan, index)
            return plan, index
        if tuple(entry.member_id for entry in index.entries) != tuple(
            sorted(member.member_id for member in plan.members)
        ):
            raise ValueError("Retrieval index readiness does not cover the exact members")
        indexed = {entry.member_id: entry.vector for entry in index.entries}
        for ref in publication.dependencies:
            self.assets.verify(ref, receipt=True)
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
                raise ValueError("Index vector does not match its actual embedding artifact")
        return plan, index

    def _check_unit_index(self, plan: RetrievalPlan, index: RetrievalIndex) -> None:
        """A unit index repeats each member's artifact vectors in order, and nothing else.

        A member's embedding artifact is either one ``RetrievalEmbedding`` (one vector) or a
        ``RetrievalUnitEmbeddings`` (its row units, or none for an unscored running member,
        which only a snapshot indexed with ``drop_running_lines`` may hold).
        """
        options = IndexTextOptions.from_index_version(plan.index_version)
        ids = [entry.member_id for entry in index.entries]
        if ids != sorted(ids) or not set(ids) <= {member.member_id for member in plan.members}:
            raise ValueError("Retrieval index readiness does not cover the exact members")
        indexed: dict[str, list[tuple[float, ...]]] = {}
        for entry in index.entries:
            indexed.setdefault(entry.member_id, []).append(entry.vector)
        for member in plan.members:
            payload = self.assets.get(member.embedding)
            units: RetrievalUnitEmbeddings | RetrievalEmbedding = TypeAdapter(
                RetrievalUnitEmbeddings | RetrievalEmbedding
            ).validate_json(payload)
            vectors = (
                list(units.vectors)
                if isinstance(units, RetrievalUnitEmbeddings)
                else [units.vector]
            )
            if (
                units.description_sha256 != member.description.sha256
                or units.fingerprint != member.embedding_fingerprint
                or vectors != indexed.get(member.member_id, [])
                or any(len(vector) != member.embedding_dimensions for vector in vectors)
                or (not vectors and not options.drop_running_lines)
            ):
                raise ValueError("Index vector does not match its actual embedding artifact")

    def load_page_metadata(self, manifest: ProcessingManifest) -> dict[int, PageMetadata]:
        """Every succeeded page metadata stage, parsed and bound to its page; no I/O elsewhere."""
        pages: dict[int, PageMetadata] = {}
        for page in manifest.pages:
            stage = page.metadata
            if stage is None or stage.state is not StageState.SUCCEEDED or stage.artifact is None:
                continue
            metadata = TypeAdapter(PageMetadata).validate_json(
                self.assets.get(stage.artifact), strict=True
            )
            if (metadata.page_index, metadata.source_sha256) != (
                page.page_index,
                manifest.scope.source_sha256,
            ):
                raise ValueError("Page metadata is bound to another source page")
            pages[page.page_index] = metadata
        return pages

    def _document_tree_path(self, processing_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", processing_id) is None:
            raise ValueError("A document tree is recorded under a SHA-256 processing id")
        return self.root / "document-tree" / f"{processing_id}.json"

    def save_document_tree(self, processing_id: str, record: DocumentTreeRecord) -> None:
        """Record the routing tree (ADR 0019) of one processing id, replacing any earlier one."""
        if record.processing_id != processing_id:
            raise ValueError("Document tree record is bound to another processing id")
        if record.artifact is not None:
            self.assets.verify(record.artifact)
        target = self._document_tree_path(processing_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unlike a stage-cache pointer this record is mutable on purpose: a later run with a
        # real call budget replaces a deferred tree with a summarised one over the same
        # processing id. So it is replaced atomically rather than linked into place.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, mode="wb") as stream:
            temporary = Path(stream.name)
            stream.write(record.model_dump_json().encode())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def document_tree_record(self, processing_id: str) -> DocumentTreeRecord | None:
        """The saved document-tree state of one processing id, in any state; None if absent."""
        path = self._document_tree_path(processing_id)
        if not path.is_file():
            return None
        record = DocumentTreeRecord.model_validate_json(path.read_bytes())
        if record.processing_id != processing_id:
            raise ValueError("Document tree record is bound to another processing id")
        return record

    def load_document_tree(self, processing_id: str) -> DocumentTree | None:
        """The succeeded routing tree of one processing id; None when absent or unfinished."""
        record = self.document_tree_record(processing_id)
        if record is None or record.state is not StageState.SUCCEEDED or record.artifact is None:
            return None
        tree = TypeAdapter(DocumentTree).validate_json(
            self.assets.get(record.artifact), strict=True
        )
        scope = ProcessingEnvelope.model_validate_json(
            self.assets.read_content(processing_id)
        ).manifest.scope
        if tree.source_sha256 != scope.source_sha256:
            raise ValueError("Document tree is bound to another source document")
        return tree

    def index_contexts(self, manifest: ProcessingManifest) -> dict[int, PageIndexContext]:
        """The contextual index-text header of every page that has verified metadata."""
        display_title = (
            None
            if manifest.document_metadata is None
            or manifest.document_metadata.display_title is None
            else manifest.document_metadata.display_title.text
        )
        return {
            page_index: PageIndexContext(
                display_title,
                None if metadata.title is None else metadata.title.text,
                None if metadata.section is None else metadata.section.text,
            )
            for page_index, metadata in self.load_page_metadata(manifest).items()
        }

    def current_id(self) -> str | None:
        """The snapshot ``current-processing`` names; None when absent or unreadable (a pointer
        lost by an asynchronous flush, ADR 0029 — the next publish rewrites it)."""
        try:
            snapshot_id = (self.root / "current-processing").read_text().strip()
        except OSError:
            return None
        return snapshot_id if re.fullmatch(r"[0-9a-f]{64}", snapshot_id) else None

    def load_current(self) -> tuple[str, ProcessingManifest]:
        snapshot_id = (self.root / "current-processing").read_text().strip()
        return snapshot_id, self.load(snapshot_id)

    @staticmethod
    def _write_pointer(
        target: Path,
        digest: str,
        *,
        immutable: bool = False,
        inline: bytes = b"",
        artifact: bytes = b"",
    ) -> None:
        """Write ``digest`` (then ``inline``, if any) as one line each, then ``artifact``'s raw
        bytes (only after an ``inline`` envelope). An immutable pointer is first-writer-wins:
        an existing one is accepted only when its first line is ``digest``."""
        if artifact and not inline:
            raise ValueError("An inline stage output follows its envelope")
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(digest.encode() + b"\n" + (inline + b"\n" if inline else b"") + artifact)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if immutable:
                try:
                    link_new_file(temporary, target)
                except FileExistsError:
                    if target.read_bytes().partition(b"\n")[0].strip() != digest.encode():
                        raise ValueError("Conflicting immutable stage cache entry") from None
            else:
                os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def processing_assets(manifest: ProcessingManifest) -> tuple[AssetRef, ...]:
    outcomes = [stage for page in manifest.pages for stage in (page.canonical, page.partition)]
    outcomes.extend(page.raw_partition for page in manifest.pages if page.raw_partition is not None)
    outcomes.extend(page.metadata for page in manifest.pages if page.metadata is not None)
    outcomes.extend(
        stage for page in manifest.pages for item in page.objects for stage in item.stages
    )
    refs = tuple(outcome.artifact for outcome in outcomes if outcome.artifact is not None)
    if manifest.retrieval is not None:
        refs += (
            manifest.retrieval.plan,
            manifest.retrieval.index,
            *manifest.retrieval.dependencies,
        )
    return refs
