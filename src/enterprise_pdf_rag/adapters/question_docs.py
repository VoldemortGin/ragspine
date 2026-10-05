"""Which PDF of a folder a question set's ``doc`` names — one rule for ingest and for answers.

``run_folder_pipeline`` resolves every reference once, before any ingest, model call or
write, and both the ``only_question_docs`` selection and the answer routing read that one
resolution, so a question whose PDF was ingested is exactly a question that routes to it
(docs/enterprise-pdf-rag/adr/0022-run-folder-question-docs-budget-and-progress.md).

Rules, first hit wins: ``alias`` (an explicit ``doc_aliases`` entry), ``exact`` (file name,
any case), ``stem`` (file name without extension, any case), ``sha_prefix`` (≥ 12 hex
characters of the content sha256 — the only rule that reads PDF bytes, and only for a
reference that looks hexadecimal and named nothing by its name), ``normalized`` (NFKC,
casefold, basename of a path, no ``.pdf``, runs of blanks / ``_`` / ``-`` / ``.`` as one
separator). A rule hitting several PDFs of different content is ambiguous, never a guess.
Near misses (a few letters apart) never match: reports differing only in a year or in
"interim / annual" are exactly that close, and the wrong one would be cited. They are only
offered as ``candidates`` for the user to put in ``doc_aliases``.
"""

import difflib
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel

type MatchRule = Literal["alias", "exact", "stem", "sha_prefix", "normalized"]
type ResolutionStatus = Literal["matched", "unmatched", "ambiguous"]
type QuestionSelectionMode = Literal["first", "first_matched"]
type Resolver = Callable[[str], "DocResolution"]

SHA_PREFIX = re.compile(r"[0-9a-f]{12,64}")
_SEPARATORS = re.compile(r"[\s_\-.]+")
_MAX_CANDIDATES = 3
_CANDIDATE_CUTOFF = 0.6
_QUESTION_IDS_SHOWN = 5


class DocCandidate(BoundaryModel):
    """A PDF whose normalized name is close to an unmatched reference; never used by itself."""

    pdf: str
    similarity: float


class DocResolution(BoundaryModel):
    reference: str
    status: ResolutionStatus
    rule: MatchRule | None = None
    # Folder-relative POSIX path of the matched PDF (the first, when several share content).
    pdf: str | None = None
    question_count: int = 0
    question_ids: tuple[str, ...] = ()
    # The PDFs an ambiguous reference hit.
    ambiguous_with: tuple[str, ...] = ()
    candidates: tuple[DocCandidate, ...] = ()


class SkippedQuestion(BoundaryModel):
    """A question ``first_matched`` passed over; its id and references, never its text."""

    question_id: str
    docs: tuple[str, ...]
    reason: str


class SelectedQuestion(BoundaryModel):
    question_id: str
    docs: tuple[str, ...]
    pdf: str
    rules: tuple[MatchRule, ...]


class QuestionSelection(BoundaryModel):
    """Which questions this run asks. ``first_matched`` takes, in set order, the first
    ``max_questions`` whose references name exactly one PDF of the folder — the PDF is in
    the folder; that says nothing about whether it holds the answer."""

    mode: QuestionSelectionMode
    max_questions: int | None
    selected: tuple[SelectedQuestion, ...] = ()
    # Passed over before the selection was full (``first_matched`` only).
    skipped: tuple[SkippedQuestion, ...] = ()
    # The whole set was scanned and still fewer than ``max_questions`` qualified.
    short: bool = False


class QuestionDocsCheck(BoundaryModel):
    """Every reference of the questions this run asks, resolved against one folder's PDFs."""

    folder: str
    pdf_count: int
    resolutions: tuple[DocResolution, ...]
    # Light questions without any ``doc``: they cannot narrow ingestion.
    questions_without_doc: tuple[str, ...] = ()
    selection: QuestionSelection | None = None

    @property
    def rule_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.resolutions:
            if item.rule is not None:
                counts[item.rule] = counts.get(item.rule, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def unresolved(self) -> tuple[DocResolution, ...]:
        return tuple(item for item in self.resolutions if item.status != "matched")

    @property
    def complete(self) -> bool:
        """Every question names its PDF and every reference names exactly one."""
        return not self.unresolved and not self.questions_without_doc

    def resolution(self, reference: str) -> DocResolution | None:
        for item in self.resolutions:
            if item.reference == reference:
                return item
        return None

    def matched_pdfs(self) -> frozenset[str]:
        return frozenset(item.pdf for item in self.resolutions if item.pdf is not None)


class QuestionDocsError(ValueError):
    """``only_question_docs`` cannot tell which PDFs the questions need; raised before any work."""


def normalize_doc_name(text: str) -> str:
    """NFKC, the basename of a path, casefold, no ``.pdf``, separator runs as one blank."""
    name = re.split(r"[\\/]", unicodedata.normalize("NFKC", text).strip())[-1].casefold()
    name = name.removesuffix(".pdf")
    return _SEPARATORS.sub(" ", name).strip()


def _hits(
    reference: str, pdfs: Sequence[Path], digest: Callable[[Path], str]
) -> tuple[MatchRule, list[Path]] | None:
    folded = reference.strip().casefold()
    exact = [pdf for pdf in pdfs if pdf.name.casefold() == folded]
    if exact:
        return "exact", exact
    stem = [pdf for pdf in pdfs if pdf.stem.casefold() == folded]
    if stem:
        return "stem", stem
    if SHA_PREFIX.fullmatch(folded):
        prefixed = [pdf for pdf in pdfs if digest(pdf).startswith(folded)]
        if prefixed:
            return "sha_prefix", prefixed
    normalized = normalize_doc_name(reference)
    if normalized:
        loose = [pdf for pdf in pdfs if normalize_doc_name(pdf.name) == normalized]
        if loose:
            return "normalized", loose
    return None


def _candidates(reference: str, pdfs: Sequence[Path], folder: Path) -> tuple[DocCandidate, ...]:
    target = normalize_doc_name(reference)
    names = {normalize_doc_name(pdf.name): pdf for pdf in pdfs}
    close = difflib.get_close_matches(
        target, list(names), n=_MAX_CANDIDATES, cutoff=_CANDIDATE_CUTOFF
    )
    return tuple(
        DocCandidate(
            pdf=names[name].relative_to(folder).as_posix(),
            similarity=round(difflib.SequenceMatcher(None, target, name).ratio(), 2),
        )
        for name in close
    )


def _settle(
    reference: str,
    rule: MatchRule,
    found: list[Path],
    folder: Path,
    digest: Callable[[Path], str],
) -> DocResolution:
    """Several hits are one document only when they hold the same bytes."""
    if len(found) > 1 and len({digest(pdf) for pdf in found}) > 1:
        return DocResolution(
            reference=reference,
            status="ambiguous",
            rule=None,
            ambiguous_with=tuple(pdf.relative_to(folder).as_posix() for pdf in found),
        )
    return DocResolution(
        reference=reference,
        status="matched",
        rule=rule,
        pdf=found[0].relative_to(folder).as_posix(),
    )


def resolve_aliases(
    aliases: Mapping[str, str] | None,
    pdfs: Sequence[Path],
    folder: Path,
    digest: Callable[[Path], str],
) -> dict[str, DocResolution]:
    """normalized alias key → the one PDF its value names; an entry naming none or several
    is refused before any work, naming that entry."""
    resolved: dict[str, DocResolution] = {}
    for key, value in (aliases or {}).items():
        hit = _hits(value, pdfs, digest)
        settled = None if hit is None else _settle(value, hit[0], hit[1], folder, digest)
        if settled is None or settled.status != "matched":
            problem = (
                "文件夹里没有这份 PDF"
                if settled is None
                else f"它同时命中多份内容不同的 PDF: {list(settled.ambiguous_with)}"
            )
            raise QuestionDocsError(
                f"DOC_ALIASES / doc_aliases 的条目 {key!r}: {value!r} 无效: {problem}。"
                "值必须是本次 PDF 目录里恰好一份 PDF 的文件名、stem 或 sha256 前缀"
                f"({folder})。"
            )
        resolved[normalize_doc_name(key)] = settled
    return resolved


def make_resolver(
    pdfs: Sequence[Path],
    folder: Path,
    digest: Callable[[Path], str],
    aliases: Mapping[str, str] | None = None,
) -> Resolver:
    """One memoized reference → PDF resolution; refuses an invalid alias immediately."""
    resolved_aliases = resolve_aliases(aliases, pdfs, folder, digest)
    memo: dict[str, DocResolution] = {}

    def resolve(reference: str) -> DocResolution:
        if reference not in memo:
            alias = resolved_aliases.get(normalize_doc_name(reference))
            if alias is not None:
                memo[reference] = alias.model_copy(update={"reference": reference, "rule": "alias"})
            else:
                hit = _hits(reference, pdfs, digest)
                memo[reference] = (
                    DocResolution(
                        reference=reference,
                        status="unmatched",
                        candidates=_candidates(reference, pdfs, folder),
                    )
                    if hit is None
                    else _settle(reference, hit[0], hit[1], folder, digest)
                )
        return memo[reference]

    return resolve


def skip_reason(docs: Sequence[str], resolve: Resolver) -> str | None:
    """Why these references do not name exactly one PDF; None when they do."""
    if not docs:
        return "没有写 doc"
    reasons = []
    for doc in docs:
        item = resolve(doc)
        if item.status == "ambiguous":
            reasons.append(f"{doc!r} 歧义, 命中多份 PDF {list(item.ambiguous_with)}")
        elif item.status == "unmatched":
            close = ", ".join(f"{c.pdf} ({c.similarity:.2f})" for c in item.candidates)
            reasons.append(
                f"{doc!r} 在文件夹里找不到"
                + (f"; 最接近的候选(不会自动采用): {close}" if close else "")
            )
    if reasons:
        return "; ".join(reasons)
    if len({resolve(doc).pdf for doc in docs}) > 1:
        return f"doc {list(docs)} 指向不止一份 PDF"
    return None


def check_references(
    references: Mapping[str, Sequence[str]],
    *,
    questions_without_doc: Sequence[str],
    pdf_count: int,
    folder: Path,
    resolve: Resolver,
    selection: QuestionSelection | None = None,
) -> QuestionDocsCheck:
    """``references`` maps each distinct reference, in set order, to the questions using it."""
    return QuestionDocsCheck(
        folder=str(folder),
        pdf_count=pdf_count,
        resolutions=tuple(
            resolve(reference).model_copy(
                update={
                    "question_count": len(ids),
                    "question_ids": tuple(ids[:_QUESTION_IDS_SHOWN]),
                }
            )
            for reference, ids in references.items()
        ),
        questions_without_doc=tuple(questions_without_doc),
        selection=selection,
    )


def describe(check: QuestionDocsCheck) -> str:
    """The check as a short table a notebook prints: reference → PDF (rule), misses marked."""
    lines = [
        f"题目文档核对: 引用 {len(check.resolutions)} 份, 已匹配 "
        f"{len(check.resolutions) - len(check.unresolved)} 份, 未匹配 / 歧义 "
        f"{len(check.unresolved)} 份; 文件夹里共 {check.pdf_count} 份 PDF; 规则命中 "
        f"{check.rule_counts}"
    ]
    for item in check.resolutions:
        if item.status == "matched":
            lines.append(f"  ✓ {item.reference!r} → {item.pdf}  [{item.rule}]")
        else:
            lines.append(f"  ✗ {_problem(item)}")
    if check.questions_without_doc:
        lines.append(
            f"  ! {len(check.questions_without_doc)} 道题没有写 doc: "
            f"{', '.join(check.questions_without_doc[:_QUESTION_IDS_SHOWN])}"
        )
    selection = check.selection
    if selection is not None and selection.mode == "first_matched":
        lines.append(
            f"题目选取 first_matched: 按题集顺序取前 {selection.max_questions} 道「引用的 PDF "
            f"在文件夹里」的题(只保证文档在, 不保证答得出), 选中 {len(selection.selected)} 道, "
            f"选满之前跳过 {len(selection.skipped)} 道"
        )
        lines += [
            f"  选中 {item.question_id}: {list(item.docs)} → {item.pdf}  [{', '.join(item.rules)}]"
            for item in selection.selected
        ]
        lines += [
            f"  跳过 {item.question_id}: {list(item.docs)} — {item.reason}"
            for item in selection.skipped
        ]
        if selection.short:
            lines.append(
                f"  ! 题集中只有 {len(selection.selected)} 道题的文档在文件夹里"
                f"(要求 {selection.max_questions} 道), 选中多少跑多少"
            )
    return "\n".join(lines)


def _problem(item: DocResolution) -> str:
    asked = f"{item.question_count} 道题引用({', '.join(item.question_ids)})"
    if item.status == "ambiguous":
        return (
            f"{item.reference!r}: 歧义, 同时命中多份内容不同的 PDF {list(item.ambiguous_with)}; "
            f"{asked}"
        )
    hint = (
        "最接近的候选(仅供参考, 不会自动采用): "
        + ", ".join(f"{c.pdf} ({c.similarity:.2f})" for c in item.candidates)
        if item.candidates
        else "文件夹里没有名字相近的 PDF"
    )
    return f"{item.reference!r}: 文件夹里找不到; {asked}; {hint}"


def unresolved_message(check: QuestionDocsCheck) -> str | None:
    """Why ``only_question_docs`` cannot start, with the three ways out; None when it can."""
    if check.complete:
        return None
    lines = ["只入库题目涉及的 PDF(ONLY_QUESTION_DOCS / only_question_docs)无法开始:"]
    lines += [f"- {_problem(item)}" for item in check.unresolved]
    if check.questions_without_doc:
        lines.append(
            f"- {len(check.questions_without_doc)} 道题没有写 doc, 无法确定它们需要哪份 PDF: "
            f"{', '.join(check.questions_without_doc[:_QUESTION_IDS_SHOWN])}"
        )
    lines += [
        "处理办法(任选其一):",
        "  1. 在 DOC_ALIASES(doc_aliases)里写明对应关系, 例如 {'题集里的写法': '实际文件名.pdf'};",
        "  2. 把 ONLY_QUESTION_DOCS 设为 False, 入库整个文件夹;",
        '  3. 把 ON_UNMATCHED_DOCS 设为 "skip"(on_unmatched_docs="skip"), 只入库已匹配的 PDF, '
        "未匹配的题目在结果里记为 routing_failed 并写明原因。",
    ]
    return "\n".join(lines)
