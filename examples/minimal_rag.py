"""Minimal RAGSpine: store one fact, ask two questions, see grounded answer + honest refusal.

Fully offline, no API key, no network. Run from the project root:

    python examples/minimal_rag.py

The four names below are RAGSpine's entire minimal-viable API.
"""

from ragspine.storage.fact_store import Fact, SqliteFactStore
from ragspine.agent.llm_provider import MockProvider
from ragspine.agent.agent import answer_question

# 1. A fact store (in-memory here; pass a path for a real sqlite db).
store = SqliteFactStore(":memory:")
store.init_schema()

# 2. One fact. The last two fields are its provenance (which doc, where in it).
store.upsert_facts([
    Fact(
        metric_code="REVENUE", entity="ACME_CN", geography="CN",
        channel="TOTAL", period_type="FY", period="2024",
        value=1320.0, unit="USD_M",
        source_doc_id="ACME_FY2024_Results.pptx",
        source_locator="slide=6,table=1,row=2,col=3",
    ),
])

# 3. A deterministic offline provider — no key, no network.
provider = MockProvider()

# 4. Ask. The stored fact is found and cited; the missing one is refused, not fabricated.
for question in (
    "中国内地FY2024的REVENUE是多少",   # exists -> grounded answer
    "中国内地FY2030的REVENUE是多少",   # absent -> honest refusal
):
    result = answer_question(question, store, provider)
    print(f"Q: {question}")
    print(f"A: {result.answer}")
    for src in result.sources:
        print(f"   source: {src['doc']} · {src['locator']}")
    print()

store.close()
