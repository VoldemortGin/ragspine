"""Keep separate immutable observations for repeated retrieval evaluations."""

import json
import re
from hashlib import sha256
from pathlib import Path

from enterprise_pdf_rag.adapters.processing_store import ProcessingStore


def _write_once(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError("Retrieval evaluation file conflicts with immutable bytes") from None


def save_evaluation(
    outputs: ProcessingStore, processing_id: str, example: bytes, validation: bytes
) -> tuple[Path, Path]:
    if re.fullmatch(r"[0-9a-f]{64}", processing_id) is None:
        raise ValueError("Retrieval evaluation requires a processing SHA-256")
    example_ref = outputs.assets.put(example, media_type="application/json")
    validation_ref = outputs.assets.put(validation, media_type="application/json")
    identity = sha256(
        ("retrieval-evaluation-v1:" + example_ref.sha256 + ":" + validation_ref.sha256).encode()
    ).hexdigest()
    target = outputs.root / "runs" / processing_id / "retrieval-evaluations" / identity
    target.mkdir(parents=True, exist_ok=True)
    example_path = target / "retrieval-example.json"
    validation_path = target / "retrieval-validation.json"
    _write_once(example_path, outputs.assets.get(example_ref))
    _write_once(validation_path, outputs.assets.get(validation_ref))
    _write_once(
        target / "evaluation.json",
        json.dumps(
            {
                "example_sha256": example_ref.sha256,
                "validation_sha256": validation_ref.sha256,
            },
            sort_keys=True,
        ).encode(),
    )
    return example_path, validation_path
