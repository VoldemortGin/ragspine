---
status: accepted
date: 2026-06-17
---

# ADR 0007 — Multilingual: architect for five, ship two; the rest as locale packs

> Immutable record. Exempt from drift tracking (no `covers`). Supersede, don't edit.

Part of the [0002](0002-product-direction.md) product direction. Gated by [0003](0003-audience-oss-library.md).

## Context

The goal is a worldwide user base: Chinese first, eventually Chinese / English /
Japanese / Italian / German. "Multilingual" is layered — answer language, deterministic
message strings (currently hardcoded Chinese), input parsing, retrieval tokenization
(BM25 is CJK uni+bigram; Japanese wants real segmentation; Italian/German want
stemming), and per-language evaluation. Shipping Chinese-only risks under-designing the
abstraction needed for the later five-language expansion.

## Decision

**Architect for five, ship two.** Thread a `locale` seam through *all* layers —
tokenizer registry, message catalog, intent vocabulary, prompt templates — each
locale-keyed and `Protocol`-pluggable like everything else. Ship and test **Chinese +
English** in core. English is the second language deliberately: it is the OSS lingua
franca, and being non-CJK it forces the tokenizer/stemming/space-delimited abstraction
to be real rather than CJK-only. Japanese / Italian / German ship as community
**locale packs** with a contributor checklist.

## Alternatives considered (rejected)

- **Output-locale only / thin i18n (option A)**: `locale` controls answer language + a
  message catalog; input/tokenization/corpus unchanged. Rejected — under-designs the
  seam for the five-language goal.
- **Full per-language pipeline localization now (option B)**: all five languages
  first-class immediately. Rejected — build the seam and two reference locales, not five.

## Consequences

- Locale packs are a concrete test of the `Protocol` extension story for outsiders,
  reinforcing the OSS-library identity ([0003](0003-audience-oss-library.md)).
- Locale-keyed prompt templates are realized by [0008](0008-prompt-registry-packaging.md).
- Per-language intent vocabulary interacts with the parser decision in [0010](0010-intent-parser-security-decoupling.md).
- New artifacts: `LocaleProfile`, a tokenizer registry, a message catalog, a "how to
  add a locale" guide.
