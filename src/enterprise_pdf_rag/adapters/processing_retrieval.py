"""Persist real description vectors and hydrate source-qualified typed artifacts."""

import json
from collections.abc import Mapping
from hashlib import sha256
from math import sqrt

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.bar_publication import parse_displayed_bar_receipt
from enterprise_pdf_rag.adapters.chart_member_validation import (
    uses_displayed_bar_policy,
    validate_retrieval_chart_member,
)
from enterprise_pdf_rag.adapters.chart_publication import parse_chart_receipt
from enterprise_pdf_rag.adapters.diagram_publication import validate_diagram_member
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.formula_qualification import (
    FormulaPublicationReceipt,
    validate_formula_member,
)
from enterprise_pdf_rag.adapters.literal_qualification import validate_literal_member
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.running_lines import read_running_spans
from enterprise_pdf_rag.adapters.shared_pdf import shared_pdfs
from enterprise_pdf_rag.processing.index_text import (
    ONE_UNIT_EACH,
    IndexTextOptions,
    PageIndexContext,
    contextual_index_text,
    inferred_table_row_units,
    member_index_text,
    table_row_units,
)
from enterprise_pdf_rag.processing.retrieval import (
    IndexEntry,
    PinnedRetrievalHit,
    RetrievalContext,
    RetrievalEmbedding,
    RetrievalIndex,
    RetrievalMember,
    RetrievalPlan,
    RetrievalUnitEmbeddings,
    resolve_member,
    retrieval_dependencies,
)
from ragspine.extraction.evidence.document.models import AssetRef, Bounds
from ragspine.extraction.evidence.figures.models import (
    ChartIR,
    FigureQualification,
    TextDescription,
)
from ragspine.extraction.evidence.figures.ports import BatchEmbeddingPort, EmbeddingPort
from ragspine.extraction.evidence.objects.diagrams.diagram_models import DiagramQualification
from ragspine.extraction.evidence.objects.formulas.formula_models import FormulaQualification
from ragspine.extraction.evidence.objects.tables.table_models import TableIR
from ragspine.extraction.evidence.objects.tables.table_rows import (
    TABLE_ROWS_PRODUCER,
    TableRowsIR,
)
from ragspine.extraction.evidence.objects.typed_ir import (
    DiagramIR,
    FormulaIR,
    GroupIR,
    ListIR,
    LiteralQualification,
    ObjectDescription,
    TextIR,
)
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingScope,
    RetrievalPublication,
    StageState,
)

# v2 admits Table members whose literal transcription qualified; v3 embeds the chart
# index-text projection (ADR 0012) instead of the description alone; v4 prepends the
# page's contextual header (``display_title | page_title | section``, ADR 0013); v5
# embeds the qualified-IR projection of Diagram members (proven nodes in reading order,
# ``A -> B`` per proven edge) and of Formula members (readable + linear + token texts),
# both admitted by ADR 0015. The string is part of the snapshot id, so older snapshots
# keep their ids and stay mountable.
_POLICY = "source-transcription-and-scoped-chart-qualification-v5"
# Uncached index texts handed to a batch embedder at a time; each slice is stored before the
# next is sent, so a failed run keeps what it already paid for.
_EMBED_SLICE = 256
# Snapshots whose vectors embed ``member_index_text``; older ones embedded the description
# text, and their lexical corpus must keep scoring exactly what they embedded. The
# displayed-bar admission builds its member through this class, so its v2 policy belongs
# here too (``chart_qa_bar_promotion.BAR_PUBLICATION_POLICY``).
PROJECTED_CHART_POLICIES = frozenset(
    {
        _POLICY,
        "source-transcription-and-scoped-chart-qualification-v4",
        "source-transcription-and-scoped-chart-qualification-v3",
        "source-transcription-donut-and-displayed-bar-v2",
    }
)
# Snapshots whose vectors embed the contextual header above the projection.
CONTEXTUAL_POLICIES = frozenset({_POLICY, "source-transcription-and-scoped-chart-qualification-v4"})
# Snapshots whose vectors embed the qualified-IR projection of a non-chart visual member
# (Diagram, Formula). A snapshot indexed before ADR 0015 holds no such member, so the
# gate is unreachable there; it keeps "one policy embeds exactly what it declared" checkable.
VISUAL_PROJECTION_POLICIES = frozenset({_POLICY})


def member_text(
    assets: LocalDocumentStore,
    plan: RetrievalPlan,
    member: RetrievalMember,
    context: PageIndexContext | None = None,
) -> str:
    """The text one pinned member was (or would be) embedded with; no evidence validation.

    ``context`` is the member's page header; it is applied only under a contextual policy,
    so a snapshot indexed before ADR 0013 keeps scoring exactly what it embedded.
    """
    payload = assets.get(member.description)
    if member.kind is not ObjectKind.CHART:
        body = TypeAdapter(ObjectDescription).validate_json(payload).text
        if (
            member.kind is ObjectKind.DIAGRAM
            and plan.qualification_policy in VISUAL_PROJECTION_POLICIES
        ):
            diagram = TypeAdapter(DiagramIR).validate_json(assets.get(member.ir))
            body = member_index_text(diagram, body)
        if (
            member.kind is ObjectKind.FORMULA
            and plan.qualification_policy in VISUAL_PROJECTION_POLICIES
        ):
            formula = TypeAdapter(FormulaIR).validate_json(assets.get(member.ir))
            body = member_index_text(formula, body)
    else:
        body = TypeAdapter(TextDescription).validate_json(payload).text
        if plan.qualification_policy in PROJECTED_CHART_POLICIES:
            chart = TypeAdapter(ChartIR).validate_json(assets.get(member.ir))
            body = member_index_text(chart, body)
    if plan.qualification_policy not in CONTEXTUAL_POLICIES:
        return body
    return contextual_index_text(body, context)


def member_units(
    assets: LocalDocumentStore,
    plan: RetrievalPlan,
    member: RetrievalMember,
    context: PageIndexContext | None,
    vectors: int,
) -> tuple[str, ...] | None:
    """The units a unit index scored this member as (``MemberText.units``); no validation.

    ``vectors`` is how many index vectors the snapshot holds for the member: none marks a
    running header / footer, and a row-units table must hold exactly one per unit. ``None``
    means the member scores its ``member_text`` as one unit, which every member of a
    snapshot indexed without the switches does.
    """
    options = IndexTextOptions.from_index_version(plan.index_version)
    if options.drop_running_lines and vectors == 0:
        return ()
    if not options.table_row_units or member.kind is not ObjectKind.TABLE:
        return None
    description = TypeAdapter(ObjectDescription).validate_json(assets.get(member.description))
    scoped = context if plan.qualification_policy in CONTEXTUAL_POLICIES else None
    if description.producer == TABLE_ROWS_PRODUCER:
        rows = TypeAdapter(TableRowsIR).validate_json(assets.get(member.ir))
        units = table_row_units(rows, scoped)
    else:
        # ADR 0031: a pending inferred grid is split the same way; a proved grid is not.
        units = inferred_table_row_units(
            TypeAdapter(TableIR).validate_json(assets.get(member.ir)), scoped
        )
    if len(units or ("",)) != vectors:
        raise ValueError("Row units differ from the vectors their snapshot indexed")
    return units


def member_anchor(assets: LocalDocumentStore, member: RetrievalMember) -> Bounds | None:
    """The member's page rectangle, for ordering a page's members as they are read.

    Every kind but ``CHART`` stores it on its description's source anchor; a chart's
    description is bound to its SVG instead, so its rectangle comes from the figure
    qualification receipt. ``None`` when the stored evidence carries neither.

    This is a best-effort read for reading order alone, so it never raises: a receipt this
    release cannot parse — an unsupported chart scope, a shape from a newer publisher —
    costs that member its place in the page order and nothing else. Geometry must not be
    able to fail a mount that the evidence itself supports. ``ValidationError`` is a
    ``ValueError`` in pydantic v2, so both arrive here.
    """
    try:
        if member.kind is not ObjectKind.CHART:
            payload = assets.get(member.description)
            return TypeAdapter(ObjectDescription).validate_json(payload).source.bbox
        receipt = assets.get(member.qualification)
        if uses_displayed_bar_policy(receipt):
            return parse_displayed_bar_receipt(receipt).qualification.source.bbox
        return parse_chart_receipt(receipt).qualification.source.bbox
    except (ValueError, KeyError, OSError):
        return None


def eligibility(record: ObjectProcessingRecord) -> tuple[bool, str | None]:
    """Kind and stage-completeness predicate shared by build and draft qualification.

    A Table is verified only when its literal transcription qualification succeeded;
    an observed grid whose description/qualification stayed unavailable is skipped.
    """
    if record.kind not in (
        ObjectKind.TEXT,
        ObjectKind.LIST,
        ObjectKind.GROUP,
        ObjectKind.TABLE,
        ObjectKind.CHART,
        ObjectKind.DIAGRAM,
        ObjectKind.FORMULA,
    ):
        return False, f"{record.kind.value} objects are not retrievable"
    stages = {stage.stage: stage for stage in record.stages}
    required = (
        ("qualified_ir", "qualified_description", "qualification", "svg")
        if record.kind in (ObjectKind.CHART, ObjectKind.DIAGRAM, ObjectKind.FORMULA)
        else ("ir", "description", "qualification", "svg")
    )
    if any(
        name not in stages or stages[name].state is not StageState.SUCCEEDED for name in required
    ):
        if record.kind is ObjectKind.TABLE:
            return (
                False,
                "Table transcription is not verified; only verified tables are retrievable",
            )
        if record.kind is ObjectKind.DIAGRAM:
            return (
                False,
                "Diagram structure is not proven; only geometry-qualified diagrams are retrievable",
            )
        if record.kind is ObjectKind.FORMULA:
            return (
                False,
                "Formula tokens are not source-proven; only proven formulas are retrievable",
            )
        return False, "required qualification stages are incomplete"
    return True, None


class ProcessingRetrieval:
    def __init__(
        self,
        sources: LocalDocumentStore,
        outputs: ProcessingStore,
        embedder: EmbeddingPort,
    ) -> None:
        self.sources = sources
        self.outputs = outputs
        self.embedder = embedder
        # What the last ``build`` sent: embedding requests and the texts they embedded.
        self.embedding_requests = 0
        self.embedded_objects = 0
        # What the last ``build`` laid out (``IndexTextOptions``): tables split into row
        # units, their units, and running headers / footers left unscored.
        self.row_unit_tables = 0
        self.row_units = 0
        self.unscored_running = 0

    @shared_pdfs()
    def build(
        self,
        scope: ProcessingScope,
        records: tuple[tuple[int, ObjectProcessingRecord], ...],
        contexts: Mapping[int, PageIndexContext] | None = None,
        options: IndexTextOptions = ONE_UNIT_EACH,
    ) -> RetrievalPublication:
        """Embed every eligible member; ``contexts`` gives each page's index-text header.

        Every member is validated first; the index texts not yet in the stage cache are then
        embedded in batches when the embedder can (``BatchEmbeddingPort``), each vector stored
        under the same per-object cache entry a single call writes, so the snapshot and the
        cache are byte-identical either way.

        ``options`` lays the index out into scoring units; off (the default) every member is
        one unit and one vector, byte for byte as before. ``table_row_units`` embeds a long
        verbatim-rows table as one vector per row unit (all its units under one cache entry);
        ``drop_running_lines`` embeds a Text member that prints only running header / footer
        lines not at all, so neither channel scores it - it stays a member, resolvable,
        quotable and in its page window. The snapshot's ``index_version`` names the switches.
        """
        self.embedding_requests = 0
        self.embedded_objects = 0
        self.row_unit_tables = 0
        self.row_units = 0
        self.unscored_running = 0
        running = read_running_spans(self.sources, scope) if options.drop_running_lines else None
        pending: list[
            tuple[
                int,
                ObjectProcessingRecord,
                tuple[AssetRef, AssetRef, AssetRef, AssetRef],
                tuple[AssetRef, ...],
                str,
                tuple[str, ...] | None,
            ]
        ] = []
        for page_index, record in records:
            eligible, _ = eligibility(record)
            if not eligible:
                continue
            stages = {stage.stage: stage for stage in record.stages}
            required = (
                ("qualified_ir", "qualified_description", "qualification", "svg")
                if record.kind in (ObjectKind.CHART, ObjectKind.DIAGRAM, ObjectKind.FORMULA)
                else ("ir", "description", "qualification", "svg")
            )
            refs = tuple(stages[name].artifact for name in required)
            if any(ref is None for ref in refs):
                raise ValueError("Eligible stages lack their actual artifacts")
            ir, description, qualification, svg = refs
            assert (
                ir is not None
                and description is not None
                and qualification is not None
                and svg is not None
            )
            lineage: tuple[AssetRef, ...] = ()
            if record.kind is ObjectKind.CHART:
                lineage_stages: tuple[str, ...] = ("ir", "description", "model_view")
                if uses_displayed_bar_policy(self.outputs.assets.get(qualification)):
                    lineage_stages = (
                        "ir",
                        "description",
                        "description_raw",
                        "model_view",
                        "normalized_description",
                        "normalization_receipt",
                        "source_paint_proof",
                        "page_context_proof",
                    )
                elif "source_paint_proof" in stages:
                    lineage_stages += ("source_paint_proof",)
                raw_refs = tuple(stages.get(name) for name in lineage_stages)
                if any(
                    stage is None
                    or stage.state is not StageState.SUCCEEDED
                    or stage.artifact is None
                    for stage in raw_refs
                ):
                    raise ValueError(
                        "Qualified chart is missing its raw branch, view or source proof lineage"
                    )
                lineage = tuple(
                    stage.artifact
                    for stage in raw_refs
                    if stage is not None and stage.artifact is not None
                )
            elif record.kind is ObjectKind.DIAGRAM:
                diagram_refs = tuple(
                    stages.get(name) for name in ("ir", "description", "model_view")
                )
                if any(
                    stage is None
                    or stage.state is not StageState.SUCCEEDED
                    or stage.artifact is None
                    for stage in diagram_refs
                ):
                    raise ValueError("Qualified diagram is missing its raw branch or model view")
                lineage = tuple(
                    stage.artifact
                    for stage in diagram_refs
                    if stage is not None and stage.artifact is not None
                )
            elif record.kind is ObjectKind.FORMULA:
                # The proof reads no model, so the model branches are lineage only when
                # they actually succeeded; the observation is always part of the closure.
                formula_receipt = TypeAdapter(FormulaPublicationReceipt).validate_json(
                    self.outputs.assets.get(qualification), strict=True
                )
                lineage = (
                    formula_receipt.qualification.observation,
                    *formula_receipt.qualification.lineage,
                )
                produced = {
                    stage.artifact
                    for stage in record.stages
                    if stage.state is StageState.SUCCEEDED and stage.artifact is not None
                }
                if not set(lineage) <= produced:
                    raise ValueError(
                        "Qualified formula lineage is outside its processing object stages"
                    )
            provisional = RetrievalMember(
                record.object_id,
                record.kind,
                page_index,
                ir,
                description,
                qualification,
                description,
                svg,
                self.embedder.fingerprint,
                1,
                lineage,
            )
            checked_ir, checked_description, _ = self._qualified(scope, provisional)
            context = None if contexts is None else contexts.get(page_index)
            text = contextual_index_text(
                member_index_text(checked_ir, checked_description.text), context
            )
            units: tuple[str, ...] | None = None
            if options.table_row_units and isinstance(checked_ir, TableRowsIR):
                units = table_row_units(checked_ir, context)
            elif options.table_row_units and isinstance(checked_ir, TableIR):
                units = inferred_table_row_units(checked_ir, context)
            if (
                running is not None
                and record.kind is ObjectKind.TEXT
                and isinstance(checked_description, ObjectDescription)
                and running.covers(page_index, checked_description.source_span_ids)
            ):
                units = ()
            pending.append(
                (page_index, record, (ir, description, qualification, svg), lineage, text, units)
            )
        if pending and all(item[5] == () for item in pending):
            # Nothing else would give the snapshot a vector dimension; score them after all.
            pending = [(*item[:5], None) for item in pending]
        self._embed_uncached(
            [
                (refs[1], (text,) if units is None else units, units is not None)
                for _, _, refs, _, text, units in pending
                if units != ()
            ]
        )
        vectors: dict[int, tuple[AssetRef, tuple[tuple[float, ...], ...]]] = {}
        for position, (_, _, refs, _, text, units) in enumerate(pending):
            if units is None:
                ref, embedding = self._embedding(refs[1], text)
                vectors[position] = (ref, (embedding.vector,))
            elif units:
                ref, unit_embeddings = self._unit_embeddings(refs[1], units)
                vectors[position] = (ref, unit_embeddings.vectors)
                self.row_unit_tables += 1
                self.row_units += len(units)
        dimensions = {len(vector) for _, scored in vectors.values() for vector in scored}
        if len(dimensions) > 1:
            raise ValueError("One retrieval snapshot cannot mix embedding dimensions")
        members: list[RetrievalMember] = []
        entries: list[IndexEntry] = []
        for position, (page_index, record, refs, lineage, _, _) in enumerate(pending):
            ir, description, qualification, svg = refs
            if position in vectors:
                embedding_ref, scored = vectors[position]
            else:
                embedding_ref, scored = self._unscored(description), ()
                self.unscored_running += 1
            member = RetrievalMember(
                record.object_id,
                record.kind,
                page_index,
                ir,
                description,
                qualification,
                embedding_ref,
                svg,
                self.embedder.fingerprint,
                len(scored[0]) if scored else next(iter(dimensions)),
                lineage,
            )
            members.append(member)
            entries.extend(IndexEntry(member.member_id, vector) for vector in scored)
        plan = RetrievalPlan(scope, tuple(members), _POLICY, options.index_version)
        index = RetrievalIndex(
            plan.snapshot_id,
            options.index_version,
            # Stable: a member's unit vectors keep their unit order.
            tuple(sorted(entries, key=lambda entry: entry.member_id)),
        )
        plan_ref = self.outputs.assets.put(
            TypeAdapter(RetrievalPlan).dump_json(plan), media_type="application/json"
        )
        index_ref = self.outputs.assets.put(
            TypeAdapter(RetrievalIndex).dump_json(index), media_type="application/json"
        )
        publication = RetrievalPublication(
            plan.snapshot_id, plan_ref, index_ref, retrieval_dependencies(plan)
        )
        self._load(publication)
        return publication

    def _cache_key(self, description: AssetRef, text: str) -> str:
        return sha256(
            repr(
                (
                    "index-text-embedding-v1",
                    description,
                    sha256(text.encode()).hexdigest(),
                    self.embedder.fingerprint,
                )
            ).encode()
        ).hexdigest()

    def _units_cache_key(self, description: AssetRef, units: tuple[str, ...]) -> str:
        """One cache entry for all of a member's units: two files per table, not per row."""
        return sha256(
            repr(
                (
                    "index-text-unit-embeddings-v1",
                    description,
                    sha256(json.dumps(units, ensure_ascii=False).encode()).hexdigest(),
                    self.embedder.fingerprint,
                )
            ).encode()
        ).hexdigest()

    def _embed_uncached(self, items: list[tuple[AssetRef, tuple[str, ...], bool]]) -> None:
        """Embed the not-yet-cached texts in batches, storing each under its own entry.

        ``items`` are ``(description, texts, units)``: one text under its single-vector entry,
        or a member's row units under one unit entry, stored once its last unit is back.
        Slices of ``_EMBED_SLICE`` texts are stored as they come back, so a failure keeps
        every earlier entry cached. An embedder without batches is left to ``_embedding`` /
        ``_unit_embeddings``.
        """
        embedder = self.embedder
        if not isinstance(embedder, BatchEmbeddingPort):
            return
        uncached: dict[str, tuple[AssetRef, tuple[str, ...], bool]] = {}
        for description, texts, units in items:
            key = (
                self._units_cache_key(description, texts)
                if units
                else self._cache_key(description, texts[0])
            )
            if key not in uncached and self.outputs.cached(key) is None:
                uncached[key] = (description, texts, units)
        jobs = list(uncached.items())
        work = [(job, text) for job, (_, (_, texts, _)) in enumerate(jobs) for text in texts]
        done: dict[int, list[tuple[float, ...]]] = {}
        # A slice smaller than the embedder's batch limit would waste the larger batches.
        step = max(_EMBED_SLICE, getattr(embedder, "batch_max_items", 0))
        for start in range(0, len(work), step):
            chunk = work[start : start + step]
            sent = embedder.request_count
            try:
                vectors = embedder.embed_descriptions([text for _, text in chunk])
            finally:
                self.embedding_requests += embedder.request_count - sent
            if len(vectors) != len(chunk):
                raise ValueError("Batch embedder returned a vector count unlike its inputs")
            # One backend transaction per slice (ADR 0036 §7.2): the entries a slice completed
            # commit together, so a crash loses at most one slice; earlier slices stay cached.
            with self.outputs.transaction():
                for (job, _), vector in zip(chunk, vectors, strict=True):
                    done.setdefault(job, []).append(vector)
                    key, (description, texts, units) = jobs[job]
                    if len(done[job]) < len(texts):
                        continue
                    if units:
                        self._store_units(key, description, tuple(done.pop(job)))
                    else:
                        self._store(key, description, done.pop(job)[0])
            self.embedded_objects += len(chunk)

    def _store(
        self, key: str, description: AssetRef, vector: tuple[float, ...]
    ) -> tuple[AssetRef, RetrievalEmbedding]:
        embedding = RetrievalEmbedding(description.sha256, self.embedder.fingerprint, vector)
        outcome = self.outputs.cache_output(
            "embedding",
            key,
            self.embedder.fingerprint,
            TypeAdapter(RetrievalEmbedding).dump_json(embedding),
        )
        assert outcome.artifact is not None
        return outcome.artifact, embedding

    def _store_units(
        self, key: str, description: AssetRef, vectors: tuple[tuple[float, ...], ...]
    ) -> tuple[AssetRef, RetrievalUnitEmbeddings]:
        embeddings = RetrievalUnitEmbeddings(description.sha256, self.embedder.fingerprint, vectors)
        outcome = self.outputs.cache_output(
            "embedding",
            key,
            self.embedder.fingerprint,
            TypeAdapter(RetrievalUnitEmbeddings).dump_json(embeddings),
        )
        assert outcome.artifact is not None
        return outcome.artifact, embeddings

    def _unscored(self, description: AssetRef) -> AssetRef:
        """A running member's embedding artifact: no vector at all, so no channel scores it."""
        return self.outputs.assets.put(
            TypeAdapter(RetrievalUnitEmbeddings).dump_json(
                RetrievalUnitEmbeddings(description.sha256, self.embedder.fingerprint, ())
            ),
            media_type="application/json",
        )

    def _embedding(self, description: AssetRef, text: str) -> tuple[AssetRef, RetrievalEmbedding]:
        fingerprint = self._cache_key(description, text)
        cached = self.outputs.cached(fingerprint)
        if cached is not None:
            assert cached.artifact is not None
            embedding = TypeAdapter(RetrievalEmbedding).validate_json(
                self.outputs.assets.get(cached.artifact)
            )
            if (
                embedding.description_sha256 != description.sha256
                or embedding.fingerprint != self.embedder.fingerprint
            ):
                raise ValueError("Cached embedding belongs to a different description or model")
            return cached.artifact, embedding
        self.embedding_requests += 1
        vector = self.embedder.embed_description(text)
        self.embedded_objects += 1
        return self._store(fingerprint, description, vector)

    def _unit_embeddings(
        self, description: AssetRef, units: tuple[str, ...]
    ) -> tuple[AssetRef, RetrievalUnitEmbeddings]:
        key = self._units_cache_key(description, units)
        cached = self.outputs.cached(key)
        if cached is not None:
            assert cached.artifact is not None
            embeddings = TypeAdapter(RetrievalUnitEmbeddings).validate_json(
                self.outputs.assets.get(cached.artifact)
            )
            if (
                embeddings.description_sha256 != description.sha256
                or embeddings.fingerprint != self.embedder.fingerprint
                or len(embeddings.vectors) != len(units)
            ):
                raise ValueError("Cached unit embeddings belong to other units or another model")
            return cached.artifact, embeddings
        vectors: list[tuple[float, ...]] = []
        for unit in units:
            self.embedding_requests += 1
            vectors.append(self.embedder.embed_description(unit))
            self.embedded_objects += 1
        return self._store_units(key, description, tuple(vectors))

    def _load(self, publication: RetrievalPublication) -> tuple[RetrievalPlan, RetrievalIndex]:
        return self.outputs.load_retrieval(publication)

    def search(
        self, publication: RetrievalPublication, query: str, *, limit: int = 5
    ) -> tuple[PinnedRetrievalHit, ...]:
        plan, index = self._load(publication)
        # Results are bounded by the corpus; a metadata pre-filter reads the whole corpus
        # (ADR 0013) and the HTTP search contract bounds its own limit separately.
        if not query.strip() or limit < 1:
            raise ValueError("A nonempty query and a positive limit are required")
        if not plan.members:
            return ()
        if any(
            member.embedding_fingerprint != self.embedder.fingerprint for member in plan.members
        ):
            raise ValueError("Query embedding provider differs from the pinned index")
        query_vector = self.embedder.embed_query(query)
        RetrievalEmbedding("query", self.embedder.fingerprint, query_vector)
        best: dict[str, float] = {}
        # The snapshot id is the content address of the whole plan, so asking the plan for
        # it once per member re-derived it over every member again; it is the same string
        # for every hit in this ranking.
        snapshot_id = plan.snapshot_id
        for entry in index.entries:
            if len(query_vector) != len(entry.vector):
                raise ValueError("Query embedding dimensions differ from the pinned index")
            divisor = sqrt(
                sum(value * value for value in query_vector)
                * sum(value * value for value in entry.vector)
            )
            score = (
                sum(left * right for left, right in zip(query_vector, entry.vector, strict=True))
                / divisor
                if divisor
                else 0.0
            )
            # A unit index may hold several vectors per member: it scores as its best one.
            if entry.member_id not in best or score > best[entry.member_id]:
                best[entry.member_id] = score
        hits = [
            PinnedRetrievalHit(snapshot_id, member_id, score) for member_id, score in best.items()
        ]
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.member_id))[:limit])

    def resolve(
        self, publication: RetrievalPublication, hit: PinnedRetrievalHit
    ) -> RetrievalContext:
        return resolve_processing_context(self.sources, self.outputs, publication, hit)

    def _literal(
        self, scope: ProcessingScope, member: RetrievalMember
    ) -> tuple[
        TextIR | ListIR | GroupIR | TableIR | TableRowsIR,
        ObjectDescription,
        LiteralQualification,
    ]:
        return validate_literal_member(self.sources, self.outputs.assets, scope, member)

    def _qualified(
        self, scope: ProcessingScope, member: RetrievalMember
    ) -> tuple[
        TextIR | ListIR | GroupIR | TableIR | TableRowsIR | ChartIR | DiagramIR | FormulaIR,
        ObjectDescription | TextDescription,
        LiteralQualification | FigureQualification | DiagramQualification | FormulaQualification,
    ]:
        if member.kind is ObjectKind.CHART:
            return validate_retrieval_chart_member(self.sources, self.outputs.assets, scope, member)
        if member.kind is ObjectKind.DIAGRAM:
            return validate_diagram_member(self.sources, self.outputs.assets, scope, member)
        if member.kind is ObjectKind.FORMULA:
            return validate_formula_member(self.sources, self.outputs.assets, scope, member)
        return self._literal(scope, member)


def resolve_processing_context(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    publication: RetrievalPublication,
    hit: PinnedRetrievalHit,
) -> RetrievalContext:
    """Hydration verifies stored evidence and never requires an embedding/model call."""
    plan, _ = outputs.load_retrieval(publication)
    member = resolve_member(plan, hit)
    if member.kind is ObjectKind.CHART:
        chart, chart_description, chart_receipt = validate_retrieval_chart_member(
            sources, outputs.assets, plan.scope, member
        )
        return RetrievalContext(plan.snapshot_id, member, chart, chart_description, chart_receipt)
    if member.kind is ObjectKind.DIAGRAM:
        diagram, diagram_description, diagram_receipt = validate_diagram_member(
            sources, outputs.assets, plan.scope, member
        )
        return RetrievalContext(
            plan.snapshot_id, member, diagram, diagram_description, diagram_receipt
        )
    if member.kind is ObjectKind.FORMULA:
        formula, formula_description, formula_receipt = validate_formula_member(
            sources, outputs.assets, plan.scope, member
        )
        return RetrievalContext(
            plan.snapshot_id, member, formula, formula_description, formula_receipt
        )
    ir, description, receipt = validate_literal_member(sources, outputs.assets, plan.scope, member)
    return RetrievalContext(plan.snapshot_id, member, ir, description, receipt)
