---
status: accepted
date: 2026-06-17
---

# ADR 0008 — Prompts: packaged PromptRegistry under ragspine/prompts/<locale>/

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction.

## Context

Prompts should move out of inline code. A prompt is three-dimensional: a **template**
(with placeholders) × a **locale** ([0007](0007-multilingual-architect-for-five-ship-two.md))
× **`DomainProfile` values** ([0004](0004-domain-profile-generalization.md)), and the
"no hardcoded company/domain" invariant means templates must be filled at runtime, never
literal. The originally proposed location — a repo-root `prompts/` folder — does **not
ship in the wheel** (`packages=['ragspine']`), so `pip install` users (the audience in
[0003](0003-audience-oss-library.md)) would not get it.

## Decision

Externalize prompts into **packaged** template files at
`ragspine/prompts/<locale>/<category>/*.jinja`, behind a **`PromptRegistry` Protocol**
(default file-backed, user-overridable). Placeholders are filled from `DomainProfile` at
runtime. Keep prompts **centralized** (one tree, locale-keyed), not co-located per
domain module.

## Alternatives considered (rejected)

- **Repo-root `prompts/` folder (the original proposal)**: correct to externalize,
  wrong location — it would not ship to `import ragspine` users.
- **Co-located `ragspine/<domain>/prompts/<locale>/`**: matches the docs "find by
  folder" co-location, but scatters a localizable asset across eight domains. Rejected
  because the readers here are translators / locale-pack contributors, who want one
  directory per locale, not a cross-domain hunt (a different access pattern than code docs).

## Consequences

- Prompts ship with the wheel and are overridable like any other backend.
- A locale pack ([0007](0007-multilingual-architect-for-five-ship-two.md)) is partly
  "drop a new `<locale>/` prompt tree."
- Translators edit flat templates without touching code; diffs stay reviewable.
