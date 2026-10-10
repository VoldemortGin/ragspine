"""Document tags (ADR 0049): sidecar / path-template sources, the root record, the filter."""

import json
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters.document_catalog import (
    CatalogEntry,
    DocumentCatalog,
    filter_catalog,
)
from enterprise_pdf_rag.adapters.document_tags import (
    SIDECAR_FILE,
    TAGS_RECORD,
    load_document_tags,
    parse_document_filter,
    resolve_document_tags,
    save_document_tags,
    tags_match,
    template_tags,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _touch(folder: Path, *relative: str) -> tuple[Path, ...]:
    paths = []
    for item in relative:
        path = folder / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 " + item.encode())
        paths.append(path)
    return tuple(paths)


def _sidecar(folder: Path, text: str) -> None:
    (folder / SIDECAR_FILE).write_text(text, encoding="utf-8")


# ---- path template ----------------------------------------------------------------------------


def test_template_reads_one_tag_per_directory_level() -> None:
    template = "{region}/{year}/{category}/{file}"
    assert template_tags(template, "HK/2024/annual/report.pdf") == {
        "region": "HK",
        "year": "2024",
        "category": "annual",
    }


def test_template_literals_must_match_and_depth_must_agree() -> None:
    template = "reports/FY{year}/{file}"
    assert template_tags(template, "reports/FY2025/x.pdf") == {"year": "2025"}
    assert template_tags(template, "decks/FY2025/x.pdf") is None
    assert template_tags(template, "reports/FY2025/extra/x.pdf") is None
    assert template_tags(template, "x.pdf") is None


@pytest.mark.parametrize(
    "template",
    ["{region}/{year}", "{region}/{region}/{file}", "{1x}/{file}", "{region/{file}", "{}/{file}"],
)
def test_a_malformed_template_is_refused(template: str) -> None:
    with pytest.raises(ValueError, match="APP_DOCUMENT_TAG_PATH_TEMPLATE"):
        template_tags(template, "a/b.pdf")


# ---- sidecar + template resolution ------------------------------------------------------------


def test_nothing_configured_tags_nothing(tmp_path: Path) -> None:
    pdfs = _touch(tmp_path, "a.pdf", "HK/2024/b.pdf")
    resolution = resolve_document_tags(tmp_path, pdfs, template="")
    assert resolution.configured is False
    assert resolution.tags == {}


def test_sidecar_columns_are_free_string_tags_by_name_or_relative_path(tmp_path: Path) -> None:
    a, b, c = _touch(tmp_path, "a.pdf", "HK/2024/b.pdf", "c.pdf")
    _sidecar(
        tmp_path,
        "﻿file,year,region,category\n"
        "a.pdf,2024,HK,annual\n"
        "HK/2024/b.pdf, 2023 ,,\n"
        "missing.pdf,2020,SG,x\n",
    )
    resolution = resolve_document_tags(tmp_path, (a, b, c), template="")
    assert resolution.configured is True
    assert resolution.tags == {
        a: {"year": "2024", "region": "HK", "category": "annual"},
        b: {"year": "2023"},
    }
    assert resolution.counts["sidecar_rows"] == 3
    assert resolution.counts["sidecar_unmatched"] == 1
    assert resolution.counts["untagged"] == 1


def test_a_bare_name_shared_by_two_pdfs_tags_neither(tmp_path: Path) -> None:
    first, second = _touch(tmp_path, "HK/report.pdf", "SG/report.pdf")
    _sidecar(tmp_path, "file,year\nreport.pdf,2024\nSG/report.pdf,2025\n")
    resolution = resolve_document_tags(tmp_path, (first, second), template="")
    assert resolution.tags == {second: {"year": "2025"}}
    assert resolution.counts["sidecar_ambiguous"] == 1


def test_sidecar_wins_over_the_template_key_by_key(tmp_path: Path) -> None:
    a, b = _touch(tmp_path, "HK/2024/a.pdf", "flat.pdf")
    _sidecar(tmp_path, "file,year,category\nHK/2024/a.pdf,2025,interim\n")
    resolution = resolve_document_tags(tmp_path, (a, b), template="{region}/{year}/{file}")
    assert resolution.tags == {a: {"region": "HK", "year": "2025", "category": "interim"}}
    # The flat PDF matches no template: empty tags, never an error; only counted.
    assert resolution.counts["template_unmatched"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "name,year\na.pdf,2024\n",
        "file,year,year\na.pdf,2024,2025\n",
        "file,year\na.pdf,2024\na.pdf,2025\n",
    ],
)
def test_a_malformed_sidecar_is_refused(tmp_path: Path, text: str) -> None:
    (a,) = _touch(tmp_path, "a.pdf")
    _sidecar(tmp_path, text)
    with pytest.raises(ValueError, match=SIDECAR_FILE):
        resolve_document_tags(tmp_path, (a,), template="")


# ---- the root record --------------------------------------------------------------------------


def test_the_record_is_written_only_when_something_changes(tmp_path: Path) -> None:
    root = tmp_path / "ingestion"
    assert save_document_tags(root, {_SHA_A: {}}) is False
    assert not (root / TAGS_RECORD).exists()
    assert load_document_tags(root) == {}

    assert save_document_tags(root, {_SHA_A: {"year": "2024"}, _SHA_B: {"region": "HK"}})
    assert load_document_tags(root) == {_SHA_A: {"year": "2024"}, _SHA_B: {"region": "HK"}}
    stamp = (root / TAGS_RECORD).stat().st_mtime_ns
    assert save_document_tags(root, {_SHA_A: {"year": "2024"}}) is False
    assert (root / TAGS_RECORD).stat().st_mtime_ns == stamp

    # Another run re-tags A and clears B; documents it does not name keep theirs.
    assert save_document_tags(root, {_SHA_B: {}})
    assert load_document_tags(root) == {_SHA_A: {"year": "2024"}}
    record = json.loads((root / TAGS_RECORD).read_text(encoding="utf-8"))
    assert record == {"format": "document-tags-v1", "documents": {_SHA_A: {"year": "2024"}}}


def test_an_unreadable_record_is_refused(tmp_path: Path) -> None:
    (tmp_path / TAGS_RECORD).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=TAGS_RECORD):
        load_document_tags(tmp_path)


# ---- the filter -------------------------------------------------------------------------------


def test_filter_parses_json_or_a_mapping_and_empty_means_none() -> None:
    assert parse_document_filter(None) is None
    assert parse_document_filter("") is None
    assert parse_document_filter("  ") is None
    assert parse_document_filter("{}") is None
    assert parse_document_filter({}) is None
    assert parse_document_filter('{"year": ["2024", "2023"], "region": "HK"}') == {
        "year": frozenset({"2024", "2023"}),
        "region": frozenset({"HK"}),
    }
    assert parse_document_filter({"year": {"2024"}}) == {"year": frozenset({"2024"})}


@pytest.mark.parametrize(
    "value", ["[1]", '{"year": 2024}', '{"year": []}', '{"": "x"}', "{oops", '{"y": [null]}']
)
def test_a_malformed_filter_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="document filter"):
        parse_document_filter(value)


def test_filter_values_or_within_a_key_and_keys_and_together() -> None:
    document_filter = parse_document_filter({"year": ["2024", "2023"], "region": "HK"})
    assert tags_match({"year": "2024", "region": "HK", "category": "x"}, document_filter)
    assert tags_match({"year": "2023", "region": "HK"}, document_filter)
    assert not tags_match({"year": "2022", "region": "HK"}, document_filter)
    assert not tags_match({"year": "2024", "region": "SG"}, document_filter)
    assert not tags_match({"year": "2024"}, document_filter)  # a missing key never matches
    assert not tags_match({}, document_filter)
    assert tags_match({}, None)  # no filter keeps every document, tagged or not


def _entry(document_id: str, tags: dict[str, str]) -> CatalogEntry:
    return CatalogEntry(
        document_id=document_id,
        origin="ingestion",
        source_store="s",
        processing_store="p",
        retrieval_status="ready",
        tags=tags,
    )


def test_filter_catalog_keeps_the_matching_documents_in_order() -> None:
    catalog = DocumentCatalog(
        ingestion_root="r",
        legacy_roots=(),
        documents=(_entry(_SHA_A, {"year": "2024"}), _entry(_SHA_B, {"year": "2023"})),
        unpublished=(),
    )
    assert filter_catalog(catalog, None) is catalog
    kept = filter_catalog(catalog, parse_document_filter({"year": "2023"}))
    assert [entry.document_id for entry in kept.documents] == [_SHA_B]
    assert kept.unpublished == catalog.unpublished
