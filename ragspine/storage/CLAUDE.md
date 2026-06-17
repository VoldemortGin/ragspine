---
covers:
  - ragspine/storage/
verified-against: bcb7144
---

# storage — agent contract

Auto-loaded when working under `ragspine/storage/`. Keep terse; deep dives go in
`ragspine/storage/docs/`.

## What lives here

Fact store (numeric) + chunk store (narrative), sqlite-backed, full lineage.

## Invariants

- **Provenance** — every stored fact/chunk carries `source_doc_id` + locator;
  never drop lineage.

## Read before editing

<!-- TODO -->

## Deep dives

<!-- none yet -->
