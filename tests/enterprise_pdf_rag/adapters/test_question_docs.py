"""Which PDF a question set's ``doc`` names: exact rules, a normalized rule, never a guess."""

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.question_docs import (
    DocResolution,
    QuestionDocsError,
    check_references,
    describe,
    make_resolver,
    normalize_doc_name,
    unresolved_message,
)


def _folder(tmp_path: Path, *names: str, same: tuple[str, ...] = ()) -> tuple[Path, list[Path]]:
    folder = tmp_path / "pdfs"
    for name in names:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Distinct bytes per file unless listed in ``same``.
        path.write_bytes(b"%PDF-1.7 " + (b"shared" if name in same else name.encode()))
    return folder, sorted(folder.rglob("*.pdf"))


def _counting_digest() -> tuple[list[Path], Callable[[Path], str]]:
    read: list[Path] = []

    def digest(pdf: Path) -> str:
        read.append(pdf)
        return hashlib.sha256(pdf.read_bytes()).hexdigest()

    return read, digest


def _resolve(
    tmp_path: Path,
    names: tuple[str, ...],
    reference: str,
    *,
    same: tuple[str, ...] = (),
    aliases: Mapping[str, str] | None = None,
) -> tuple[DocResolution, list[Path]]:
    folder, pdfs = _folder(tmp_path, *names, same=same)
    read, digest = _counting_digest()
    return make_resolver(pdfs, folder, digest, aliases)(reference), read


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("Meridian Report_2024.PDF", "meridian report 2024"),
        ("meridian--report . 2024", "meridian report 2024"),
        (
            "\uff2d\uff25\uff32\uff29\uff24\uff29\uff21\uff2e\u3000\uff32\uff45\uff50\uff4f\uff52\uff54\uff0e\uff50\uff44\uff46",
            "meridian report",
        ),
        ("reports/2024/Meridian.pdf", "meridian"),
        ("reports\\2024\\Meridian.pdf", "meridian"),
        ("  _Meridian_  ", "meridian"),
    ],
)
def test_normalization_folds_width_case_extension_separators_and_directories(
    text: str, normalized: str
) -> None:
    assert normalize_doc_name(text) == normalized


@pytest.mark.parametrize(
    ("reference", "rule"),
    [
        ("Meridian-Report_2024.pdf", "exact"),
        ("MERIDIAN-REPORT_2024.PDF", "exact"),
        ("meridian-report_2024", "stem"),
        ("Meridian Report 2024", "normalized"),
        ("meridian.report.2024.pdf", "normalized"),
        (
            "\uff2d\uff25\uff32\uff29\uff24\uff29\uff21\uff2e\uff0d\uff32\uff25\uff30\uff2f\uff32\uff34\uff3f\uff12\uff10\uff12\uff14",
            "normalized",
        ),
        ("archive/2024/Meridian-Report_2024.pdf", "normalized"),
    ],
)
def test_each_difference_resolves_by_its_rule_without_reading_a_byte(
    tmp_path: Path, reference: str, rule: str
) -> None:
    resolution, read = _resolve(
        tmp_path, ("Meridian-Report_2024.pdf", "Orion FY2024.pdf"), reference
    )
    assert (resolution.status, resolution.rule, resolution.pdf) == (
        "matched",
        rule,
        "Meridian-Report_2024.pdf",
    )
    assert read == []


def test_a_few_letters_apart_is_never_a_match_only_a_candidate(tmp_path: Path) -> None:
    resolution, _ = _resolve(
        tmp_path, ("Meridian Interim Report 2024.pdf", "Orion.pdf"), "Meridian Interim Report 2023"
    )
    assert (resolution.status, resolution.pdf, resolution.rule) == ("unmatched", None, None)
    assert [candidate.pdf for candidate in resolution.candidates] == [
        "Meridian Interim Report 2024.pdf"
    ]
    assert 0.9 < resolution.candidates[0].similarity < 1.0


def test_several_pdfs_under_one_name_are_ambiguous_unless_they_are_the_same_bytes(
    tmp_path: Path,
) -> None:
    resolution, _ = _resolve(tmp_path / "a", ("x/report.pdf", "y/report.pdf"), "report.pdf")
    assert resolution.status == "ambiguous" and resolution.pdf is None
    assert resolution.ambiguous_with == ("x/report.pdf", "y/report.pdf")

    loose, _ = _resolve(tmp_path / "b", ("Report 2024.pdf", "report_2024.pdf"), "REPORT-2024")
    assert loose.status == "ambiguous"

    copies, _ = _resolve(
        tmp_path / "c",
        ("x/report.pdf", "y/report.pdf"),
        "report",
        same=("x/report.pdf", "y/report.pdf"),
    )
    assert (copies.status, copies.pdf) == ("matched", "x/report.pdf")


def test_a_sha_prefix_reads_bytes_only_when_it_looks_hexadecimal_and_no_name_matched(
    tmp_path: Path,
) -> None:
    folder, pdfs = _folder(tmp_path, "a.pdf", "b.pdf")
    read, digest = _counting_digest()
    resolve = make_resolver(pdfs, folder, digest)
    sha = hashlib.sha256((folder / "b.pdf").read_bytes()).hexdigest()

    assert resolve("a").status == "matched" and read == []
    prefixed = resolve(sha[:12].upper())
    assert (prefixed.status, prefixed.rule, prefixed.pdf) == ("matched", "sha_prefix", "b.pdf")
    assert sorted(read) == pdfs


def test_an_alias_wins_and_names_its_rule(tmp_path: Path) -> None:
    resolution, _ = _resolve(
        tmp_path,
        ("Meridian Interim Report 2024.pdf", "Orion.pdf"),
        "Meridian 1H24",
        aliases={"meridian_1h24": "Meridian Interim Report 2024"},
    )
    assert (resolution.status, resolution.rule, resolution.pdf) == (
        "matched",
        "alias",
        "Meridian Interim Report 2024.pdf",
    )
    assert resolution.reference == "Meridian 1H24"


@pytest.mark.parametrize("value", ["Atlas.pdf", "report"])
def test_an_alias_naming_no_pdf_or_several_is_refused_naming_the_entry(
    tmp_path: Path, value: str
) -> None:
    folder, pdfs = _folder(tmp_path, "x/report.pdf", "y/report.pdf", "Orion.pdf")
    _, digest = _counting_digest()
    with pytest.raises(QuestionDocsError, match=f"'my doc': '{value}'"):
        make_resolver(pdfs, folder, digest, {"my doc": value})


def test_the_check_lists_matches_misses_and_questions_without_doc_with_the_ways_out(
    tmp_path: Path,
) -> None:
    folder, pdfs = _folder(tmp_path, "Meridian 2024.pdf", "Orion.pdf")
    _, digest = _counting_digest()
    check = check_references(
        {"meridian_2024": ["q1", "q3"], "Meridian 2023": ["q2"]},
        questions_without_doc=["q4"],
        pdf_count=len(pdfs),
        folder=folder,
        resolve=make_resolver(pdfs, folder, digest),
    )

    assert check.rule_counts == {"normalized": 1}
    assert [item.reference for item in check.unresolved] == ["Meridian 2023"]
    assert check.resolutions[0].question_ids == ("q1", "q3")
    assert not check.complete and check.matched_pdfs() == {"Meridian 2024.pdf"}
    table = describe(check)
    assert "'meridian_2024' → Meridian 2024.pdf  [normalized]" in table
    assert "'Meridian 2023': 文件夹里找不到" in table and "Meridian 2024.pdf (0." in table
    # A miss is only a missing label (ADR 0032): the question is still answered across PDFs.
    assert "检索本身在所有已入库 PDF 中进行" in table
    assert "将改为在所有已入库 PDF 中跨文档检索作答" in table
    message = unresolved_message(check)
    assert message is not None
    for part in ("DOC_ALIASES", "ONLY_QUESTION_DOCS", "ON_UNMATCHED_DOCS", "q4", "q2"):
        assert part in message
