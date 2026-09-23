"""The real AIA sample is swept for Formula objects read-only, through the shipped script."""

import json
import subprocess
import sys

import pytest

from enterprise_pdf_rag.adapters.processing_runtime import PROCESSING_OUTPUT
from ragspine.common.evidence.settings import ROOT_DIR

_SCRIPT = ROOT_DIR / "scripts" / "enterprise_pdf_rag" / "formula_smoke.py"
pytestmark = pytest.mark.skipif(
    not (PROCESSING_OUTPUT / "current-processing").is_file(), reason="AIA sample store absent"
)


def _state() -> tuple[bytes, bytes, int, int]:
    source_root = PROCESSING_OUTPUT.parent
    return (
        (source_root / "current-manifest").read_bytes(),
        (PROCESSING_OUTPUT / "current-processing").read_bytes(),
        sum(1 for _ in (source_root / "objects" / "sha256").iterdir()),
        sum(1 for _ in (PROCESSING_OUTPUT / "objects" / "sha256").iterdir()),
    )


def test_formula_smoke_reads_the_aia_release_without_touching_it() -> None:
    before = _state()
    processing_id = (PROCESSING_OUTPUT / "current-processing").read_text().strip()

    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--source-store",
            str(PROCESSING_OUTPUT.parent),
            "--processing-store",
            str(PROCESSING_OUTPUT),
            "--processing-id",
            processing_id,
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Traceback" not in completed.stderr
    lines = completed.stdout.strip().split("\n")
    total = lines[-1]
    assert total.startswith("formula objects seen: ")
    seen = int(total.removeprefix("formula objects seen: "))
    # The published AIA pages hold no Formula object today; the sweep still has to run.
    assert seen == len(lines) - 1
    for line in lines[:-1]:
        record = json.loads(line)
        assert {"page", "object"} <= set(record)
    assert _state() == before
