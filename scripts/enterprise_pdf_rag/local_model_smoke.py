"""Run one explicit synthetic embedding and rerank request through configured tunnels."""

import json
import sys

from ragspine.common.evidence.providers.local_models import (
    LocalEmbeddingAdapter,
    LocalRerankAdapter,
)
from ragspine.common.evidence.providers.providers import load_local_model_config


def main() -> int:
    embedder = LocalEmbeddingAdapter(load_local_model_config("embedding"))
    rerank_config = load_local_model_config("rerank")
    reranker = LocalRerankAdapter(rerank_config)
    vector = embedder.embed_description(
        "Synthetic probe: revenue increased from one period to the next."
    )
    ranked = reranker.rerank(
        "Which synthetic statement reports an increase?",
        (
            "Synthetic statement A reports no change.",
            "Synthetic statement B reports an increase.",
        ),
        limit=2,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "embedding_fingerprint": embedder.fingerprint,
                "embedding_dimension": len(vector),
                "rerank_model": rerank_config.model,
                "rerank_indexes": [result.index for result in ranked],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
