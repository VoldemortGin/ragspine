"""run-folder with document tags and an explicit tag filter (ADR 0049).

Three authored PDFs laid out ``<region>/<year>/<file>``; a sidecar adds a category. Tags are
read from the folder, recorded under the ingestion root, attached to the catalog, and a filter
the caller passes narrows only the documents a question is asked across.
"""

import json
from pathlib import Path

import pytest

from enterprise_pdf_rag import cli
from enterprise_pdf_rag.adapters.document_catalog import scan_catalog
from enterprise_pdf_rag.adapters.document_tags import SIDECAR_FILE, TAGS_RECORD
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from ragspine.common.evidence.configs import get_settings
from tests.enterprise_pdf_rag.adapters.test_folder_cross_document import _LOW, _script
from tests.enterprise_pdf_rag.adapters.test_folder_pipeline import (
    _MERIDIAN,
    _OFFLINE,
    _ORION,
    _PER_PDF,
    _model_env,
    _pdf,
)
from tests.enterprise_pdf_rag.answers.fake_llm import scripted_client

_TEMPLATE = "{region}/{year}/{file}"
_LAYOUT = (
    ("HK/2024/meridian.pdf", _MERIDIAN),
    ("TH/2024/orion.pdf", _ORION),
    ("HK/2023/alpha.pdf", _LOW),
)
_QUESTIONS = (
    {"id": "meridian", "question": f"What does {_MERIDIAN} say on page 2?"},
    {"id": "orion", "question": f"What does {_ORION} say on page 3?"},
)


def _folder(root: Path, layout: tuple[tuple[str, str], ...] = _LAYOUT) -> Path:
    folder = root / "pdfs"
    for relative, label in layout:
        _pdf(folder / relative, label)
    return folder


def _questions(root: Path) -> Path:
    path = root / "questions.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _QUESTIONS) + "\n")
    return path


def _run(
    root: Path,
    folder: Path,
    *,
    ingestion: Path | None = None,
    **options: object,
) -> FolderPipelineResult:
    llm, _ = scripted_client(
        root / f"answers-{len(list(root.glob('answers-*')))}", _script, max_live_calls=20
    )
    return run_folder_pipeline(
        folder,
        questions=_questions(root),
        ingestion_root=ingestion or root / "ingestion",
        max_live_calls_per_pdf=_PER_PDF,
        build_tree=False,
        embedder=_OFFLINE,
        answer_llm=llm,
        report_dir=root / "reports",
        **options,  # type: ignore[arg-type]
    )


def _by_name(result: FolderPipelineResult) -> dict[str, object]:
    return {Path(item.pdf_path).name: item for item in result.documents}


def _cases(result: FolderPipelineResult) -> dict[str, object]:
    assert result.eval is not None
    return {case.case_id: case for case in result.eval.cases}


@pytest.fixture(scope="module")
def tagged(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, FolderPipelineResult]:
    root = tmp_path_factory.mktemp("tags")
    with pytest.MonkeyPatch.context() as monkeypatch:
        _model_env(monkeypatch)
        monkeypatch.setenv("APP_DOCUMENT_TAG_PATH_TEMPLATE", _TEMPLATE)
        get_settings.cache_clear()
        folder = _folder(root)
        (folder / SIDECAR_FILE).write_text(
            "file,category\nmeridian.pdf,interim\nTH/2024/orion.pdf,annual\n", encoding="utf-8"
        )
        result = _run(root, folder, document_filter='{"region": "HK"}')
    get_settings.cache_clear()
    return root, folder, result


def test_tags_come_from_the_template_and_the_sidecar_and_are_recorded(
    tagged: tuple[Path, Path, FolderPipelineResult],
) -> None:
    root, _, result = tagged
    documents = _by_name(result)
    assert documents["meridian.pdf"].tags == {  # type: ignore[attr-defined]
        "region": "HK",
        "year": "2024",
        "category": "interim",
    }
    assert documents["orion.pdf"].tags == {  # type: ignore[attr-defined]
        "region": "TH",
        "year": "2024",
        "category": "annual",
    }
    assert documents["alpha.pdf"].tags == {"region": "HK", "year": "2023"}  # type: ignore[attr-defined]
    record = json.loads((root / "ingestion" / TAGS_RECORD).read_text(encoding="utf-8"))
    shas = {name: item.sha256 for name, item in documents.items()}  # type: ignore[attr-defined]
    assert record["documents"][shas["alpha.pdf"]] == {"region": "HK", "year": "2023"}
    catalog = scan_catalog(root / "ingestion")
    assert {entry.document_id: entry.tags for entry in catalog.documents} == record["documents"]


def test_the_filter_narrows_only_the_documents_a_question_is_asked_across(
    tagged: tuple[Path, Path, FolderPipelineResult],
) -> None:
    root, _, result = tagged
    shas = {name: item.sha256 for name, item in _by_name(result).items()}  # type: ignore[attr-defined]
    assert result.document_filter == {"region": ["HK"]}
    cases = _cases(result)
    meridian, orion = cases["meridian"], cases["orion"]
    assert meridian.verdict == "answered"  # type: ignore[attr-defined]
    assert meridian.cited_documents == (shas["meridian.pdf"],)  # type: ignore[attr-defined]
    assert meridian.searched_documents == 2  # type: ignore[attr-defined]
    searched = set(meridian.envelope["searched_documents"])  # type: ignore[attr-defined]
    assert searched == {shas["meridian.pdf"], shas["alpha.pdf"]}
    # The filtered-out PDF was ingested and published, but never searched: no answer from it.
    assert _by_name(result)["orion.pdf"].status == "published"  # type: ignore[attr-defined]
    assert orion.verdict == "abstained" and orion.cited_documents == ()  # type: ignore[attr-defined]

    markdown = (root / "reports" / "report.md").read_text(encoding="utf-8")
    assert (
        '- document filter: `{"region": ["HK"]}` (2 of 3 published documents searched)' in markdown
    )
    assert "| tags |" in markdown
    assert "category=interim; region=HK; year=2024" in markdown


def test_ranking_inside_the_kept_documents_is_what_they_rank_alone(
    tagged: tuple[Path, Path, FolderPipelineResult],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, result = tagged
    _model_env(monkeypatch)
    alone = _folder(tmp_path, tuple(item for item in _LAYOUT if item[0].startswith("HK/")))
    unfiltered = _run(tmp_path, alone)
    assert unfiltered.document_filter is None
    filtered_case = _cases(result)["meridian"]
    plain_case = _cases(unfiltered)["meridian"]
    for key in ("member_ids", "claims", "status", "fusion_mode", "searched_documents"):
        assert filtered_case.envelope[key] == plain_case.envelope[key], key  # type: ignore[attr-defined]


def test_retagging_never_reingests_and_no_filter_searches_every_document(
    tagged: tuple[Path, Path, FolderPipelineResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, folder, first = tagged
    _model_env(monkeypatch)
    monkeypatch.setenv("APP_DOCUMENT_TAG_PATH_TEMPLATE", _TEMPLATE)
    (folder / SIDECAR_FILE).write_text("file,category\nmeridian.pdf,annual\n", encoding="utf-8")
    again = _run(root, folder)
    before, after = _by_name(first), _by_name(again)
    for name, item in after.items():
        assert item.live_calls == 0, name  # type: ignore[attr-defined]
        assert item.index_reused, name  # type: ignore[attr-defined]
        assert (
            item.publication.processing_id  # type: ignore[attr-defined]
            == before[name].publication.processing_id  # type: ignore[attr-defined]
        )
    assert after["meridian.pdf"].tags["category"] == "annual"  # type: ignore[attr-defined]
    assert after["orion.pdf"].tags == {"region": "TH", "year": "2024"}  # type: ignore[attr-defined]
    record = json.loads((root / "ingestion" / TAGS_RECORD).read_text(encoding="utf-8"))
    assert record["documents"][after["orion.pdf"].sha256] == {"region": "TH", "year": "2024"}  # type: ignore[attr-defined]
    assert {case.searched_documents for case in _cases(again).values()} == {3}  # type: ignore[attr-defined]


def test_without_tags_or_a_filter_nothing_is_written_and_the_report_has_no_tag_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, _LAYOUT[:2])
    events: list[str] = []
    result = _run(tmp_path, folder, progress=lambda event, _payload: events.append(event))
    assert not (tmp_path / "ingestion" / TAGS_RECORD).exists()
    assert all(item.tags == {} for item in result.documents)
    assert result.document_filter is None
    assert "document_tags_resolved" not in events
    markdown = (tmp_path / "reports" / "report.md").read_text(encoding="utf-8")
    assert "| tags |" not in markdown and "document filter" not in markdown
    assert all(entry.tags == {} for entry in scan_catalog(tmp_path / "ingestion").documents)


def test_a_malformed_filter_or_template_stops_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_env(monkeypatch)
    folder = _folder(tmp_path, _LAYOUT[:1])
    with pytest.raises(ValueError, match="document filter"):
        _run(tmp_path, folder, document_filter='{"year": 2024}')
    monkeypatch.setenv("APP_DOCUMENT_TAG_PATH_TEMPLATE", "{region}/{year}")
    with pytest.raises(ValueError, match="APP_DOCUMENT_TAG_PATH_TEMPLATE"):
        _run(tmp_path, folder)
    assert not (tmp_path / "ingestion").exists()


def test_cli_passes_the_document_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake(*_args: object, **kwargs: object) -> object:
        seen.append(kwargs["document_filter"])
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "run_folder_pipeline", fake)
    base = ["run-folder", "--folder", str(tmp_path), "--max-live-calls-per-pdf", "0"]
    assert cli.main(base) == 1
    assert cli.main([*base, "--document-filter", '{"year": ["2024"]}']) == 1
    assert seen == [None, '{"year": ["2024"]}']
