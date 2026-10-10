"""Logical document tags (ADR 0049): where they come from, where they live, how they filter.

A tag is a free ``name → value`` string pair (``year``, ``region``, ``category`` …) a user puts
on a PDF to organise a folder; no name is built in. Two sources, read only, never written:

- the sidecar ``<PDF folder>/documents.csv`` — a ``file`` column (a path relative to the folder,
  or a bare file name naming exactly one PDF) plus any other columns, each one a tag;
- the path template ``APP_DOCUMENT_TAG_PATH_TEMPLATE`` (e.g. ``{region}/{year}/{file}``) read
  against each PDF's path relative to the folder; a PDF it does not match gets no tags from it.

Both together merge key by key, the sidecar winning. Tags are mutable metadata: they are kept
outside every content address and fingerprint, in one record of the ingestion root
(``document-tags.json``, keyed by source sha256), so re-tagging a PDF never re-ingests it.

A filter is explicit, from the caller only — ``{"year": ["2024"], "region": ["HK"]}``: values
of one key OR together, keys AND together, a document lacking a filtered key never matches.
Nothing here ever reads a question or a question set.
"""

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ragspine.common.evidence.object_backend.files import FileBackend

SIDECAR_FILE: Final = "documents.csv"
TAGS_RECORD: Final = "document-tags.json"
TAGS_FORMAT: Final = "document-tags-v1"
_FILE_COLUMN = "file"
_FILE_PLACEHOLDER = "file"
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TEMPLATE_SETTING = "APP_DOCUMENT_TAG_PATH_TEMPLATE"

type DocumentTags = dict[str, str]
type DocumentFilter = dict[str, frozenset[str]]
type FilterInput = str | Mapping[str, str | Iterable[str]] | None


@dataclass(frozen=True, slots=True)
class TagResolution:
    """Each discovered PDF's tags (only PDFs with at least one) and counts for the trace."""

    configured: bool
    tags: dict[Path, DocumentTags] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def _compile_template(template: str) -> re.Pattern[str]:
    def refuse(why: str) -> ValueError:
        return ValueError(f"{_TEMPLATE_SETTING} {template!r}: {why}")

    pattern: list[str] = []
    names: set[str] = set()
    position = 0
    for match in _PLACEHOLDER.finditer(template):
        literal = template[position : match.start()]
        if "{" in literal or "}" in literal:
            raise refuse("unbalanced braces")
        pattern.append(re.escape(literal))
        name = match.group(1)
        if _NAME.fullmatch(name) is None:
            raise refuse(f"placeholder {{{name}}} is not a name")
        if name in names:
            raise refuse(f"placeholder {{{name}}} appears twice")
        names.add(name)
        pattern.append(f"(?P<{name}>[^/]+)")
        position = match.end()
    tail = template[position:]
    if "{" in tail or "}" in tail:
        raise refuse("unbalanced braces")
    pattern.append(re.escape(tail))
    if _FILE_PLACEHOLDER not in names:
        raise refuse("it must contain {file}")
    return re.compile("".join(pattern))


def template_tags(template: str, relative: str) -> DocumentTags | None:
    """The tags ``template`` reads from a folder-relative posix path; ``None`` when it does not match."""
    match = _compile_template(template.strip().strip("/")).fullmatch(relative)
    if match is None:
        return None
    return {
        name: value.strip()
        for name, value in match.groupdict().items()
        if name != _FILE_PLACEHOLDER and value.strip()
    }


def _sidecar_rows(path: Path) -> list[tuple[str, DocumentTags]]:
    def refuse(why: str) -> ValueError:
        return ValueError(f"{SIDECAR_FILE}: {why}")

    reader = csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig")))
    header = next(reader, None)
    if header is None:
        return []
    columns = [name.strip() for name in header]
    if _FILE_COLUMN not in columns:
        raise refuse(f"no {_FILE_COLUMN!r} column")
    named = [name for name in columns if name]
    if len(set(named)) != len(named):
        raise refuse("a column name appears twice")
    rows: list[tuple[str, DocumentTags]] = []
    for row in reader:
        values = dict(zip(columns, (cell.strip() for cell in row), strict=False))
        reference = values.pop(_FILE_COLUMN, "").replace("\\", "/").removeprefix("./")
        if not reference:
            continue
        rows.append((reference, {name: value for name, value in values.items() if name and value}))
    return rows


def resolve_document_tags(folder: Path, pdfs: Sequence[Path], *, template: str) -> TagResolution:
    """Every PDF's tags from the folder's sidecar and the path template; reads, never writes.

    A malformed sidecar or template is a ``ValueError`` before any work. A sidecar row naming
    no PDF, or a bare name shared by several, tags nothing and is only counted, as is a PDF the
    template does not match. Two rows naming one PDF are a ``ValueError``.
    """
    sidecar = folder / SIDECAR_FILE
    template = template.strip()
    if not sidecar.is_file() and not template:
        return TagResolution(configured=False)
    counts: Counter[str] = Counter()
    relative = {pdf: pdf.relative_to(folder).as_posix() for pdf in pdfs}
    tags: dict[Path, DocumentTags] = {}
    if template:
        for pdf, path in relative.items():
            found = template_tags(template, path)
            if found is None:
                counts["template_unmatched"] += 1
            elif found:
                tags[pdf] = found
    if sidecar.is_file():
        by_path = {path: pdf for pdf, path in relative.items()}
        by_name: dict[str, list[Path]] = {}
        for pdf in pdfs:
            by_name.setdefault(pdf.name, []).append(pdf)
        claimed: set[Path] = set()
        for reference, row in _sidecar_rows(sidecar):
            counts["sidecar_rows"] += 1
            named = by_path.get(reference)
            if named is None and "/" not in reference:
                candidates = by_name.get(reference, [])
                if len(candidates) > 1:
                    counts["sidecar_ambiguous"] += 1
                    continue
                named = candidates[0] if candidates else None
            if named is None:
                counts["sidecar_unmatched"] += 1
                continue
            if named in claimed:
                raise ValueError(f"{SIDECAR_FILE}: two rows name {relative[named]!r}")
            claimed.add(named)
            merged = {**tags.get(named, {}), **row}
            if merged:
                tags[named] = merged
    counts["tagged"] = len(tags)
    counts["untagged"] = len(pdfs) - len(tags)
    return TagResolution(configured=True, tags=tags, counts=dict(counts))


def load_document_tags(root: Path) -> dict[str, DocumentTags]:
    """sha256 → tags from the ingestion root's record; an absent record is ``{}``."""
    payload = FileBackend(root).record(TAGS_RECORD)
    if payload is None:
        return {}
    try:
        record = json.loads(payload)
        documents = record["documents"] if record.get("format") == TAGS_FORMAT else None
        if not isinstance(documents, dict):
            raise ValueError("unknown format")
        return {
            str(sha): {str(name): str(value) for name, value in tags.items()}
            for sha, tags in documents.items()
        }
    except (ValueError, KeyError, AttributeError, TypeError) as error:
        raise ValueError(f"{TAGS_RECORD} under the ingestion root is unreadable") from error


def save_document_tags(root: Path, tags: Mapping[str, Mapping[str, str]]) -> bool:
    """Set (or, given ``{}``, clear) the named documents' tags; others keep theirs.

    The record is replaced atomically, and only when its content changes, so a run that tags
    nothing and finds no record writes nothing. Returns whether it wrote.
    """
    current = load_document_tags(root)
    updated = dict(current)
    for sha, values in tags.items():
        if values:
            updated[sha] = dict(values)
        else:
            updated.pop(sha, None)
    if updated == current:
        return False
    record = {"format": TAGS_FORMAT, "documents": dict(sorted(updated.items()))}
    root.mkdir(parents=True, exist_ok=True)
    FileBackend(root).put_record(
        TAGS_RECORD, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
    )
    return True


def parse_document_filter(value: FilterInput) -> DocumentFilter | None:
    """A caller's filter as ``{name: values}``; ``None`` / blank / ``{}`` is no filter.

    Accepts a JSON object string (the CLI / notebook form) or a mapping; a value is one string
    or a non-empty list of strings. Anything else is a ``ValueError``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"document filter is not JSON: {error.msg}") from None
    if not isinstance(value, Mapping):
        raise ValueError("document filter must be an object of tag name → value(s)")
    parsed: DocumentFilter = {}
    for name, values in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("document filter tag names must be non-empty strings")
        items = [values] if isinstance(values, str) else values
        if not isinstance(items, Iterable):
            raise ValueError(f"document filter {name!r}: values must be strings")
        allowed = list(items)
        if not allowed or not all(isinstance(item, str) for item in allowed):
            raise ValueError(f"document filter {name!r}: give one or more string values")
        parsed[name.strip()] = frozenset(item.strip() for item in allowed)
    return parsed or None


def tags_match(tags: Mapping[str, str], document_filter: DocumentFilter | None) -> bool:
    """Whether a document's tags pass the filter: every key present, each value one allowed."""
    if document_filter is None:
        return True
    return all(tags.get(name) in values for name, values in document_filter.items())
