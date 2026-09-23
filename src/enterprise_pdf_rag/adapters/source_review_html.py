"""Pure, one-page-at-a-time HTML projections of caller-validated source assets."""

import re
from html import escape

from enterprise_pdf_rag.documents.aia import AIA_SPEC
from ragspine.extraction.evidence.document.models import DocumentSnapshot, TextSidecar

_STYLE = """
body{font:16px system-ui;max-width:1200px;margin:32px auto;padding:20px;line-height:1.6;color:#20242a}
a{color:#075cab}code{overflow-wrap:anywhere}nav{display:flex;flex-wrap:wrap;gap:16px;margin:20px 0}
.pages{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:10px;padding:0;list-style:none}
.pages a{display:block;padding:8px 12px;border:1px solid #ccd2d8;border-radius:4px;text-align:center}
.source-svg{overflow:auto}.source-svg>svg{display:block;width:100%;height:auto;border:1px solid #ddd}
.pending{background:#fff6df;border-left:4px solid #a77300;padding:12px 18px}
table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}
td:first-child{white-space:pre-wrap;overflow-wrap:anywhere}.disabled{color:#6b7078}
"""


def _html(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; img-src data:; font-src data:\">"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _embed_svg(svg: str) -> str:
    # The standalone declaration cannot be nested inside HTML; all SVG elements,
    # attributes, glyph paths and definitions stay intact. Raw assets stay separate.
    return re.sub(r"^\s*<\?xml[^?]*\?>\s*", "", svg, count=1)


def _source(snapshot: DocumentSnapshot) -> str:
    manifest = snapshot.manifest
    return (
        f"<p>{escape(manifest.filename)} · 共 {len(manifest.pages)} 页</p>"
        f"<p>原始 PDF SHA-256: <code>{escape(manifest.source.sha256)}</code></p>"
        f"<p>固定来源快照: <code>{escape(snapshot.manifest_id)}</code></p>"
    )


def _pending() -> str:
    return (
        '<aside class="pending"><strong>来源审阅。图表语义仍待验证。</strong>'
        "<p>visual_completeness: pending; span_to_svg_mapping: pending; semantics: pending.</p>"
        "<p>原生 SVG 保存不等于与 PDF 视觉等价。基线、字体轮廓和裁剪等仍需核验。"
        "文字侧车与 glyph 没有已验证的一一映射。数值标签也不是已验证的年份或系列关系。"
        "没有 ChartIR、LLM 描述或 embedding。不提供财务结论。</p></aside>"
    )


def _validate_text(snapshot: DocumentSnapshot, page_index: int, text: TextSidecar) -> None:
    if (
        not 0 <= page_index < len(snapshot.manifest.pages)
        or snapshot.manifest.pages[page_index].page_index != page_index
        or text.page_index != page_index
        or text.source_sha256 != snapshot.manifest.source.sha256
    ):
        raise ValueError("Source page/text identity does not match the selected snapshot")


def _text_table(text: TextSidecar) -> str:
    intro = "<h2>本页原文观测 (不是 LLM 描述)</h2><p>坐标为 PDF 页左上角原点、单位为 point。</p>"
    if not text.spans:
        return intro + "<p>未提取到文本。不能据此认定页面空白。</p>"
    rows = "".join(
        f"<tr><td>{escape(span.text)}</td><td>{escape(str(span.bbox))}</td></tr>"
        for span in text.spans
    )
    return (
        intro
        + f"<table><thead><tr><th>原文</th><th>PDF bbox</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def render_index(*, snapshot: DocumentSnapshot, region_svg: str, region_text: TextSidecar) -> str:
    """List every page; embed only the selected review region, not the document."""
    manifest = snapshot.manifest
    region = manifest.region
    _validate_text(snapshot, region.page_index, region_text)
    links = "".join(
        f'<li><a href="pages/page-{index + 1:03d}.html">第 {index + 1} 页</a></li>'
        for index in range(len(manifest.pages))
    )
    warnings = "".join(f"<li>{escape(message)}</li>" for message in region.warnings)
    if manifest.source.sha256 == AIA_SPEC.sha256 and region.page_index == AIA_SPEC.focus_page_index:
        warnings += (
            "<li>本轮已查看第 25 页该选区: 原生 SVG 的红色基线比 PDF 栅格渲染更细。"
            "这是局部视觉观察。不升级本页或其他页的视觉、文字映射及图表语义资格。</li>"
        )
    body = (
        "<h1>PDF 来源审阅目录</h1>"
        + _source(snapshot)
        + '<nav><a href="source.pdf">打开完整原始 PDF</a><a href="text.json">全页文字侧车</a></nav>'
        + "<h2>逐页浏览</h2><p>每次打开一页的原生 SVG 与对应原文。下面的重点选区不代表整个 PDF。</p>"
        + f'<ol class="pages">{links}</ol>'
        + _pending()
        + f"<h2>重点选区: 第 {region.page_index + 1} 页</h2><p>页坐标选区: {escape(str(region.bbox))}</p>"
        + f'<p><a href="pages/page-{region.page_index + 1:03d}.html">查看该页完整内容</a></p>'
        + f'<div class="source-svg">{_embed_svg(region_svg)}</div><ul>{warnings}</ul>'
        + _text_table(region_text)
    )
    return _html("PDF 来源审阅目录", body)


def render_page(*, snapshot: DocumentSnapshot, page_index: int, svg: str, text: TextSidecar) -> str:
    """Show one validated page and its source text without semantic promotion."""
    _validate_text(snapshot, page_index, text)
    page_number = page_index + 1
    count = len(snapshot.manifest.pages)
    previous = (
        f'<a rel="prev" href="page-{page_number - 1:03d}.html">上一页</a>'
        if page_index > 0
        else '<span class="disabled">已是第一页</span>'
    )
    following = (
        f'<a rel="next" href="page-{page_number + 1:03d}.html">下一页</a>'
        if page_number < count
        else '<span class="disabled">已是最后一页</span>'
    )
    navigation = (
        f'<nav aria-label="页导航">{previous}<a href="../review.html">返回全部 {count} 页目录</a>'
        f'{following}<a href="../source.pdf#page={page_number}">原 PDF 第 {page_number} 页</a></nav>'
    )
    warnings = "".join(
        f"<li>{escape(message)}</li>" for message in snapshot.manifest.pages[page_index].warnings
    )
    body = (
        f"<h1>第 {page_number} 页, 共 {count} 页</h1>"
        + _source(snapshot)
        + navigation
        + _pending()
        + f'<main><div class="source-svg">{_embed_svg(svg)}</div>'
        + f"<ul>{warnings}</ul>"
        + _text_table(text)
        + "</main>"
        + navigation
    )
    return _html(f"第 {page_number} 页 — {snapshot.manifest.filename}", body)
