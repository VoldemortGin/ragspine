#!/usr/bin/env python3
"""Read-only: run the formula proof over every Formula object of a saved processing id.

Nothing is written: the stores are opened for reading, the pinned PDF is re-observed and
``qualify_formula`` is reported per object. The AIA release holds no Formula object today
(``formula objects seen: 0``), so this is the check that runs the moment one appears.
"""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.formula_qualification import qualify_formula
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.extraction.evidence.page.models import ObjectKind, PageInput, PagePartition


def run(*, source_store: Path, processing_store: Path, processing_id: str) -> int:
    """Print one JSON line per Formula object and return how many were seen."""
    sources = LocalDocumentStore(source_store, activate_on_publish=False)
    outputs = ProcessingStore(processing_store)
    manifest = outputs.load(processing_id)
    source = sources.load(manifest.scope.source_manifest_id)
    pdf = sources.get(source.manifest.source)
    seen = 0
    for record in manifest.pages:
        if record.partition.artifact is None:
            continue
        partition = TypeAdapter(PagePartition).validate_json(
            outputs.assets.get(record.partition.artifact)
        )
        items = [item for item in partition.objects if item.kind is ObjectKind.FORMULA]
        if not items:
            continue
        source_page = source.manifest.pages[record.page_index]
        page = PageInput(
            manifest.scope.source_manifest_id,
            manifest.scope.source_sha256,
            record.page_index,
            source_page.width,
            source_page.height,
            source_page.svg,
            read_text_sidecar(sources, source, record.page_index),
        )
        for item in items:
            seen += 1
            try:
                result = qualify_formula(pdf, page=page, item=item, model_ir=None)
            except ValueError as error:
                print(
                    json.dumps(
                        {
                            "page": record.page_index + 1,
                            "object": item.object_id,
                            "error": str(error),
                        }
                    )
                )
                continue
            print(
                json.dumps(
                    {
                        "page": record.page_index + 1,
                        "object": item.object_id,
                        "proof_level": None if result.ir is None else result.ir.proof_level,
                        "linear": None if result.ir is None else result.ir.linear,
                        "diagnostics": list(result.diagnostics),
                    },
                    ensure_ascii=False,
                )
            )
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-store", type=Path, required=True)
    parser.add_argument("--processing-store", type=Path, required=True)
    parser.add_argument("--processing-id", required=True)
    arguments = parser.parse_args()
    seen = run(
        source_store=arguments.source_store,
        processing_store=arguments.processing_store,
        processing_id=arguments.processing_id,
    )
    print(f"formula objects seen: {seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
