# ADR 0006 — Preserve non-chart visual inference as two pending branches

Status: Accepted for the physical-pages-1–20 processing milestone, 2026-09-19.

Image, Diagram and Formula objects use the same immutable source crop, structured SVG digest and deterministic render for two independent model calls. One call creates typed observations; the other writes a natural-language description directly from the source view. Neither branch receives the other branch's output. The native SVG crop remains the durable visual source; the PNG is a bounded model input aid and never replaces it.

Every result binds the pinned PDF digest, physical page, top-left bbox, native/crop/structured-SVG/render digests, renderer fingerprint and exact full or partially clipped source-span occurrences. Textless images and formulas remain processable because the visual source is still available even when there is no readable text observation. Empty text evidence never becomes fabricated labels.

Image object names are visible-content hypotheses while observed labels are rebuilt from exact cited source occurrences. Diagram nodes require source-bound labels when a label is supplied, and every inferred edge must reference returned nodes; node/edge relationships remain pending. Formula source literals are reconstructed only from ordered cited source occurrences. A normalized LaTeX form is explicitly `inferred` or `unavailable` and never overwrites the source literal.

Provider confidence is model-declared provenance. Decimal values in `[0,1]` are retained as uncalibrated scores; ordinal values such as `high` or `medium` remain verbatim in the method with no invented numeric mapping. Typed IR, descriptions, classification and relationships remain `PENDING` regardless of model agreement or confidence.

The two branches have independent failure records. A successful branch and its raw JSON remain available when the other branch fails; transport errors do not cause a retry loop or a mock fallback. These pending descriptions are not eligible for the retrieval index. Later qualification must independently bind the exact source, visual version, occurrences and asserted relationships before any claim becomes eligible.
