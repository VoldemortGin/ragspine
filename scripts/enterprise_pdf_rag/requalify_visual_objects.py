#!/usr/bin/env python3
"""Re-prove a snapshot's visual objects from its stored branches and save a new draft.

Calls no model and moves no pointer; ``--dry-run`` writes nothing at all.
"""

import argparse
import sys
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.adapters.visual_requalification import (
    RequalificationSummary,
    requalify_visual_objects,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-store", type=Path, required=True, help="source document store")
    parser.add_argument(
        "--processing-store", type=Path, required=True, help="processing snapshot store"
    )
    parser.add_argument("--processing-id", required=True, help="the snapshot to re-prove")
    parser.add_argument(
        "--dry-run", action="store_true", help="report the verdicts without writing anything"
    )
    parser.add_argument("--out", type=Path, help="also write the JSON summary to this path")
    args = parser.parse_args(argv)
    summary = requalify_visual_objects(
        LocalDocumentStore(args.source_store.resolve(), activate_on_publish=False),
        ProcessingStore(args.processing_store.resolve()),
        processing_id=args.processing_id,
        dry_run=args.dry_run,
    )
    payload = TypeAdapter(RequalificationSummary).dump_json(summary, indent=2).decode()
    if args.out is not None:
        out: Path = args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
