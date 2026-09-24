"""通用 retrieval-only 评测的纯逻辑：题集加载、页/内容判定、recall@k / page_recall@k / MRR、单题检索。

只跑检索、不调生成 LLM；编排（resume / 并发 / 落盘）在 ``ragspine.cli.batch``。

判定口径与 ``nl_gold_ragspine`` 对齐，只 import 它的公开函数（``load_nl_gold`` / ``normalize_answer`` /
``contains_normalized`` / ``RECALL_KS``），不改它：

- 有 page group 时按页判定：命中 = ``page ∈ group``；题目给了 ``doc`` 还要求块的 ``doc_id``（或去扩展名后）
  与之匹配（casefold）。页码经 ``retrieval.page_parent.pages.page_key`` 从 locator 解析；没页码的块不参与
  页级排名（与 nl_gold 的 ``_page_of`` 过滤一致）。
- 没 page group 时回退到 ``expected`` 与命中块 ``text`` / ``prompt_text`` 比对：含数字则每个数字都须出现，
  否则整串须出现（规范化同 nl_gold：NFKC、千分位、``%``、数字边界）。
- rank = 各 group 首次命中位置的最大值（所有 group 都进前 k），任一 group 未命中即 None；
  ``gold_rank`` 逐条照抄 nl_gold 的 ``_gold_rank`` 语义，靠对等测试钉住（nl_gold 解禁前的双实现）。

检索器按 ``NarrativeRetriever`` 协议调用，**不带** ask 的 entity/period 意图过滤，排序可能与 ask 不同。
命中块文本只进评测产物（results.jsonl / summary.md），本模块不发 observability trace。
"""

import csv
import io
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ragspine.agent.agent import NarrativeRetriever
from ragspine.eval.nl_gold_ragspine import (
    GOLD_SCHEMA_VERSIONS,
    RECALL_KS,
    contains_normalized,
    load_nl_gold,
    normalize_answer,
)
from ragspine.retrieval.page_parent.pages import page_key

PageHit = tuple[str, int]
Basis = Literal["page", "content", "none"]

_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
# 数字片段按原文取（千分位 / 全角经 NFKC），每段再交给 nl_gold 的 contains_normalized 规范化比对。
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%?")
_KNOWN_KEYS = frozenset({"id", "question", "expected", "pages", "page", "doc"})


class QuestionSetError(ValueError):
    """题集文件无法解析成合法题目（格式 / 重复 id / 非法页码 / 空题集）。"""


def parse_pages(value: object) -> frozenset[int]:
    """页码（1 起）解析：``12``、``[12, 13]``、``"12, 14-15"``；空值为空集，非法值抛 ValueError。"""
    if value is None or value == "":
        return frozenset()
    if isinstance(value, bool):
        raise ValueError(f"非法页码 {value!r}")
    if isinstance(value, int):
        if value < 1:
            raise ValueError(f"页码须 >= 1：{value}")
        return frozenset({value})
    if isinstance(value, str):
        pages: set[int] = set()
        for part in (p.strip() for p in value.split(",")):
            if not part:
                continue
            if part.isdigit():
                pages |= parse_pages(int(part))
                continue
            match = _RANGE_RE.match(part)
            start, end = (int(match.group(1)), int(match.group(2))) if match else (0, -1)
            if start < 1 or start > end:
                raise ValueError(f"非法页码 {part!r}")
            pages.update(range(start, end + 1))
        return frozenset(pages)
    if isinstance(value, (list, tuple, set, frozenset)):
        merged: set[int] = set()
        for item in value:
            merged |= parse_pages(item)
        return frozenset(merged)
    raise ValueError(f"非法页码 {value!r}")


class BatchQuestion(BaseModel):
    """一道题：各格式的 loader 先归一成这组字段。``page_groups`` 每组任一页命中即可，所有组都须命中。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    question: str = Field(min_length=1)
    expected: str | None = None
    page_groups: tuple[frozenset[int], ...] = ()
    doc: tuple[str, ...] = ()
    extra: dict[str, str] = Field(default_factory=dict)

    @field_validator("page_groups", mode="before")
    @classmethod
    def _parse_groups(cls, value: object) -> tuple[frozenset[int], ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("page_groups 须为数组")
        groups = tuple(parse_pages(item) for item in value)
        return tuple(group for group in groups if group)

    @field_validator("doc", mode="before")
    @classmethod
    def _parse_doc(cls, value: object) -> object:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return (value,)
        return value


# ---------------------------------------------------------------------------
# 题集加载
# ---------------------------------------------------------------------------


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False)


def _question_from_record(record: Mapping[str, Any], index: int) -> BatchQuestion:
    raw_expected = record.get("expected")
    docs: list[str] = []
    raw_doc = record.get("doc")
    if isinstance(raw_doc, str) and raw_doc:
        docs.append(raw_doc)
    elif isinstance(raw_doc, list):
        docs.extend(str(d) for d in raw_doc if d)
    expected: str | None
    if isinstance(raw_expected, Mapping):
        # data/golden/qa_golden_set.jsonl：expected 是对象，value → expected，source.doc → doc。
        value = raw_expected.get("value")
        expected = None if value is None else _as_text(value)
        source = raw_expected.get("source")
        if isinstance(source, Mapping) and source.get("doc"):
            docs.append(str(source["doc"]))
    elif raw_expected is None or raw_expected == "":
        expected = None
    else:
        expected = _as_text(raw_expected)
    pages = record.get("pages", record.get("page"))
    raw_id = record.get("id")
    return BatchQuestion(
        id=_as_text(raw_id) if raw_id not in (None, "") else f"q{index}",
        question=str(record.get("question") or "").strip(),
        expected=expected,
        page_groups=(parse_pages(pages),),
        doc=tuple(docs),
        extra={
            str(key): _as_text(value)
            for key, value in record.items()
            if key not in _KNOWN_KEYS and value is not None
        },
    )


def _records(path: Path, text: str) -> list[Mapping[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(text)
        if isinstance(payload, Mapping):
            payload = payload.get("questions")
        if not isinstance(payload, list):
            raise QuestionSetError(f"{path}: .json 须为题目数组或含 questions 数组的对象")
        rows = payload
    elif suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif suffix == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
    elif suffix == ".txt":
        lines = (line.strip() for line in text.splitlines())
        rows = [{"question": line} for line in lines if line and not line.startswith("#")]
    else:
        raise QuestionSetError(f"{path}: 不支持的题集格式 {suffix or '(无后缀)'}")
    if not all(isinstance(row, Mapping) for row in rows):
        raise QuestionSetError(f"{path}: 每道题须为对象")
    return rows


def _nl_gold_questions(path: Path) -> list[BatchQuestion]:
    """nl-answers-gold v1 / v2（``GOLD_SCHEMA_VERSIONS``）：每个 (case, 语言) 一题；筛选与 nl_gold ``recall_at_k`` 的 eligible 相同。"""
    questions: list[BatchQuestion] = []
    for case in load_nl_gold(path):
        if case.skip_reason:
            continue
        groups = (
            ()
            if case.expect_abstain
            else tuple(frozenset(a.page_index + 1 for a in group) for group in case.required_claims)
        )
        for language, text in case.questions:
            questions.append(
                BatchQuestion(
                    id=f"{case.case_id}:{language}",
                    question=text,
                    page_groups=groups,
                    extra={"case_class": case.case_class, "language": language},
                )
            )
    return questions


def load_questions(path: str | Path) -> tuple[BatchQuestion, ...]:
    """按后缀读题集（.json / .jsonl / .csv / .txt；nl-answers-gold v1 / v2 的 .json 自动识别）。"""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
        payload = json.loads(text) if source.suffix.lower() == ".json" else None
        if isinstance(payload, Mapping) and payload.get("schema_version") in GOLD_SCHEMA_VERSIONS:
            questions = _nl_gold_questions(source)
        else:
            questions = [
                _question_from_record(record, index)
                for index, record in enumerate(_records(source, text), start=1)
            ]
    except QuestionSetError:
        raise
    except (OSError, ValueError, ValidationError) as exc:
        raise QuestionSetError(f"{source}: {exc}") from exc
    if not questions:
        raise QuestionSetError(f"{source}: 题集为空")
    ids = [q.id for q in questions]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise QuestionSetError(f"{source}: 题目 id 重复：{', '.join(duplicates)}")
    return tuple(questions)


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def hit_page(hit: Mapping[str, object]) -> PageHit | None:
    """命中块 / 来源的 (doc_id, page)；兼容检索 snippet 与 ask 的 sources 两种键名。"""
    doc = hit.get("doc_id") or hit.get("doc") or ""
    locator = hit.get("source_locator") or hit.get("locator") or ""
    return page_key(SimpleNamespace(doc_id=str(doc), source_locator=str(locator)))


def doc_matches(doc_id: str, docs: Sequence[str]) -> bool:
    """``doc_id`` 或其去扩展名部分与题目给的任一 doc 相同（casefold）；没给 doc 恒为真。"""
    if not docs:
        return True
    names = {doc_id.casefold(), Path(doc_id).stem.casefold()}
    return any(d.casefold() in names for d in docs)


def gold_rank(
    hits: Sequence[PageHit],
    groups: Sequence[frozenset[int]],
    *,
    docs: Sequence[str] = (),
    distinct: bool = False,
) -> int | None:
    """所有 group 都已在前 k 个命中内出现的最小 k；凑不齐（或没有 group）为 None。

    ``hits`` 只含带页码的命中（按检索顺序）。``distinct=True`` 按不同页计名次，同一页只算第一次出现；
    去重键在给了 ``docs`` 时是 (doc, page)，否则是 page（与 nl_gold ``_gold_rank`` 同义）。
    """
    if not groups:
        return None
    ranked: list[PageHit] = list(hits)
    if distinct:
        seen: set[object] = set()
        ranked = []
        for doc_id, page in hits:
            key: object = (doc_id, page) if docs else page
            if key not in seen:
                seen.add(key)
                ranked.append((doc_id, page))
    worst = 0
    for group in groups:
        rank = next(
            (
                i + 1
                for i, (doc_id, page) in enumerate(ranked)
                if page in group and doc_matches(doc_id, docs)
            ),
            None,
        )
        if rank is None:
            return None
        worst = max(worst, rank)
    return worst


def content_hit(text: str, expected: str) -> bool:
    """``expected`` 是否出现在 ``text`` 中：整串出现即命中；否则含数字时每个数字都须出现。

    规范化与匹配全部委托 nl_gold 的 ``normalize_answer`` / ``contains_normalized``（口径随它走）。
    """
    haystack = normalize_answer(text)
    if contains_normalized(haystack, expected):
        return True
    numbers = _NUMBER_RE.findall(unicodedata.normalize("NFKC", expected))
    if numbers:
        return all(contains_normalized(haystack, number) for number in numbers)
    return contains_normalized(haystack, expected)


@dataclass(frozen=True)
class HitJudgement:
    """一题的判定：依据（page / content / none）、块名次、不同页名次、逐条命中标记。"""

    basis: Basis
    rank: int | None
    page_rank: int | None
    hit_flags: tuple[bool, ...]

    @property
    def judged(self) -> bool:
        return self.basis != "none"


def _hit_text(hit: Mapping[str, object]) -> str:
    return f"{hit.get('text') or ''}\n{hit.get('prompt_text') or ''}"


def judge_hits(question: BatchQuestion, hits: Sequence[Mapping[str, object]]) -> HitJudgement:
    """按题目的 page group（优先）或 expected 判定一组按序命中。"""
    keys = [hit_page(hit) for hit in hits]
    if question.page_groups:
        paged = [key for key in keys if key is not None]
        flags = tuple(
            key is not None
            and any(key[1] in group for group in question.page_groups)
            and doc_matches(key[0], question.doc)
            for key in keys
        )
        return HitJudgement(
            basis="page",
            rank=gold_rank(paged, question.page_groups, docs=question.doc),
            page_rank=gold_rank(paged, question.page_groups, docs=question.doc, distinct=True),
            hit_flags=flags,
        )
    if question.expected:
        expected = question.expected
        flags = tuple(
            content_hit(_hit_text(hit), expected)
            and doc_matches(str(hit.get("doc_id") or hit.get("doc") or ""), question.doc)
            for hit in hits
        )
        rank = next((i + 1 for i, flag in enumerate(flags) if flag), None)
        seen: set[object] = set()
        page_rank: int | None = None
        for i, (key, flag) in enumerate(zip(keys, flags, strict=True)):
            dedup: object = key if key is not None else ("#", i)
            if dedup in seen:
                continue
            seen.add(dedup)
            if flag:
                page_rank = len(seen)
                break
        return HitJudgement(basis="content", rank=rank, page_rank=page_rank, hit_flags=flags)
    return HitJudgement(basis="none", rank=None, page_rank=None, hit_flags=(False,) * len(hits))


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def recall_ks(top_k: int) -> tuple[int, ...]:
    """``RECALL_KS`` 中不超过 ``top_k`` 的值，再加上 ``top_k`` 本身。"""
    return tuple(sorted({k for k in RECALL_KS if k <= top_k} | {top_k}))


def _rate(numerator: float, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def retrieval_metrics(
    ranks: Sequence[tuple[int | None, int | None]], ks: Sequence[int]
) -> dict[str, Any]:
    """已判定题目的 (块名次, 不同页名次) → recall@k / page_recall@k / MRR（名次 None 记未命中）。"""

    def at_k(values: list[int | None]) -> dict[str, float]:
        return {
            f"@{k}": _rate(sum(1 for r in values if r is not None and r <= k), len(values))
            for k in ks
        }

    def mrr(values: list[int | None]) -> float:
        return _rate(sum(1 / r for r in values if r is not None), len(values))

    chunk_ranks = [rank for rank, _ in ranks]
    page_ranks = [page_rank for _, page_rank in ranks]
    return {
        "judged": len(ranks),
        "recall": at_k(chunk_ranks),
        "page_recall": at_k(page_ranks),
        "mrr": mrr(chunk_ranks),
        "page_mrr": mrr(page_ranks),
    }


# ---------------------------------------------------------------------------
# 单题检索
# ---------------------------------------------------------------------------


def retrieve_hits(
    retriever: NarrativeRetriever, question: str, *, top_k: int
) -> list[dict[str, object]]:
    """只检索、不带意图过滤（ask 会传 entity/period），截到前 ``top_k`` 条。"""
    return list(retriever.retrieve(question, filters=None, top_k=top_k))[:top_k]
