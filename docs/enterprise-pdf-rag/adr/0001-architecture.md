# ADR 0001 — Python 3.12 backend and the first figure slice

Status: Accepted for the approved initial implementation, 2026-09-19.

## Context

PRD v0.2 requires one PDF entrance, a structured SVG shared by two enrichment branches, description-only embedding and snapshot-bound ChartIR hydration. This milestone must run without paid model calls and must not claim production chart understanding.

## Decision

Use a uv-managed Python 3.12 src-layout package. The figures domain owns immutable standard-library values, protocols and deterministic eligibility rules. Pydantic validates process boundaries. SDK and FastAPI code lives in adapters; a central beartype hook checks internal calls. Construction requires an explicit execution mode. Offline demonstration producers carry offline-demo provenance and cannot be published as production.

The first parser adapter uses only released pdfspine. It converts its page text/drawing observations for an explicit page and bounding box into supported SVG primitives with element-to-source anchors. Unsupported raster, clipping, rotation or graphics capabilities yield pending diagnostics, never a false complete representation. A synthetic labelled bar chart is the first verified slice; the official AIA sample is a source-extraction diagnostic, not a chart-understanding benchmark.

Use in-memory repository and description index for the initial single-process CLI/API demonstration. This is not durable release publication, multi-user authentication or the later manifest/CAS service. Those requirements remain in PRD, not silently implemented by a dictionary. Two enrichers receive the same SVG. Only qualified description text crosses the embedding port. Query context is loaded from its matching bundle and snapshot, not reconstructed from the retrieved summary.

Source qualification is independently injected through a port. The authored-fixture adapter retains the full SVG/source binding and exact field-to-element occurrences established from known label positions. Producers cannot qualify themselves merely by returning VERIFIED. There is no public approval switch for arbitrary input.

LLM connectivity is a separate explicit `llm-smoke` command reading only process `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_MODEL`; missing fields fail. Embedding and rerank use their own required base/model settings for user-managed deployment, with no cloud defaults. A successful bounded connectivity probe does not enable the production figure service or certify model accuracy.

## Rejected alternatives

- Another PDF parser or PNG-in-SVG wrapping would violate the approved source and structure contract.
- Embedding ChartIR or serializing it as the description would destroy the independent same-SVG branch requirement.
- Default mock fallback would hide missing production configuration.
- A large workflow platform and speculative empty APIs do not help this tracer slice.

## Validation

Implement observable behaviors one at a time using red → green → refactor. The single read-only ./ci.sh gate uses the locked toolchain, Ruff, strict mypy, architecture/schema/drift checks and offline warnings-as-errors tests. No commit, push or deployment is authorized by this milestone.

Run focused offline tests during TDD, then the full gate after substantial changes and at phase completion. Live LLM tests remain an explicit separate command, reserved for major releases or material model-call-flow changes. Routine implementation, configuration and sanitization changes use transport substitutes, never automatic paid calls. The initial authorized connectivity probe is complete and must not be repeatedly invoked as a general regression test. Connectivity does not constitute the later chart golden-set quality acceptance.
