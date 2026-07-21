---
status: accepted
date: 2026-07-21
supersedes:
  - 0012-onboarding-complexity-budget.md#decision-rule-3
---

# ADR 0019 — fastai-style layered API: `RAGSpine` facade + unified ingest CLI

> Immutable record. ADRs are exempt from drift tracking. To reverse this
> decision, add a new ADR that supersedes it rather than editing this file.

## Context

ADR 0012 correctly protects the cost of reaching a first trustworthy answer:
the base install is offline, needs no key or configuration, and exposes a small
low-level API (`FactStore`, `Fact`, `MockProvider`, `answer_question`). That
surface is valuable for composition and testing, but it leaves the most common
product journey fragmented: a new user must discover extraction, ingestion,
storage, retrieval, provider, and lifecycle APIs before they can load their own
document and ask a question.

The README consequently has two different onboarding paths. The installed
`ragspine quickstart` is self-contained but uses an ephemeral synthetic fact;
the next end-to-end path depends on repository-only scripts and a generated
database. This preserves a small import surface while making the first
*user-owned-data* success substantially harder than the first demo success.

fastai demonstrates a useful library shape: a high-level path that chooses
coherent defaults for the common case, with progressively lower layers still
available for control. This is layering, not a second framework or DSL.

## Decision

Adopt a **two-layer public API**.

### High-level layer

Add a root-exported `RAGSpine` facade for the common lifecycle:

1. create/open a workspace or knowledge base;
2. ingest supported sources through one method;
3. ask a question and receive the existing answer/result type with provenance;
4. close owned resources, including context-manager support.

The facade composes existing extractors, ingestion functions, stores,
retrievers, security gates, and `answer_question`; it does not reimplement their
domain rules. Defaults must remain deterministic, offline, and base-install
compatible. A capability requiring an optional dependency must fail with the
existing actionable extra-install error rather than silently downgrade to a
different parser or model.

Add one installed, self-contained shell path:

```text
ragspine ingest <source> [--workspace <path>]
ragspine ask --workspace <path> <question>
```

`ragspine ingest` is the single canonical ingestion command. It routes by source
type into existing ingestion APIs, reports what was accepted/rejected, and
persists enough state for a later `ragspine ask` process. It must not call or
depend on repository-only `scripts/`. Existing advanced ingestion APIs and
service/job endpoints remain available beneath it.

The exact storage flag spelling may reuse an existing database option during
implementation, but documentation must present one canonical term consistently;
the CLI must not make users choose independently among fact, chunk, vector, and
graph stores for the basic path.

### Low-level layer

Preserve the existing four-name API and their contracts unchanged:

- `FactStore`
- `Fact`
- `MockProvider`
- `answer_question`

They remain the stable primitives for users who want explicit composition.
`answer_question` retains exactly three required positional arguments
(`question`, `store`, `provider`); new options remain keyword-only and defaulted.
No existing import or CLI command is removed.

### Local supersession of ADR 0012

This ADR **supersedes only rule 3's mechanical requirement that package-root
`__all__` contain exactly four names**. Root `__all__` may contain the four
existing primitives plus `RAGSpine`. The intent of rule 3 survives as a stricter
layering rule: the root exposes only the one high-level facade and the four
low-level primitives; backend factories, storage implementations, extractors,
and service types stay in their domain modules.

All other ADR 0012 rules remain in force, including the two-command/zero-config
quickstart ceiling, base-install sufficiency, the sacred `answer_question`
signature, honest missing-store errors, cold-reproducible docs, one canonical
shell ask path, and keeping opt-in tooling out of the core import graph.

## Non-negotiable invariants

The facade and CLI are new entrances to the same decision core, not bypasses.
They must preserve:

- **anti-fabrication:** absent structured evidence still yields an honest
  not-found response regardless of model output;
- **provenance:** ingestion and answers retain `source_doc_id` plus locator;
- **RESTRICTED isolation:** restricted content still passes through the existing
  retrieval/rerank exits and persistence policy;
- **privacy-aware traces:** no answer, fact value, prompt, or chunk text enters a
  trace payload;
- **deterministic SecurityGate:** it remains always-on and non-pluggable;
- **license hygiene and lean core:** optional SDK/model backends remain lazy and
  extra-gated; importing or using the base facade must not load them.

No convenience default may fabricate an empty knowledge base, silently replace
a requested capability, weaken source isolation, or discard lineage.

## Consequences

- The canonical installed-user journey becomes package-owned rather than
  repository-script-owned: install, ingest user data, then ask.
- New users learn one object and one ingestion command first; advanced users can
  descend into the same existing domain modules without a migration or escape
  hatch.
- The root public surface grows deliberately from four to five names. CI budget
  checks must assert this exact layered surface and continue checking that the
  root import does not load optional SDKs.
- Facade/CLI conformance tests must prove result equivalence with the low-level
  path, provenance preservation, deterministic reruns, missing-extra messages,
  resource ownership, and all security/privacy invariants.
- Documentation must separate the installed-user path from source-contributor
  scripts. `ragspine quickstart` remains the fastest invariant demonstration;
  `ragspine ingest` becomes the path to the first success on user-owned data.

## Alternatives considered

- **Keep only the four primitives and improve recipes.** Rejected: prose does not
  remove lifecycle and wiring decisions from the user's critical path.
- **Add only an ingest script.** Rejected: a repository script is not an installed
  product contract, and ingestion without a coherent object lifecycle leaves the
  Python path fragmented.
- **Hide or deprecate the low-level API.** Rejected: explicit composition and
  Protocol-driven substitution are core product strengths; the facade is an
  additional layer, not a replacement.
- **Make semantic embeddings, an LLM key, the service extra, or Redis part of the
  high-level default.** Rejected: this would violate the lean-core and onboarding
  budgets. These remain explicit opt-in capabilities.
