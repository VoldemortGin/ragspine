"""叙事「（资料来源：…）」后缀按文档合并（ADR 0029）。纯函数，零 LLM，不 import 检索实现。

同一文档的多个片段合成一条：``doc page=6, 10, 14, 18``——页码去重、升序、连续页（≥2 页）合成
``a-b``（ASCII 连字符）；pptx 用 ``slide=``。解析不出页码的 locator 去重后原样附在后面。组内顺序：
page 组、slide 组、原样项（按首次出现）；文档按首次出现排序，文档之间仍用 ``；``。
某文档只有一个不同的 locator 时输出 ``doc locator``，与合并前逐字节相同。

只改展示用后缀；``AgentResult.sources`` 仍逐片段承载完整 locator 血缘。
开关 ``RAGSPINE_CITATION_MERGE=on|off``（默认 on）；off 时后缀与合并前逐字节相同。
"""

import os
import re
from collections.abc import Mapping, Sequence

CITATION_MERGE_ENV = "RAGSPINE_CITATION_MERGE"

_PAGE_RE = re.compile(r"(?:^|@)(page|slide)=(\d+)(?=$|[#,])")
_KINDS = ("page", "slide")


def resolve_citation_merge() -> bool:
    """读 RAGSPINE_CITATION_MERGE（on|off，默认 on）。"""
    spec = (os.environ.get(CITATION_MERGE_ENV) or "on").strip().lower()
    if spec not in ("on", "off"):
        raise ValueError(f"{CITATION_MERGE_ENV} 只能是 on / off，收到 {spec!r}")
    return spec == "on"


def _ranges(numbers: set[int]) -> str:
    """{5, 6, 7, 9} → "5-7, 9"。"""
    parts: list[str] = []
    ordered = sorted(numbers)
    start = prev = ordered[0]
    for n in [*ordered[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def _merge_locators(locators: list[str]) -> str:
    numbers: dict[str, set[int]] = {kind: set() for kind in _KINDS}
    verbatim: list[str] = []
    for loc in locators:
        m = _PAGE_RE.search(loc)
        if m:
            numbers[m.group(1)].add(int(m.group(2)))
        elif loc and loc not in verbatim:
            verbatim.append(loc)
    groups = [f"{kind}={_ranges(numbers[kind])}" for kind in _KINDS if numbers[kind]]
    return ", ".join([*groups, *verbatim])


def merge_citation(missing: Sequence[Mapping[str, object]]) -> str:
    """把待附来源（``{doc, locator}`` 逐片段）拼成后缀正文，按文档合并。"""
    by_doc: dict[str, list[str]] = {}
    for s in missing:
        locators = by_doc.setdefault(f"{s['doc']}", [])
        loc = f"{s['locator']}"
        if loc not in locators:
            locators.append(loc)
    cites = []
    for doc, locators in by_doc.items():
        body = locators[0] if len(locators) == 1 else _merge_locators(locators)
        cites.append(f"{doc} {body}".strip())
    return "；".join(cites)
