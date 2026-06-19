---
status: accepted
date: 2026-06-19
---

# ADR 0012 — Onboarding complexity budget: a standing veto on first-answer cost

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Relates to [0009](0009-dependency-and-framework-policy.md), [0005](0005-lean-core-experimental-isolation.md), [0004](0004-domain-profile-generalization.md); gated by [0003](0003-audience-oss-library.md). Does not supersede any prior ADR — it fills the un-decided onboarding-cost gap. (If a future change alters the default install or the first-run command, that change supersedes part of [0009] and must be declared per supersede-don't-edit; this ADR does not make that change.)

## Context

The audience is locked by [0003]: developers who **cold `pip install` and evaluate** — not portfolio reviewers, not internal-ops. For that reader the binding cost is *how much they pay to reach a first correct answer with provenance*. No prior ADR governs that cost. [0009] decided the lean default install (install weight is an adoption barrier); [0005] decided what ships in core vs extras; [0004] bet on full generality via `DomainProfile`. Generality is in standing tension with onboarding cost: a config-first general library asks the newcomer to author a profile before any value.

An audit of the shipped code measured the honest shortest path and found it is already excellent **and** already at risk of creep:

- `answer_question(question, store, provider)` has exactly **3 required positional args** plus 3 keyword-only optionals that all default to no-ops (`agent/agent.py`, verified by `inspect.signature`). The call surface honors "plain Python."
- A correct first answer with provenance is reachable in **2 commands, 0 config, 0 keys, 0 network**: `pip install rag-spine && ragspine quickstart` (verified — builds an ephemeral KB, runs `MockProvider`, prints one FOUND-with-lineage and one honest not-found, exit 0).
- The minimal-viable API is **4 names**: `FactStore`, `Fact`, `MockProvider`, `answer_question`. Everything else the engine does (dual-channel, glossary, sensitivity, review queue, FAQ, intent slots, clarification, security gate, composite routing) runs *transparently* for a well-formed structured question and is never a getting-started concept.

The risk is that the "surpass LangGraph/Dify" roadmap creeps this baseline upward feature by feature. This ADR codifies the baseline as a ceiling and stands as a veto.

The founder's binding principle: **win by subtraction, not by over-abstraction.** A first answer must cost almost nothing to reach; new capability earns its way in behind a default or an opt-in tier, never onto the first-answer path.

## Decision

Adopt an **onboarding complexity budget**: a measured ceiling on the cost to a first correct answer, enforced by CI, applied only to the *flexibility / config surface* and never to the guarantees.

**Definition — "on the first-answer path."** A thing is *on the path* only if a newcomer must **understand it (a name/concept) or perform it (a command/config/LOC)** to get a first correct answer with provenance. Code that merely *executes* transparently (security gate, clarification, composite routing, dual-channel) is **off the path**. This single predicate is what every rule below is scoped to.

**Budget rules (the ceilings).**

1. **Time-to-first-answer ceiling.** The canonical zero-config quickstart reaches a correct answer with provenance in **≤2 commands, 0 config files, 0 API keys, 0 network** (`pip install rag-spine && ragspine quickstart`). No roadmap item may raise this ceiling. Opt-in alternate paths that use a key/network/extra are *not* violations of this rule — it governs only the canonical quickstart.
2. **Sacred signature.** `answer_question` keeps exactly **3 required positional params** (`question, store, provider`); every other param is keyword-only with a non-empty default. Forbidden: a 4th required positional, de-defaulting an optional, or a **second orchestration entry a newcomer must choose between for the basic first answer**. New capability arrives as a defaulted keyword-only optional or in the opt-in tier. (Additional *optional* advanced entries — e.g. a future streaming/batch API with its own defaults — are allowed; what is banned is a competing *mandatory* entry on the first-answer path.)
3. **First-answer concept count.** The path adds no name beyond the minimal-viable **4**: `FactStore`, `Fact`, `MockProvider`, `answer_question`. Enforced as a surface proxy: the curated package-root `__all__` is exactly those 4, and the quickstart code references no additional public `ragspine.*` symbols.
4. **Base-install sufficiency.** The first answer works on the **base package alone**. No extra (`[dev]`, `[service]`, `[llm]`, `[embed]`, `[pdf]`, `[ocr]`) may become required for `quickstart` or for `ragspine ask` on an existing db. A separate base-only CI venv asserts this — the normal `[dev,service]` suite would mask an accidental hard dependency.
5. **No silent fabrication of preconditions.** Any shell entry that opens a fact store **guards a missing/empty db with a helpful non-zero error**, never silently inits an empty db and returns a false "not found." (`ragspine ask` complies; the audit's standing breach is `scripts/ask.py`, which this rule targets to fix, not codify.)
6. **Docs reproduce cold.** Every headline example reproduces verbatim on a fresh clone in the documented order, **or** states its precondition/required extra. A FOUND example depending on the gitignored `data/fact_metric.db` without a stated "run the demo first" note is a violation.
7. **One canonical ask.** Exactly one documented canonical shell ask — the guarded `ragspine ask`. The README Quickstart must not steer newcomers to the unguarded `scripts/ask.py`. The richer script may keep existing as a documented power-user tool.
8. **Guarantees are budget-exempt.** No budget-reduction change may weaken **anti-fabrication, provenance, RESTRICTED two-exit isolation, privacy-aware traces, the deterministic never-pluggable security gate, or dependency-license hygiene** (the hard core of [0002]). Cutting an invariant to save a step is auto-rejected. The teeth here are the existing frozen invariant/regression tests.
9. **Generality off the critical path.** `DomainProfile` authoring, locale packs, and custom dimensions are reachable in **one documented step** but never *required* before the first answer; the bundled ACME example carries the zero-config first run ([0004] "prove generality by demonstration"). The audit's silent-unreachable-data trap (an unknown entity silently falling back to the home entity and returning a false not-found) is named as a **breach to fix** — loud warning or verbatim-entity match — not behavior to codify.
10. **Opt-in tier stays out of core import.** Attestation, the linter/drift checks, registry authoring tooling, **trace sinks/exporters/analysis tooling** (note: privacy-aware trace *emission* is core and exempt under rule 8 — only the sinks are opt-in), and the **eval harness** (not the in-core regression invariants) must not be loaded on the `import ragspine` / first-answer path. They live behind explicit opt-in imports or extras. The PEP 562 lazy-submodule package root already guarantees this; a CI import-graph check locks it as a regression gate.

**Relief valve (so the budget can never be turned against its own invariants).** A change that *strengthens a guarantee* (rule 8) may raise a budget ceiling, but only by the **minimum the guarantee requires**, and it must be declared. Rules 1–4 are otherwise absolute; this is the one sanctioned exception, governed by rule 8, not by the veto.

**Opt-in tier (what stays out of the core path).** Attestation; linter/drift checks (`[dev]`); registry authoring (packaged prompts ship per [0008], but management is opt-in); trace sinks/exporters; the eval harness + golden sets + baseline gate (`[dev]`/CI); `AnthropicProvider` (`[llm]` + key), embeddings (`[embed]`), narrative/vector channel, PDF/OCR (`[pdf]`/`[ocr]`), service layer (`[service]`); `DomainProfile` authoring, locale packs, custom dimensions, and the pluggable LLM intent path. None may become required for a first answer.

**This is a standing veto, not a one-time review.** Because this is a solo, no-branch, no-PR repo, the enforcement surface is `scripts/ci.sh` + the `.githooks/pre-push` hook — not a PR ritual. A change that breaches a hard rule is rejected by the gate (push blocked) or pushed to the opt-in tier; there is no temporary exemption (except the rule-8 relief valve above). Every future feature is checked against these rules before it can land.

## Alternatives considered (rejected)

- **Budget the invariants/guarantees away to cut steps** — Rejected. [0002] separates guarantees from flexibility; invariants are the hard, non-pluggable core. "Make onboarding cheaper" may never mean "ship fewer invariants" (rule 8).
- **A batteries-included heavier default to reduce step count** — Rejected. Already rejected in [0009] ("a multi-GB default is an adoption barrier for a library under evaluation"); this ADR cross-references that decision and does not reopen it.
- **Treat the budget as a positioning posture / review heuristic only** — Rejected as toothless. The defensible rules are bound to CI checks (signature introspection, base-only quickstart smoke, import-graph, db-guard) so the budget is gated evidence, mirroring how [0006]/[0009] turn assertions into checks.
- **Frame onboarding around an impressive demo** — Rejected. [0003] rejects the showcase identity; the budget is framed from the cold-`pip install` evaluator POV.
- **Re-decide core vs extras here** — Rejected as out of lane. [0005] owns *what is in core*; this ADR owns *cost to first answer*. Keeping the boundary clean avoids reading as a supersession of [0005].

## Consequences

- A `tests/budget/` module makes ~6 prose rules into teeth: signature introspection (rule 2), import-graph (rule 10), db-guard parametrized over shell entry points (rule 5), curated `__all__` set (rule 3).
- A **separate base-only CI venv** job runs the quickstart and `ragspine ask` to assert rules 1/4 (the `[dev,service]` suite cannot catch an accidental first-answer dependency).
- **0 network** — the strongest, most distinctive claim vs LangGraph — becomes a hard assertion: quickstart runs with sockets disabled and must still exit 0.
- Simplicity-serving fixes ship first: the 3-line db-guard into `scripts/ask.py`, the README Quickstart fix (guarded `ragspine ask` + "run the demo first" note), the 4-name root `__all__` re-export (verify it pulls only the 4 zero-SDK core modules, keeping the rule-10 import-graph test green), and the silent-fallback fix. The heavy proof suite (broad attestation, expanded eval/benchmark, registry tooling, additional trace sinks, full `DomainProfile` generality) ranks **after** these and behind the opt-in tier.
- Doc-grep gates (rules 6/7) are low-confidence guards: they catch the known breach and accept false negatives; they are not a claim that *every* example is enforced.
- **Verification:** `scripts/ci.sh` gains a budget stage (in-process: rules 2/3/5/10) plus the base-only venv job (rules 1/4 incl. the no-network assertion). The four standing invariants (anti-fabrication, provenance, RESTRICTED two-exit, privacy traces) remain gated by the existing regression suite, which rule 8 leans on rather than duplicating.
- Not added to the [0002] decision-index table (0002 is immutable and scoped to the original eight).
