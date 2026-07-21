---
status: accepted
date: 2026-07-21
---

# ADR 0020 — Unified configuration resolution and effective pipeline plan

> Immutable record. ADRs are exempt from drift tracking. To reverse this
> decision, add a new ADR that supersedes it rather than editing this file.

## Context

ADR 0019 established the fastai-style `RAGSpine` facade: one high-level object
for the common ingest-and-ask lifecycle, with composable domain APIs underneath.
The facade currently resolves retrieval presets itself, translates part of that
selection into `ServiceConfig`, and handles GraphRAG separately. Other shipped
capabilities—including chunker selection, query transformation, corrective and
adaptive retrieval, RAPTOR, multi-index routing, and visual retrieval—still
have independent factories or service-level switches.

Adding each capability directly to `RAGSpine.local`, the CLI, and the service
would create several assembly implementations with subtly different defaults.
It would also make ingestion-time decisions such as chunking invisible to the
later query process. A workspace could then be queried with a pipeline that is
incompatible with the index already on disk, producing plausible but
irreproducible results.

The desired user experience is fastai-like: choose one coherent preset, ingest,
and ask. Advanced users must still be able to override a strict nested config or
inject a component without replacing the facade. This convenience cannot weaken
the offline default, optional-dependency isolation, or RAGSpine's safety
invariants.

## Decision

### One resolver and one effective plan

Introduce one side-effect-free configuration resolver as the sole source of
assembly truth:

```text
resolve_config(preset, config, legacy arguments, injected components)
    -> EffectivePlan
```

`EffectivePlan` is an immutable, serializable description of the pipeline that
will actually run. It contains resolved ingestion, retrieval, graph, generation,
storage, and security selections; the source of every resolved value; required
optional extras; and an index fingerprint. It contains no provider secret,
question, prompt, document text, chunk text, answer, or fact value.

The Python facade, installed CLI, HTTP service, and worker must consume this same
plan or an adapter derived mechanically from it. They may own different resource
lifecycles, but they may not independently reinterpret presets, environment
variables, or defaults. `session.py` must not hand-copy a subset of facade
settings into a separately defaulted `ServiceConfig`.

Resolution validates the complete plan before opening stores, loading models,
or mutating a workspace. An invalid combination or a missing dependency fails
once, early, and actionably. Resolution itself performs no network request and
does not select an algorithm merely because an optional package happens to be
installed.

### Presets are fixed, versioned recipes

The high-level golden path remains:

```python
with RAGSpine.local(".ragspine", preset="balanced") as rag:
    rag.ingest("./documents")
    result = rag.ask("What changed and why?")
```

`economy`, `balanced`, and `quality` are fixed, validated whole-pipeline recipes,
not aliases for one backend field. Their resolved meanings are versioned product
contracts and must be covered by snapshot or explicit-value tests.

- **economy** is the base-install, zero-network recipe and preserves the current
  default behavior unless a later ADR explicitly changes it;
- **balanced** is a coherent offline recipe that may enable additional
  deterministic, base-compatible stages only when their behavior is explicitly
  fixed by the preset contract;
- **quality** is a fixed higher-quality recipe whose required extras are declared
  and checked before execution.

Model-bearing query transformations, LLM RAPTOR summarization, and GPU visual
retrieval are not silently implied by an installed dependency. If a preset
includes such a capability, that dependency and runtime requirement is part of
the preset's explicit contract; otherwise it is enabled through advanced config.
There is no "best available" dependency probing because it would make the same
configuration resolve differently across machines.

The existing `profile` argument remains a compatibility alias during migration.
The documented API uses `preset`. Passing both is a configuration conflict rather
than an implicit winner. The default continues to resolve to the current economy
behavior, preserving ADR 0012's onboarding and byte-identity budgets.

### Strict advanced configuration and component escape hatch

`RAGSpineConfig` remains the serializable Pydantic v2 boundary: strict, frozen,
and `extra="forbid"`. It grows by pipeline domain rather than by adding many
keyword arguments to `RAGSpine.local`. Cross-field validation rejects incoherent
plans, such as an economy retrieval mode with embeddings enabled.

Backend-specific tuning that does not belong in the common semantic config—for
example a vector database's native index parameters—stays behind typed component
injection or the existing low-level factories. Component injection is the
advanced escape hatch and has the highest precedence, but it cannot replace or
disable the deterministic security gate, provenance handling, persistence
isolation policy, or anti-fabrication control flow.

### Compatibility precedence

For values that can be expressed through more than one migration surface, the
resolver applies this precedence, from highest to lowest:

1. explicitly injected component objects;
2. explicit legacy facade arguments (`retrieval`, `graph`, and `profile` while
   supported);
3. explicit fields in `RAGSpineConfig` or its validated mapping representation;
4. the selected preset;
5. library defaults.

Precedence applies across different layers. Conflicting values supplied at the
same layer fail with a configuration error; they are never resolved by argument
order. Explicit requests are never silently downgraded. Existing valid calls to
`RAGSpine.local(profile=...)`, `RAGSpine.local(retrieval=...)`, and
`RAGSpine.local(graph=...)` retain their behavior during the compatibility
window. The low-level `answer_question` signature and the five-name curated root
API from ADR 0019 remain unchanged.

### Index fingerprint and workspace compatibility

The resolver derives a deterministic index fingerprint from every setting that
changes persisted retrieval meaning, including at minimum chunking, contextual
index text, embedding representation, RAPTOR tree construction, visual indexing,
and their schema or algorithm versions. Runtime-only settings that do not alter
the persisted representation do not enter the fingerprint.

Successful ingestion records the fingerprint and sufficient version metadata in
workspace-owned metadata. Before querying or incrementally ingesting, the facade
compares the stored fingerprint with the effective plan. A mismatch fails with
an actionable reindex-required error that identifies the incompatible categories
and gives the canonical reindex command. It must not reuse the old index, mutate
it partially, fabricate an empty replacement, or silently choose the old plan.

Fingerprinting is a compatibility guard, not a cache of secrets: absolute source
paths, credentials, document content, prompts, and user questions are excluded.

### Diagnostics and discoverability

The effective plan is the shared basis for Python and CLI diagnostics. A Python
inspection method and `ragspine config show --effective` must present the same
machine-readable selections and value origins. `ragspine doctor` checks declared
extras, provider prerequisites, device requirements, workspace metadata, and
fingerprint compatibility without sending a model or embedding request.

Controlled errors must distinguish configuration conflict, missing optional
extra, unavailable capability, and required reindexing. Each error states the
requested capability, the unmet requirement, and one copyable remediation command
where one exists. Diagnostics remain privacy-safe and do not echo secrets or
knowledge-base content.

## Non-negotiable invariants

Every plan and every entry point assembled from it preserves:

- **anti-fabrication:** absent structured evidence yields an honest not-found
  result regardless of provider output;
- **provenance:** ingestion and answers retain source document and locator, plus
  library origin when multi-index retrieval is active;
- **RESTRICTED isolation:** restricted chunks cannot leave the established
  retrieval/rerank exits or bypass at-rest persistence policy;
- **privacy-aware observability:** effective-plan diagnostics and traces never
  contain answer, fact value, prompt, document, or chunk text;
- **deterministic security:** `SecurityGate` remains always-on and non-pluggable;
- **lean core and license hygiene:** optional SDKs and model backends remain lazy,
  extra-gated, and absent from `import ragspine` and the economy first-answer path;
- **offline reproducibility:** default resolution and execution require no key,
  model download, network request, Redis, or service extra.

The resolver may select among safe implementations but may not expose a switch
that disables these guarantees. All new presets and migrated capabilities require
parametrized conformance tests for these invariants.

## Consequences

- Adding a RAG capability becomes a two-sided contract: it must participate in
  the effective plan and be wired into the correct ingest or ask phase. Adding a
  config field without real facade execution does not count as high-level support.
- Ingestion and querying share one reproducible assembly description, preventing
  configuration drift between Python, CLI, service, and worker processes.
- Preset changes become observable compatibility changes. A change affecting the
  persisted representation requires an index-version/fingerprint change and
  migration or reindex guidance.
- The facade remains small while advanced configuration stays discoverable and
  type-safe. Backend-native knobs do not leak into the common API.
- CI must prove preset resolution, precedence, same-layer conflict handling,
  serialization without secrets, missing-extra diagnostics, fingerprint stability
  and mismatch refusal, facade/service assembly equivalence, base-only import,
  zero-network economy behavior, and the standing safety invariants.
- `ServiceConfig` may remain as a service transport/runtime structure, but it is
  populated from `EffectivePlan`; it is no longer an independent source of RAG
  assembly defaults.

## Alternatives considered

- **Add every capability as a keyword to `RAGSpine.local`.** Rejected: it turns
  the golden path into a matrix of implementation details and allows invalid
  cross-layer combinations.
- **Keep separate facade, CLI, and service assembly.** Rejected: duplicated
  defaults already leave shipped capabilities unreachable from the facade and
  will inevitably drift.
- **Make `auto` choose whichever optional dependencies are installed.** Rejected:
  identical user code would produce different indexes and answers across
  machines, and an explicit capability could silently disappear.
- **Store only the selected preset name in the workspace.** Rejected: presets can
  evolve and advanced overrides can change persisted meaning; compatibility must
  compare the fully resolved, versioned index contract.
- **Treat fingerprint mismatch as a warning.** Rejected: querying an incompatible
  index yields misleading quality failures that look like valid not-found
  answers. Honest refusal and an actionable reindex instruction are required.
- **Replace the low-level APIs with the effective plan.** Rejected: the plan is
  the high-level assembly contract, while explicit Protocol-driven composition
  remains a stable product strength.
