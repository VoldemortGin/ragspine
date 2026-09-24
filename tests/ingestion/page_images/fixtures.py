"""页图测试的确定性小样本：pdfspine 现场生成的几页 PDF + 对应的 DI 风格 markdown。

不依赖 data/ 下的真实样本；同样的参数生成的 PDF 字节完全一致。
"""

from pathlib import Path

import pdfspine


def make_pdf(path: Path, labels: list[str], *, width: float = 400, height: float = 225) -> Path:
    """每页一行大字 label（只在图里、不在 markdown 里的内容用来证明模型读了图）。"""
    doc = pdfspine.open()
    try:
        for label in labels:
            page = doc.new_page(width=width, height=height)
            page.insert_text((20, 110), label, fontsize=24)
        path.write_bytes(doc.tobytes())
    finally:
        doc.close()
    return path


def make_md(pages: list[str]) -> str:
    """DI 风格 markdown：每页一个标题 + 一段正文，页间用 PageBreak 分隔。"""
    return "\n\n<!-- PageBreak -->\n\n".join(
        f"# Page {i}\n\n{text}\n" for i, text in enumerate(pages, start=1)
    )


def write_deck(
    tmp_path: Path, pages: list[str], labels: list[str] | None = None
) -> tuple[Path, Path]:
    """写 deck.md + deck.pdf（页数默认一致），返回 (md, pdf)。"""
    md = tmp_path / "deck.md"
    md.write_text(make_md(pages), encoding="utf-8")
    pdf = make_pdf(
        tmp_path / "deck.pdf", labels or [f"LABEL-{i}" for i in range(1, len(pages) + 1)]
    )
    return md, pdf
