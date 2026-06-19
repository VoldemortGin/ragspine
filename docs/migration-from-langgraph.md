# Migrating to RAGSpine from LangGraph (and a note on Dify)

A guide for teams evaluating RAGSpine after hitting friction with LangGraph or Dify. It is
deliberately fair: RAGSpine occupies a **narrow niche** — grounded numeric + narrative Q&A
with refusal-on-missing-data and citations — not general agent orchestration. If you need a
general stateful agent graph, you should keep LangGraph; this guide tells you honestly when
*not* to switch.

> This is a guide, not a code-bound contract — it carries no `covers:` frontmatter and is
> exempt from drift tracking, like the glossary and the ADRs.

## First, don't conflate the two

LangGraph and Dify are very different tools, and lumping them together is a strawman:

- **LangGraph** is an **MIT-licensed pip library** with **no runtime and no UI** — the same
  delivery model as RAGSpine. You `pip install` it and write Python. Its "abstraction tax" is
  **conceptual**: to do anything you first model your flow as a graph (`StateGraph`, nodes,
  edges, conditional edges, a typed `State` with reducers), then `compile()` and `invoke()`.
- **Dify** is a **self-hosted platform**: a multi-container deployment (Postgres, Redis, a
  vector store, a sandbox, a plugin daemon, API/worker/web/nginx) with a **visual workflow
  builder** whose app definitions live in Postgres. Its tax is **operational + lock-in**:
  you run and own a cluster, and your application is a database-backed visual DSL, not a
  portable Python artifact.

RAGSpine competes with the *first* on "you don't need a graph model to do RAG," and with the
*second* on "this is a library you import, not a platform you operate."

## Concept mapping: LangGraph → RAGSpine

| LangGraph | RAGSpine | Note |
|---|---|---|
| `StateGraph` + nodes + edges + conditional edges | *(none)* | Orchestration is **internal** to `answer_question`; there is no graph DSL to learn or assemble. |
| `State` (`Annotated[TypedDict, reducer]`) | *(none on the path)* | No user-authored state object for a basic Q&A. |
| `graph.compile()` / `.invoke()` / `.stream()` | `answer_question(question, store, provider)` | One call, **3 args**. No compile step. |
| `init_chat_model("claude-…")` (live key required) | `MockProvider()` | Deterministic, **offline, no key**. `AnthropicProvider` is opt-in behind the `[llm]` extra. |
| `@tool` + `bind_tools` | built-in `query_metric` | The grounded numeric tool ships and is profile-driven — you don't wire it. |
| message types (`SystemMessage` / `HumanMessage` / `ToolMessage`) | plain strings in/out | `answer_question` takes a `str`, returns an `AgentResult`. |
| checkpointer / persistence (`MemorySaver`, `PostgresSaver`) | `FactStore` (sqlite) | Persistence here is **your fact store**, not a graph execution checkpointer. |
| provenance / citations (bolt-on, your responsibility) | `source_doc_id` + `source_locator` on every fact; `result.sources` | **Code-enforced**, not opt-in. See [ADR 0012](adr/0012-onboarding-complexity-budget.md) and the README. |

The point of the table is the empty cells: most of what you assemble by hand in LangGraph
has **no counterpart to learn** in RAGSpine because routing, the tool loop, and the
anti-fabrication guard run transparently inside the one call.

## The difference that matters: fabricate vs. refuse

LangGraph's correctness is *emergent at runtime* — the model chooses the next node and writes
the answer — so when the data isn't there, a live agent will typically produce a **plausible,
unsourced number**. There is no built-in mechanism that overrides the model and refuses; you
must engineer it. (And it cannot run keyless at all, so you can't even see this behavior
offline.)

A sketch of the LangGraph path (prebuilt agent — the *shortest* version, which also hides the
graph you'd otherwise hand-build):

```python
from langchain.chat_models import init_chat_model
from langgraph.prebuilt import create_react_agent

model = init_chat_model("anthropic:claude-…")          # requires a real API key
agent = create_react_agent(model, tools=[my_revenue_tool])
out = agent.invoke({"messages": [("user", "中国内地FY2030的REVENUE是多少")]})
# → a fluent answer that may invent a number; provenance is whatever you remembered to thread through.
```

The same missing fact in RAGSpine (`examples/minimal_rag.py`, fully offline, no key):

```bash
$ python examples/minimal_rag.py
Q: 中国内地FY2024的REVENUE是多少
A: ACME_CN FY2024 REVENUE：1320 USD_M（来源：ACME_FY2024_Results.pptx · slide=6,table=1,row=2,col=3）
   source: ACME_FY2024_Results.pptx · slide=6,table=1,row=2,col=3

Q: 中国内地FY2030的REVENUE是多少
A: 查不到：REVENUE / ACME_CN / 2030（渠道 TOTAL）未在事实表中找到。为避免误导，不提供任何推测数字…
```

When the structured channel returns no fact, the orchestrator **deterministically rewrites the
answer to "not found"** regardless of what the model said — anti-fabrication lives in the
control flow, not in a prompt. That refusal is frozen by a regression test and gated in CI.

**The honest tax comparison** (order of magnitude, not a benchmark):

| | concepts to first answer | LOC (first hand-built) | API key | network |
|---|---|---|---|---|
| LangGraph (hand-built graph) | ~5–7 (StateGraph, State, nodes, edges, tools, messages) | ~60 | required | required |
| LangGraph (prebuilt `create_react_agent`) | ~3 (hides the graph) | ~10 | required | required |
| **RAGSpine** | **4** (`FactStore`, `Fact`, `MockProvider`, `answer_question`) | **~15** | **none** | **none** |

## When NOT to use RAGSpine

Route yourself fairly — these are real reasons to stay where you are:

- **Stay on LangGraph** if you need arbitrary stateful / multi-agent **graphs**, human-in-the-
  loop interrupts, durable execution, time-travel checkpointing, token streaming, or the
  LangChain/LangSmith ecosystem. RAGSpine **deliberately does not** compete on general graph
  orchestration — copying that would require giving the model control of routing, which would
  destroy the determinism that makes its guarantees provable (see the non-goals in
  [ADR 0012](adr/0012-onboarding-complexity-budget.md) and the product direction in
  [ADR 0002](adr/0002-product-direction.md)).
- **Stay on Dify** if you need a no-code platform, a non-developer team, a hosted chat UI,
  multi-tenant app management, or a bundled vector store + model marketplace. RAGSpine is a
  library you import, not a platform you operate.
- **Use RAGSpine** when you need grounded numeric + narrative Q&A with **refusal-on-missing-
  data and citations**, as plain Python embedded in your own backend, runnable offline with no
  key. RAGSpine targets the cold-`pip install` engineer evaluating exactly that
  ([ADR 0003](adr/0003-audience-oss-library.md)).

## A note on lock-in (precise, not loaded)

- **LangGraph** lock-in is **conceptual** (your logic is expressed as a graph) plus ecosystem
  gravity — **not** licensing (MIT) and **not** platform (the OSS checkpointers work without
  the paid LangGraph Platform).
- **Dify** lock-in is the deepest: your application is a visual DSL + plugin configuration
  living in Postgres behind a running cluster, with **no portable Python artifact** to lift
  out.
- **RAGSpine** is plain Python calling typed `Protocol`s; the core imports zero SDKs and runs
  offline. The thing you "lock into" is ordinary functions you can read in an afternoon.

## How to actually move a RAG use case over

1. Put your numbers in a `FactStore` (sqlite) with their provenance (`source_doc_id` +
   `source_locator`) — RAGSpine's extraction/ingestion layers do this from xlsx/pptx/pdf, or
   you upsert `Fact`s directly as in `examples/minimal_rag.py`.
2. Replace your graph + tool wiring with a single `answer_question(question, store, provider)`.
3. Start with `MockProvider` (offline, deterministic) to validate behavior and your tests with
   no key; swap in `AnthropicProvider` (the `[llm]` extra) only when you want a live model for
   the narrative channel.
4. Keep your narrative documents in the chunk store for the narrative/composite path; the
   structured numeric channel already answers "what's the number" deterministically.

For the 10-second offline tour, run `ragspine quickstart` (no key, no network).
