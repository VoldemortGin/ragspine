# Glossary

Project terminology. Exempt from drift tracking (terminology, not code-bound).

<!-- TODO: keep alphabetical; one term per line. Seed entries: -->

- **Dual channel** — the structured numeric path + the narrative RAG path, run
  deterministically and (for composite questions) compared and merged.
- **IR / StyledGrid** — the frozen intermediate representation that documents are
  extracted into before ingestion.
- **Provenance** — the `source_doc_id` + locator carried by every fact and answer.
- **Anti-fabrication guard** — the orchestrator step that rewrites an answer to
  "not found" when the structured channel returns no `found` fact.
