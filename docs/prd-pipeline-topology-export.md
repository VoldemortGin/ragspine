# PRD — Pipeline Topology Export

> **status:** implemented (2026-06-18) · **created:** 2026-06-17 · **methodology:** TDD (red tests first)
> Implemented in `ragspine/pipeline/` (`graph.py` + `topology.py`), `scripts/topology.py`, and
> `HybridRetriever.topology()`. The live contract is `ragspine/pipeline/CLAUDE.md`; this PRD is
> retained as the originating spec and carries no `covers:` frontmatter. A deep-dive doc at
> `ragspine/pipeline/docs/topology.md` (with `covers:`) is still TODO.

## Problem statement

RAGSpine's value proposition is that it is **framework-free** — you assemble a backend RAG
pipeline out of plain Python instead of submitting to Dify or LangGraph. But those frameworks
ship one thing RAGSpine doesn't: a **visual graph of your pipeline**. When you wire up a
RAGSpine pipeline (intent → route → retrieve → rerank → synthesize, plus the structured
channel, the FAQ short-circuit, and ingestion), there is currently no way to *see* the
topology — for a README diagram, for onboarding a teammate, or for confirming "is the vector
channel actually wired on in this deployment?"

We want a **code-first** way to export the pipeline's topology as a graph: no canvas, no DSL,
no runtime — call a method, get a diagram. This turns "framework-free" from a feature into a
*better* developer experience than the visual-builder frameworks: the graph is generated from
the real code, so it can never drift from a hand-drawn picture.

## Solution

A small, dependency-free graph value type plus exporters, and a `.topology()` method on the
orchestrators that returns the **static** topology of the *actually assembled* pipeline.

- `PipelineGraph` (nodes + edges + title) with `to_mermaid()` / `to_dot()` / `to_dict()`.
- `Agent.topology()`, `HybridRetriever.topology()`, and a service-app topology builder each
  return a `PipelineGraph` reflecting **their real wiring** — e.g. whether a vector backend,
  a listwise judge, or a narrative retriever is present — not a generic hardcoded picture.
- Composition: subgraphs `merge()` into a full-pipeline graph.
- A CLI (`scripts/topology.py`) dumps any format to a file (e.g. `docs/generated/topology.mmd`).

**v1 is static**, derived from the assembled objects. It answers *"what is this pipeline?"* and
renders before any query runs. A *runtime, per-request execution trace* graph (which path a
specific question actually took) is a deliberately separate, future PRD — see Out of Scope.

Mermaid is the default format because GitHub renders it inline in Markdown, so a README can
embed a live, regenerable architecture diagram.

## User stories

1. As a developer, I want `agent.topology().to_mermaid()` so I can paste a pipeline diagram
   straight into my README or design doc.
2. As a developer, I want the graph to reflect **my** wiring — vector channel on/off, judge
   present/absent, narrative retriever present/absent — so the diagram is true to the deployment,
   not a generic illustration.
3. As a developer debugging routing, I want the conditional edges labeled (`route=structured` /
   `route=narrative` / `route=composite`, FAQ `hit`/`miss`, clarify/refuse gates) so the graph
   shows *where* a question can go.
4. As a developer, I want `to_dict()` (JSON) output so I can render the topology in my own UI or
   feed it to another tool.
5. As a developer, I want `to_dot()` (Graphviz) output for richer offline rendering.
6. As an operator, I want a CLI that writes the topology to a file in a chosen format, so docs
   can be regenerated in CI/build (into the git-ignored `docs/generated/`).
7. As a maintainer, I want a test that **fails if the declared topology drifts from the code**
   (a node naming a component that no longer exists), so the diagram can't silently lie.
8. As a contributor, I want deterministic output (stable node order) so regenerated diagrams
   produce clean, reviewable diffs.
9. As a user, I want `HybridRetriever.topology()` to show the retrieval sub-pipeline (BM25 +
   vector → RRF → rerank) so I can reason about my retrieval stack in isolation.
10. As a user assembling the service, I want the service topology to show the FAQ short-circuit
    sitting *in front of* the agent, and the async ingestion path (routes → queue → jobs).

## Proposed API surface

New domain package `ragspine/pipeline/` (screaming-architecture: the graph types are their own
concern, orchestrators only delegate):

```python
# ragspine/pipeline/graph.py
@dataclass(frozen=True)
class Node:
    id: str
    label: str
    kind: str          # "stage" | "store" | "external" | "gate" | "channel"
    domain: str | None = None      # e.g. "retrieval", "agent" — for grouping/subgraphs
    symbol: str | None = None      # dotted path to the code it represents (drift guard)

@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    label: str | None = None       # e.g. "route=structured", "miss", "hit"
    kind: str = "flow"             # "flow" | "conditional" | "data"

@dataclass(frozen=True)
class PipelineGraph:
    title: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    def to_mermaid(self, *, direction: str = "TD") -> str
    def to_dot(self) -> str
    def to_dict(self) -> dict                         # JSON-serializable
    def merge(self, other: "PipelineGraph", *, group: str | None = None) -> "PipelineGraph"
```

`.topology()` accessors (thin; build a `PipelineGraph` from the live composition):

```python
ragspine.agent.agent.Agent.topology(self) -> PipelineGraph       # full request flow
ragspine.retrieval.lexical.retrieval.HybridRetriever.topology(self) -> PipelineGraph
ragspine.pipeline.service_topology(app) -> PipelineGraph         # FastAPI app → graph
```

> Note on `Agent`: the current entry point is the function `answer_question(...)`. This PRD
> assumes either (a) a thin `Agent` class wrapping the orchestration that already holds its
> store/provider/retriever, or (b) a free function `agent_topology(*, narrative_retriever=None,
> ...)`. Decide during triage; the graph contract is identical either way.

CLI:

```bash
.venv/bin/python scripts/topology.py --of mermaid            # to stdout
.venv/bin/python scripts/topology.py --of dot  --out docs/generated/topology.dot
.venv/bin/python scripts/topology.py --of json --out docs/generated/topology.json
```

## Implementation decisions

- **Static, not runtime.** v1 derives the graph from the assembled objects / declared structure.
  No execution, no tracing. (Runtime trace = separate future PRD.)
- **Zero new runtime dependencies.** Mermaid and DOT are emitted as plain strings; JSON via
  stdlib. No Graphviz binary required to *produce* output (rendering DOT is the user's choice).
- **Reflect the real wiring.** `.topology()` introspects what is actually present — e.g.
  `HybridRetriever` reports the vector node only when an embedding backend is injected, and the
  rerank node only when a judge is present; `Agent` reports the narrative branch only when a
  narrative retriever is wired. The diagram tells the truth about *this* pipeline.
- **Deterministic output.** Nodes/edges are emitted in a stable, declared order so regenerated
  diagrams diff cleanly and can be snapshot-tested.
- **Drift guard via `symbol`.** Nodes that map to code carry a dotted `symbol`; a test resolves
  every `symbol` (importable) so a renamed/removed component fails the build instead of leaving a
  lying diagram. This mirrors the repo's `check_doc_drift.py` philosophy.
- **Generated output is git-ignored.** CLI writes under `docs/generated/` (already ignored), so
  diagrams are regenerable, never hand-edited, and never bloat the diff.
- **Composition over a monolith.** Each orchestrator owns its subgraph; the full-pipeline graph
  is `merge()`d, so a user can export just the retrieval stack or the whole flow.

## Testing decisions (TDD — write these red first)

- **Exporters, known graph:** `to_mermaid` / `to_dot` / `to_dict` on a hand-built `PipelineGraph`
  produce valid, deterministic, golden-matchable output (Mermaid parses as a `flowchart`; DOT as
  a `digraph`; dict round-trips through `json.dumps`).
- **Agent topology — branches & gates:** `Agent.topology()` contains the three route branches
  (structured / narrative / composite) and the clarify + out-of-scope-refuse gates, with labeled
  conditional edges.
- **Wiring-faithful:** with a narrative retriever absent, the narrative branch node is absent;
  with it present, it appears. Same for `HybridRetriever`: vector node only when an embedding
  backend is injected; rerank node only when a judge is present.
- **FAQ short-circuit placement:** the service topology shows the FAQ node *upstream* of the
  agent, and the async path routes → queue → jobs.
- **Composition:** `merge()` of two subgraphs yields the union with no duplicate node ids and
  edges preserved.
- **Drift guard:** every `Node.symbol` in every shipped `.topology()` resolves to an importable
  object; a deliberately-bogus symbol fails the guard test.
- **CLI:** `scripts/topology.py --of mermaid|dot|json [--out FILE]` writes the requested format;
  exit 0; file created when `--out` given.
- **Determinism regression:** calling `.topology()` twice yields byte-identical exports.

## Out of scope (v1)

- **Runtime / per-request execution trace graph** (which path a *specific* question took, with
  timings and hit counts). This is valuable but distinct — it belongs to a follow-up PRD and
  would build on the existing `observability` traces, not on the static topology.
- Interactive / web canvas, live editing, or building the pipeline *from* a graph (export is
  read-only).
- Rendering to image formats (PNG/SVG). Output is text (Mermaid/DOT/JSON); rendering is the
  user's tool (GitHub for Mermaid, Graphviz for DOT).
- Auto-embedding the diagram into the README during CI (can be a later `make docs` step).

## Further notes

- GitHub renders Mermaid in Markdown natively, so the README's "Architecture" section could
  embed a generated diagram that stays honest with the code.
- This feature is the concrete, code-first rebuttal to "but Dify/LangGraph give me a visual
  graph" — RAGSpine gives you one *generated from the real wiring*, in three formats, with a
  drift guard, and no runtime.
- Fits the roadmap line already in `README.md` ("Pipeline-topology export (`.topology()` →
  Mermaid/DOT) is on the roadmap").
