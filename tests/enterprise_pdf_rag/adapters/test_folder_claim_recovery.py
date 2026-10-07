"""A folder run interrupted mid-call no longer loses a page silently and for good (ADR 0023)."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ragspine.common.evidence.providers import json_completion
from tests.enterprise_pdf_rag.adapters.model_cache_helpers import (
    claim_generations,
    claims,
    drop_db_claims,
)
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _MERIDIAN,
    _PAGES,
    _PER_PDF,
    _folder,
    _model_env,
    _run,
)

pytestmark = pytest.mark.usefixtures("model_cache_backend")


def _cache_dir(tmp_path: Path) -> Path:
    """The one per-PDF model cache of a single-PDF folder run."""
    (cache,) = (path for path in (tmp_path / "ingestion").rglob("model-cache") if path.is_dir())
    return cache


def test_an_interrupted_run_shows_the_blocked_call_then_recovers_once_the_lease_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    real: Callable[..., bytes] = vars(json_completion)["_send_once"]
    sent = {"count": 0}

    def interrupted(url: str, **kwargs: object) -> bytes:
        sent["count"] += 1
        if sent["count"] == 2:
            raise KeyboardInterrupt  # the user's Interrupt, or a kernel going away mid-call
        return real(url, **kwargs)

    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    monkeypatch.setattr(json_completion, "_send_once", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, folder)
    monkeypatch.setattr(json_completion, "_send_once", real)
    cache = _cache_dir(tmp_path)
    assert len(claims(cache)) == 1

    # Rerun at once: the claim may still be in flight, so the call is not sent — but it shows.
    blocked = _run(tmp_path, folder)
    (document,) = blocked.documents
    assert document.ingestion is not None
    assert document.ingestion.calls_claim_blocked == 1
    assert document.ingestion.claims_taken_over == 0
    assert (
        document.ingestion.layout_succeeded_pages
        + document.ingestion.metadata_page_states.get("succeeded", 0)
        == 2 * _PAGES - 1
    )
    assert len(claims(cache)) == 1

    # Past the lease the claim's holder is presumed dead: the call is made again, once.
    lease = json_completion._claim_lease(180.0)
    clock = json_completion._wall_clock
    monkeypatch.setattr(json_completion, "_wall_clock", lambda: clock() + lease + 1)
    recovered = _run(tmp_path, folder, max_live_calls_per_pdf=_PER_PDF)
    (document,) = recovered.documents
    assert document.status == "published"
    assert document.ingestion is not None
    assert document.ingestion.claims_taken_over == 1
    assert document.ingestion.calls_claim_blocked == 0
    assert document.ingestion.layout_succeeded_pages == _PAGES
    assert document.ingestion.metadata_page_states == {"succeeded": _PAGES}
    assert claims(cache) == []

    again = _run(tmp_path, folder)
    assert again.live_calls.total == 0
    (document,) = again.documents
    assert document.ingestion is not None and document.ingestion.claims_taken_over == 0


def test_a_claim_left_by_the_old_client_heals_on_a_rerun_once_it_is_old_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is on the user's disk today: a fingerprint-only claim from a killed old kernel."""
    _model_env(monkeypatch)
    real: Callable[..., bytes] = vars(json_completion)["_send_once"]
    sent = {"count": 0}

    def killed(url: str, **kwargs: object) -> bytes:
        sent["count"] += 1
        if sent["count"] == 2:
            raise KeyboardInterrupt
        return real(url, **kwargs)

    folder = _folder(tmp_path, ("meridian.pdf", _MERIDIAN))
    monkeypatch.setattr(json_completion, "_send_once", killed)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, folder)
    monkeypatch.setattr(json_completion, "_send_once", real)
    cache = _cache_dir(tmp_path)
    ((key, generation),) = claim_generations(cache).items()
    assert generation == 0 and len(claims(cache)) == 1
    # The old client's claim: a bare fingerprint in a ``.claim`` file (on the sqlite backend the
    # run's own db claim is replaced by that file, which the backend reads through).
    drop_db_claims(cache)
    claim = cache / "requests" / f"{key}.json.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(key)

    blocked = _run(tmp_path, folder)
    assert blocked.documents[0].ingestion is not None
    assert blocked.documents[0].ingestion.calls_claim_blocked == 1

    stamp = json_completion._wall_clock() - json_completion.LEGACY_CLAIM_LEASE_SECONDS - 1
    os.utime(claim, (stamp, stamp))
    healed = _run(tmp_path, folder)
    (document,) = healed.documents
    assert document.ingestion is not None
    assert (document.ingestion.claims_taken_over, document.ingestion.calls_claim_blocked) == (1, 0)
    assert document.ingestion.layout_succeeded_pages == _PAGES
    assert document.ingestion.metadata_page_states == {"succeeded": _PAGES}
