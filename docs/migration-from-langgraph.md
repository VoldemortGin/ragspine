# Migrating a RAG use case from LangGraph or Dify

RAGSpine provides grounded numeric and narrative Q&A as a Python library. This guide
explains how to move that part of an application, or embed it in an existing workflow.
It is not a claim of LangGraph API compatibility or a replacement for the whole Dify platform.

The [LangGraph/Dify capability roadmap](prd-langgraph-dify-capabilities.md) describes the
Spine family's planned expansion and acceptance criteria. Planned capabilities are not
shipped guarantees: general durable workflow execution and compatible checkpoint replay
are not completed by this migration guide, a fact database, or conversation persistence.

> This guide carries no `covers:` frontmatter and is exempt from drift tracking, like the
> glossary and ADRs. Capability statements were checked against official documentation on
> 2026-09-26; use the linked specifications and tests for RAGSpine's exact guarantees.

## Compare the right layers

- **LangGraph** is an orchestration framework and runtime. Its Graph API supports state,
  nodes, edges, and reducers; its Functional API supports ordinary Python control flow.
  Nodes can be deterministic Python functions, model calls, or a mixture. A graph that
  only uses local functions does not require an LLM API key or network access. Using a
  hosted model introduces that provider's requirements, not a requirement of graph
  execution itself. See the official [overview](https://docs.langchain.com/oss/python/langgraph/overview)
  and [Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api).
- **Dify** is an application platform with visual workflow authoring and managed or
  self-hosted deployment options. Applications can be exported and imported as YAML DSL.
  Export includes orchestration and configuration, but not knowledge-base data, usage
  logs, or third-party tool API keys. Secret environment variables require care when
  exporting. A DSL file is portable configuration for Dify, not a standalone Python
  program or a complete deployment backup. See [app export and import](https://github.com/langgenius/dify-docs/blob/main/en/cloud/use-dify/workspace/app-management.mdx).
- **RAGSpine** supplies domain-specific retrieval, structured facts, provenance, and answer
  controls. The Spine family separates general orchestration into `spineagent` and
  application concerns into `spinestudio`; see the [family roadmap](prd-langgraph-dify-capabilities.md).

The useful comparison is which guarantees each layer already implements and which ones
an application must implement and test. Graph orchestration does not inherently cause
fabrication, and a library interface does not by itself prove answer correctness.

## Concept mapping: LangGraph → RAGSpine

| LangGraph application concept | RAGSpine counterpart | Migration boundary |
|---|---|---|
| A node or tool that answers a RAG question | `answer_question(question, store, provider)` | Replace the RAG operation, or call it inside the existing graph. |
| User-defined graph state and reducers | Inputs and `AgentResult` for a Q&A call | No automatic translation of arbitrary state, reducers, or routing. |
| Local node, mock model, or hosted model | `MockProvider` or a configured provider | Both can run local deterministic logic; provider choice determines credentials and network needs. |
| A numeric lookup tool | Profile-driven `query_metric` and fact storage | Map metrics, entities, periods, units, and provenance explicitly. |
| Checkpointer and execution history | No equivalent in `FactStore` | A SQLite fact store persists facts, not graph checkpoints or suspended execution. |
| Application-defined citation/refusal checks | Built-in checks on supported answer paths | Retain application-specific checks and verify the selected profile and settings. |
| Streaming, interrupts, durable execution, replay | Separate orchestration requirements | Do not infer support from a successful synchronous Q&A call or stored chat history. |

## Grounding is an application contract

LangGraph can route deterministically and run validation or refusal nodes before returning
an answer. Its general orchestration primitives do not automatically impose RAGSpine's
specific fact schema or answer policy; a LangGraph application can implement those policies
itself or delegate the RAG step to RAGSpine. Similarly, Dify workflows need an explicit
policy for retrieval misses, citations, and unsupported claims.

RAGSpine's structured numeric path binds answers to stored facts and their provenance.
The offline example in `scripts/examples/minimal_rag.py` creates one synthetic fact and
asks for a present and an absent year:

```bash
python scripts/examples/minimal_rag.py
```

The present fact is returned with a source; the absent fact is refused in this example.
The example has no narrative retriever. In a configured application, a structured miss
can instead try a grounded narrative fallback, enabled by default; if no acceptable
fallback is found, the structured refusal remains. See
[ADR 0023](adr/0023-structured-miss-narrative-fallback.md).

The narrative number guard is also enabled by default and checks numbers against retrieved
evidence, rewriting unsupported numeric answers. It has documented limits and can be
explicitly disabled; it is not a proof of every natural-language claim or of the source
document's truth. See [ADR 0024](adr/0024-narrative-number-guard.md). Keep domain-specific
acceptance tests for source quality, authorization, units, and answer meaning.

| Execution choice | API key needed for this path? | Network needed for this path? |
|---|---|---|
| LangGraph with pure local Python nodes | No | No |
| LangGraph calling a hosted model | Depends on the provider | Normally yes |
| RAGSpine's synthetic example with `MockProvider` | No | No |
| RAGSpine calling a hosted model or remote store | Depends on the provider | Depends on the configured services |

These are runtime requirements after dependencies are installed, not installation or
model-download requirements. They are not a performance or code-size benchmark.

## When to retain the existing system

- Keep **LangGraph** for stateful workflow orchestration, interrupts, streaming, and
  checkpoint-based durable execution or time travel that your application relies on.
  RAGSpine can be one operation inside that workflow. Durability also requires appropriate
  checkpointing and application design; a graph compiled without checkpointing cannot
  recover interrupted work. See the official [persistence documentation](https://docs.langchain.com/oss/python/langgraph/persistence).
- Keep **Dify** for visual authoring, application publishing, and platform operations your
  team already uses. Export the DSL to preserve the application definition, then inventory
  its models, plugins, knowledge data, and credentials separately before migration.
- Use **RAGSpine** for the grounded Q&A component when its data model and answer policies
  match your use case. Expanding beyond that component should follow the family roadmap
  and its staged tests, rather than assuming feature parity with either system.

## How to move a RAG use case

1. Capture acceptance cases before changing the workflow: successful retrieval, missing
   facts, conflicting evidence, unsupported numbers, source attribution, and any access
   restrictions. Preserve representative expected results from the existing application.
2. Map structured numbers into `SqliteFactStore` with `source_doc_id` and `source_locator`.
   Use the ingestion layers or upsert synthetic `Fact` values as shown in the minimal
   example. Keep units, periods, and entity/profile mappings explicit.
3. Replace only the relevant RAG node/tool with
   `answer_question(question, store, provider)`. Preserve surrounding orchestration,
   checkpoints, approvals, and error handling until their replacements pass their own tests.
4. Start with `MockProvider` for deterministic offline tests. Select a live or local model
   provider only when needed, and test the provider separately from the retrieval contract.
5. Configure a narrative retriever for narrative/composite answers. Test the enabled
   fallback and number-guard settings explicitly; structured-miss behavior depends on
   whether acceptable narrative evidence is available.
6. For Dify, retain the exported DSL as a reference and migrate knowledge content and
   external dependencies separately. There is no automatic Dify DSL importer or LangGraph
   checkpoint converter promised by this guide.

For the offline tour, run `ragspine quickstart`. For requirements beyond the Q&A operation,
start with the [family capability roadmap and test plan](prd-langgraph-dify-capabilities.md).
