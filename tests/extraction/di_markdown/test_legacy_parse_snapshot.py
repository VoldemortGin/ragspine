"""DI markdown 解析旧路径字节不变守护：不含 `<!-- page: N -->` 标记的输入（PageBreak 分页、无任何标记），
`repr(parse_di_markdown(x))` 与冻结快照逐字节一致。

快照摘要取自引入 page 标记模式之前的实现（main@a0fe837）。输入：test_parse.py 的全部样例、
页图测试的 `fixtures.make_md`，以及可选的 71 页真实样本（缺失时 skip）。
"""

import hashlib
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.parse import parse_di_markdown
from tests.ingestion.page_images.fixtures import make_md

_PARSE_SAMPLES = (
    "one\n\n<!-- PageBreak -->\n\ntwo\n<!-- PageBreak -->\nthree\n",
    'a\n\n<!-- PageNumber="7" -->\n\n<!-- PageBreak -->\n\nb\n\n<!-- PageBreak -->\n\n<!-- PageFooter="f" -->\n<!-- PageNumber="Page 9" -->\n',
    'a\n<!-- PageNumber="iv" -->\n<!-- PageBreak -->\nb\n<!-- PageNumber="3 of 10" -->',
    '<!-- PageHeader="Annual Report" -->\n\n# Title\n\nBody text.\n\n<!-- PageFooter="Confidential" -->\n<!-- PageNumber="12" -->\n',
    'line one\n<!-- PageHeader="H" -->\nline two\n',
    "<!-- anything else -->\ntext <!-- inline --> here\n",
    'a\n<!-- PageBreak -->\n\n<!-- PageNumber="2" -->\n<!-- PageBreak -->\n<!-- PageBreak -->\nb',
    "",
    "This is p1.\nStill p1.\n\n\nThis is p2 &amp; more &gt;40%.\n",
    "# Title\n\nintro\n\n## Section A\n\n### Sub A1\n\na1 text\n\n## Section B\n\nb text\n\n#### Deep\n\nd\n\n# Next Title\n\nn\n",
    "# Chapter\n\n## Part\n\n<!-- PageBreak -->\n\ncontinued\n",
    "preface\n\n# T\n",
    "#1 in market\n\n####### seven\n\n## Closed ##\n\n## #hash & more\n\n#\n",
    "para line\n# Head\nafter\n",
    "# Chart\n\n<figure>\n<figcaption>Figure 2 This is a figure</figcaption>\n\nValues\n300\n\n&lt;1% Jan Feb\n\n</figure>\n\nThis is footnote.\n",
    "<figure>\n\nA\nB\n\n</figure>\n<!-- PageBreak -->\n<figure>\nC\n\nD",
    '## Results\n\n<table>\n<caption>Table 1. Demo</caption>\n<tr><th rowspan="2">%</th><th colspan="2">H1</th></tr>\n<tr><th>A</th><th>B</th></tr>\n<tr><td>x &amp; y</td><td>1</td><td>2</td></tr>\n</table>\nThis is the footnote of the table.\n',
    "<table><tr><td>1</td></tr></table>\n\n<table><tr><td>2</td></tr></table>",
    "<table>\n<tr><td>a</td><td>b\n\n<tr><td>c</td>\n<!-- PageBreak -->\nnext page\n",
    '<!-- unterminated comment\n\nstray </table> and </figure>\n\n<!-- PageNumber="5"\n\n<td>orphan cell</td>\n',
    "# T\r\n\r\npara\r\n<!-- PageBreak -->\r\nnext\r\n",
)

_GROUPS = {
    "page_break": [s for s in _PARSE_SAMPLES if "PageBreak" in s]
    + [make_md(["a"]), make_md(["Agency mix.", "Partnership.", "Closing."])],
    "no_marker": [s for s in _PARSE_SAMPLES if "PageBreak" not in s] + ["", "\n\n"],
}

# 冻结快照：{组名: sha256}。改动旧路径导致摘要变化 = 行为回归。
_FROZEN = {
    "page_break": "cd065f417bcbf2dbaa620fd5b8dd8faab934b8c8da6a36bacfe0955ad4ebff6a",
    "no_marker": "c91b322b30df308046f80d307443f520fa3acefceb8fe7c24a2e70493ee8b78c",
}
_REAL_SAMPLE_FROZEN = "f3b3c032fdfe8ee0d049b3addc31948caa6674d6577a4f31139ebf43e1783c2d"

SAMPLE = ROOT_DIR / "data" / "di-markdown" / "aia-group-2026-interim-results-presentation.md"


def _digest(texts: list[str]) -> str:
    blob = "\n".join(repr(parse_di_markdown(t)) for t in texts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("group", sorted(_FROZEN))
def test_legacy_inputs_parse_byte_identical(group):
    assert _digest(_GROUPS[group]) == _FROZEN[group]


def test_real_sample_parse_byte_identical():
    if not SAMPLE.is_file():
        pytest.skip(
            "Optional DI markdown sample is absent (data/ is git-ignored); "
            "no download is performed."
        )
    assert _digest([SAMPLE.read_text(encoding="utf-8")]) == _REAL_SAMPLE_FROZEN
