---
covers:
  - ragspine/pipeline/
verified-against: ce9b364
---

# pipeline — agent contract

Auto-loaded when working under `ragspine/pipeline/`. Keep terse; deep dives go in
`ragspine/pipeline/docs/`.

## What lives here

Pipeline-topology export — the code-first answer to "Dify/LangGraph give me a
visual graph". From the *real wiring* (not a hand-drawn picture), derive a static
`PipelineGraph` and emit Mermaid / DOT / JSON.

- `graph.py` — leaf, zero-dependency value layer: frozen `Node` / `Edge` /
  `PipelineGraph` + the three exporters (`to_mermaid` / `to_dot` / `to_dict`) and
  `merge()`. Imports nothing from the rest of the repo.
- `topology.py` — the three builders: `agent_topology(*, narrative_retriever=None)`,
  `retriever_topology(retriever)`, `service_topology(app)`. Each derives the graph
  from the live composition (duck-typed), reflecting what is actually present.

The CLI lives at `scripts/topology.py`; `HybridRetriever.topology()` is a thin
delegator to `retriever_topology`.

## Invariants

- **Static + deterministic.** v1 derives the graph from the assembled objects —
  no execution, no tracing. Nodes/edges are emitted in stable declared order, so
  exports are byte-identical across calls (regenerated diagrams diff cleanly).
- **Every `Node.symbol` must resolve.** Code-backed nodes carry a dotted `symbol`;
  the drift guard resolves each via `importlib`, so a renamed/removed component
  fails the build instead of leaving a lying diagram. Conceptual nodes (e.g. the
  route diamond) carry `symbol=None` and are skipped by the guard.
- **No orchestrator imports.** This package imports nothing from agent / retrieval
  / service at module level — all introspection is duck-typed (`getattr`) and all
  symbols are strings. This keeps the graph types a leaf concern (zero import
  cycles); `HybridRetriever.topology()` delegates *into* here, never the reverse.
- **Wiring-faithful.** A node appears only when its component is wired: vector node
  iff `embedding_backend`, multi-query iff `query_rewriter`, narrative branch iff a
  narrative retriever. The diagram tells the truth about *this* pipeline.
- **rerank is NOT in the retriever subgraph.** `HybridRetriever` ends at RRF/top_k;
  the listwise judge is a downstream `link`-layer stage, so it rides a `data` edge off
  the narrative-retriever node in the agent topology (deliberate PRD-vs-reality
  reconciliation — the judge is judge-conditional and lives inside the retriever's
  `retrieve`, not as a separate sequential agent stage).
- **`SecurityGate` is consulted *inside* `clarify_scope`**, not a sequential stage after
  it — so the four `ClarificationResult` modes (out_of_scope / ask_first / none /
  answer_with_assumptions) are conditional edges out of the `clarify` node, and the gate
  hangs off a `data` edge. Don't reattach the mode exits to the gate node.
- **`domain` grouping is data-only in v1.** `Node.domain` round-trips via `to_dict` for
  external grouping/UIs, but `to_mermaid`/`to_dot` do NOT emit subgraph/cluster blocks —
  visual subgraph rendering is out of scope for v1.

## Read before editing

- **The `Submodules:` index in `__init__.py` must match the directory** (graph.py +
  topology.py). `scripts/check_docstring_refs.py` fails the build otherwise.
- **Don't add a runtime/per-request trace here.** That is a separate future PRD
  built on `common/observability`, not on this static topology.

## Deep dives

Planned (`ragspine/pipeline/docs/topology.md`, not written yet): the static-vs-runtime
boundary, the drift-guard contract, and the PRD-vs-reality reconciliation notes.
