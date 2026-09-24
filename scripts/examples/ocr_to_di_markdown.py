"""把逐页 OCR 结果（行 + bbox + 文字）转成 DI 风格 markdown + sidecar，供「真实场景」评测入库。

背景（ADR 0025 的评测）：现有 AIA markdown 由带文本层的原件导出，图表数值本来就在文字里，会系统性低估页图的
作用。转曲版 PDF 没有文本层，只能 OCR；这个脚本把 OCR 结果拼成 ``parse_di_markdown`` 能读的 markdown：

- 每页之间 ``<!-- PageBreak -->``，页尾 ``<!-- PageNumber="N" -->``（N = 物理页序）；
- 按 bbox 的阅读顺序拼段落：纵向有重叠的行归为同一视觉行、行内按 x 排；上下间距大于行高的一定比例就分段；
- 页面上部、字号明显大于本页中位行高的第一行记为 ``#`` 标题（粗略的版面启发式，DI 用训练过的分类器）；
- 表格不还原，降级为段落（同一视觉行的单元格以空格连接）；文字做 HTML 转义。

旁边写 ``<stem>.meta.json``，``source_pdf`` 指向转曲版 PDF，入库时即关联并渲染页图（页数须一致）。
路径与现有 sidecar 一致：在仓库内时写相对仓库根的 POSIX 路径（入库从仓库根运行，按 cwd 解析），
在仓库外时才写绝对路径。

输入 JSON 格式（PaddleOCR 实验脚本的输出）::

    {"engine": "...", "dpi": 300, "pages": [{"page": 1, "lines": [{"bbox": [x0, y0, x1, y1], "text": "...", "score": 0.99}]}]}

bbox 单位是 PDF 点（左上原点）。从项目根目录运行::

    .venv/bin/python scripts/examples/ocr_to_di_markdown.py <ocr.json> --pdf <outlined.pdf> --out <out.md>
"""

import argparse
import html
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PAGE_BREAK = "<!-- PageBreak -->"
# 同一视觉行：两行的纵向重叠 ≥ 较矮那行高度的这个比例。
ROW_OVERLAP = 0.5
# 分段：相邻视觉行的上下间距 > 行高中位数 × 该比例。
PARAGRAPH_GAP = 0.8
# 标题：位于页面上部这个比例之内，且行高 ≥ 本页行高中位数 × 该倍数。
TITLE_ZONE = 0.25
TITLE_SCALE = 1.4


@dataclass(frozen=True)
class OcrLine:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)


def _lines(raw: Sequence[Mapping[str, Any]], min_score: float) -> list[OcrLine]:
    out = []
    for item in raw:
        text = " ".join(str(item.get("text") or "").split())
        if not text or float(item.get("score") or 0.0) < min_score:
            continue
        x0, y0, x1, y1 = (float(v) for v in item["bbox"])
        out.append(OcrLine(x0, y0, x1, y1, text))
    return out


def _rows(lines: Sequence[OcrLine]) -> list[list[OcrLine]]:
    """纵向有足够重叠的行归为同一视觉行；视觉行按上沿排序，行内按 x 排序。"""
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda ln: (ln.y0, ln.x0)):
        for row in rows:
            top, bottom = min(ln.y0 for ln in row), max(ln.y1 for ln in row)
            overlap = min(bottom, line.y1) - max(top, line.y0)
            shorter = min(bottom - top, line.height) or 1.0
            if overlap >= ROW_OVERLAP * shorter:
                row.append(line)
                break
        else:
            rows.append([line])
    rows.sort(key=lambda row: min(ln.y0 for ln in row))
    return [sorted(row, key=lambda ln: ln.x0) for row in rows]


def _escape(text: str) -> str:
    escaped = html.escape(text, quote=False)
    # 行首 # 会被当成标题：转成实体（parse 会反转义回来）。
    return "&#35;" + escaped[1:] if escaped.startswith("#") else escaped


def page_markdown(
    raw_lines: Sequence[Mapping[str, Any]], page_height: float, *, min_score: float = 0.0
) -> str:
    """一页 OCR 行 → markdown 正文（不含分页 / 页码注释）。"""
    rows = _rows(_lines(raw_lines, min_score))
    if not rows:
        return ""
    heights = [max(ln.height for ln in row) for row in rows]
    median_h = statistics.median(heights) or 1.0
    blocks: list[str] = []
    paragraph: list[str] = []
    title_done = False
    previous_bottom: float | None = None
    for row, height in zip(rows, heights, strict=True):
        top = min(ln.y0 for ln in row)
        text = _escape(" ".join(ln.text for ln in row))
        is_title = (
            not title_done
            and page_height > 0
            and top <= page_height * TITLE_ZONE
            and height >= median_h * TITLE_SCALE
        )
        if is_title:
            if paragraph:
                blocks.append("\n".join(paragraph))
                paragraph = []
            blocks.append(f"# {text}")
            title_done = True
        else:
            if (
                previous_bottom is not None
                and top - previous_bottom > median_h * PARAGRAPH_GAP
                and paragraph
            ):
                blocks.append("\n".join(paragraph))
                paragraph = []
            paragraph.append(text)
        previous_bottom = max(ln.y1 for ln in row)
    if paragraph:
        blocks.append("\n".join(paragraph))
    return "\n\n".join(blocks)


def ocr_to_di_markdown(
    ocr: Mapping[str, Any], page_heights: Sequence[float], *, min_score: float = 0.0
) -> str:
    """整份 OCR 结果 → DI 风格 markdown。页数 = ``len(page_heights)``（PDF 页数），缺页输出空页。"""
    by_page = {int(p["page"]): p.get("lines") or [] for p in ocr.get("pages") or []}
    extra = sorted(set(by_page) - set(range(1, len(page_heights) + 1)))
    if extra:
        raise ValueError(f"OCR 结果里有 PDF 之外的页：{extra}")
    pages = []
    for index, height in enumerate(page_heights, start=1):
        body = page_markdown(by_page.get(index, []), height, min_score=min_score)
        footer = f'<!-- PageNumber="{index}" -->'
        pages.append(f"{body}\n\n{footer}\n" if body else f"{footer}\n")
    return f"\n{PAGE_BREAK}\n\n".join(pages)


def repo_relative(path: Path, root: Path | None = None) -> str:
    """仓库内的路径写成相对仓库根的 POSIX 路径（与现有 sidecar 一致）；仓库外写绝对路径。"""
    resolved = path.resolve()
    base = (root or _repo_root()).resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return str(resolved)


def _repo_root() -> Path:
    cwd = Path.cwd().resolve()
    return next((c for c in (cwd, *cwd.parents) if (c / ".project-root").exists()), cwd)


def pdf_page_heights(pdf: Path) -> list[float]:
    import pdfspine

    doc = pdfspine.open(str(pdf))
    try:
        return [float(doc[i].rect.height) for i in range(len(doc))]
    finally:
        doc.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ocr_json", type=Path, help="逐页 OCR 结果 JSON")
    parser.add_argument(
        "--pdf",
        type=Path,
        required=True,
        help="OCR 所用的（转曲版）PDF，写进 sidecar 的 source_pdf",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="输出 markdown 路径（sidecar 写在旁边）"
    )
    parser.add_argument(
        "--min-score", type=float, default=0.0, help="丢弃置信度低于该值的行（默认全留）"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ocr = json.loads(args.ocr_json.read_text(encoding="utf-8"))
    pdf = args.pdf.resolve()
    heights = pdf_page_heights(pdf)
    n_ocr = len(ocr.get("pages") or [])
    if n_ocr != len(heights):
        print(f"OCR 有 {n_ocr} 页，PDF 有 {len(heights)} 页：页数不一致", file=sys.stderr)
        return 2
    markdown = ocr_to_di_markdown(ocr, heights, min_score=args.min_score)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown, encoding="utf-8")
    sidecar = args.out.with_suffix(".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "source_pdf": repo_relative(pdf),
                "output_markdown": repo_relative(args.out),
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "generator": "scripts/examples/ocr_to_di_markdown.py - OCR lines stand-in for Azure DI "
                "prebuilt-layout Markdown (not real DI output)",
                "ocr_json": repo_relative(args.ocr_json),
                "ocr_engine": ocr.get("engine", ""),
                "ocr_dpi": ocr.get("dpi"),
                "min_score": args.min_score,
                "known_gaps_vs_di": [
                    "No tables or figures: table cells on one visual row are joined with spaces.",
                    "Headings: only the first large row in the top quarter of a page becomes '#'.",
                    "Reading order is row-major by bbox; multi-column slides interleave columns.",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"markdown: {args.out}（{len(heights)} 页）\nsidecar: {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
