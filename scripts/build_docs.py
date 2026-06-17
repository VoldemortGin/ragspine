"""构建离线静态文档站：docs/site_src/*.md -> docs/site/*.html。

用法（从项目根目录）：
    .venv/bin/python scripts/build_docs.py

特性：
- 读取 docs/site_src/_pages.json 作为页面清单（顺序即导航顺序）；
- python-markdown 转换（fenced_code / tables / toc，Unicode slug 支持中文标题锚点）；
- 统一模板：暗色侧边导航（当前页高亮）、页内 TOC、上一页/下一页、顶部站名+构建时间；
- 零外部 CDN：样式在同目录 style.css，搜索为纯原生 JS（search.js + search-index.json，
  另输出 search-data.js 内嵌同一份索引，保证 file:// 直开时不受 fetch CORS 限制）；
- 构建后自检：页面齐全、站内链接两两可达、锚点存在、搜索索引覆盖全部页面。
"""

import html as html_mod
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import markdown
from markdown.extensions.toc import slugify_unicode

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / 'docs' / 'site_src'
OUT_DIR = ROOT / 'docs' / 'site'
SITE_NAME = 'RAGSpine Docs'

# ---------------------------------------------------------------- 模板

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — {site_name}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="topbar">
  <a class="site-name" href="index.html">{site_name}</a>
  <span class="build-time">构建于 {build_time}</span>
</header>
<div class="layout">
  <nav class="sidebar">
    <div class="search-box">
      <input type="search" id="search-input" placeholder="搜索文档…" autocomplete="off">
      <div id="search-results" class="search-results" hidden></div>
    </div>
    <ul class="nav-list">
{nav_items}
    </ul>
  </nav>
  <main class="content">
    <article class="article" id="article">
{body}
    </article>
    <nav class="pager">
{pager}
    </nav>
  </main>
  <aside class="page-toc">
    <div class="page-toc-title">本页目录</div>
{toc}
  </aside>
</div>
<script src="search-data.js"></script>
<script src="search.js"></script>
</body>
</html>
"""

STYLE_CSS = """\
/* RAGSpine Docs — 工程文档风样式（零外部依赖） */
:root {
  --sidebar-bg: #1e2129;
  --sidebar-fg: #c5cad3;
  --sidebar-active-bg: #2f6fed;
  --sidebar-active-fg: #ffffff;
  --topbar-bg: #2f6fed;
  --topbar-fg: #ffffff;
  --body-bg: #ffffff;
  --body-fg: #24292f;
  --muted-fg: #57606a;
  --border: #d8dde3;
  --code-bg: #f4f5f7;
  --link: #2056c7;
  --mark-bg: #ffe27a;
  --sidebar-w: 264px;
  --toc-w: 220px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 64px; }
body {
  margin: 0;
  background: var(--body-bg);
  color: var(--body-fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 15.5px;
  line-height: 1.75;
}

/* 顶栏 */
.topbar {
  position: fixed; top: 0; left: 0; right: 0; height: 48px; z-index: 30;
  display: flex; align-items: center; gap: 16px; padding: 0 20px;
  background: var(--topbar-bg); color: var(--topbar-fg);
  box-shadow: 0 1px 4px rgba(0,0,0,.18);
}
.topbar .site-name { color: var(--topbar-fg); font-weight: 700; font-size: 16px; text-decoration: none; }
.topbar .build-time { margin-left: auto; font-size: 12px; opacity: .85; }

/* 三栏布局 */
.layout { display: flex; padding-top: 48px; min-height: 100vh; }

/* 侧边导航（暗色） */
.sidebar {
  position: fixed; top: 48px; bottom: 0; left: 0; width: var(--sidebar-w);
  overflow-y: auto; background: var(--sidebar-bg); color: var(--sidebar-fg);
  padding: 14px 0 24px;
}
.sidebar .nav-list { list-style: none; margin: 8px 0 0; padding: 0; }
.sidebar .nav-list li a {
  display: block; padding: 7px 18px; color: var(--sidebar-fg);
  text-decoration: none; font-size: 14px; border-left: 3px solid transparent;
}
.sidebar .nav-list li a:hover { background: rgba(255,255,255,.07); color: #fff; }
.sidebar .nav-list li a.active {
  background: var(--sidebar-active-bg); color: var(--sidebar-active-fg);
  border-left-color: #fff; font-weight: 600;
}
.nav-index { color: rgba(255,255,255,.45); font-variant-numeric: tabular-nums; margin-right: 6px; }

/* 侧边搜索 */
.search-box { padding: 0 14px; position: relative; }
.search-box input {
  width: 100%; padding: 7px 10px; font-size: 13px;
  background: #2a2e38; color: #e8eaee; border: 1px solid #3a3f4b; border-radius: 5px;
  outline: none;
}
.search-box input:focus { border-color: var(--sidebar-active-bg); }
.search-results {
  margin-top: 6px; max-height: 50vh; overflow-y: auto;
  background: #262a33; border: 1px solid #3a3f4b; border-radius: 5px;
}
.search-results .sr-item { display: block; padding: 8px 10px; text-decoration: none; border-bottom: 1px solid #333845; }
.search-results .sr-item:last-child { border-bottom: none; }
.search-results .sr-item:hover { background: rgba(255,255,255,.06); }
.search-results .sr-title { color: #fff; font-size: 13px; font-weight: 600; }
.search-results .sr-snippet { color: #9aa1ad; font-size: 12px; line-height: 1.5; margin-top: 2px; }
.search-results .sr-snippet b { color: #ffd35c; font-weight: 600; }
.search-results .sr-empty { padding: 8px 10px; color: #9aa1ad; font-size: 12px; }

/* 正文 */
.content { flex: 1; margin-left: var(--sidebar-w); padding: 28px 36px 64px; min-width: 0; }
.article { max-width: 900px; margin: 0 auto; }
.article h1 { font-size: 30px; line-height: 1.3; margin: 6px 0 18px; padding-bottom: 10px; border-bottom: 1px solid var(--border); }
.article h2 { font-size: 22px; margin: 36px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.article h3 { font-size: 18px; margin: 28px 0 10px; }
.article h4 { font-size: 16px; margin: 22px 0 8px; }
.article a { color: var(--link); }
.article p { margin: 10px 0; }
.article ul, .article ol { padding-left: 26px; }
.article li { margin: 4px 0; }
.article blockquote {
  margin: 12px 0; padding: 2px 16px; color: var(--muted-fg);
  border-left: 4px solid var(--border); background: #fafbfc;
}
.article hr { border: none; border-top: 1px solid var(--border); margin: 28px 0; }
.article img { max-width: 100%; }

/* 代码 */
.article code {
  font-family: "SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas,
    "Liberation Mono", "Sarasa Mono SC", monospace;
  font-size: .875em;
  background: var(--code-bg); border-radius: 4px; padding: .15em .4em;
}
.article pre {
  background: var(--code-bg); border: 1px solid #e4e7eb; border-radius: 6px;
  padding: 13px 16px; overflow-x: auto; line-height: 1.55; margin: 14px 0;
}
.article pre code { background: none; padding: 0; font-size: 13px; display: block; }

/* 表格 */
.article table { border-collapse: collapse; margin: 14px 0; width: 100%; display: block; overflow-x: auto; font-size: 14px; }
.article th, .article td { border: 1px solid var(--border); padding: 6px 11px; text-align: left; vertical-align: top; }
.article th { background: #f3f5f8; font-weight: 600; white-space: nowrap; }
.article tr:nth-child(2n) td { background: #fafbfc; }

/* 搜索高亮 */
mark.search-hl { background: var(--mark-bg); padding: 0 1px; border-radius: 2px; }

/* 页内 TOC（宽屏右侧固定，窄屏隐藏） */
.page-toc {
  position: fixed; top: 64px; right: 12px; width: var(--toc-w);
  max-height: calc(100vh - 90px); overflow-y: auto;
  font-size: 12.5px; line-height: 1.5; padding-left: 12px;
  border-left: 1px solid var(--border);
}
.page-toc-title { font-weight: 700; color: var(--muted-fg); margin-bottom: 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.page-toc ul { list-style: none; margin: 0; padding-left: 0; }
.page-toc ul ul { padding-left: 14px; }
.page-toc li { margin: 3px 0; }
.page-toc a { color: var(--muted-fg); text-decoration: none; display: block; }
.page-toc a:hover { color: var(--link); }

/* 上一页/下一页 */
.pager { max-width: 900px; margin: 44px auto 0; display: flex; justify-content: space-between; gap: 14px; }
.pager a {
  flex: 1; max-width: 48%; border: 1px solid var(--border); border-radius: 7px;
  padding: 11px 15px; text-decoration: none; color: var(--body-fg);
}
.pager a:hover { border-color: var(--link); }
.pager .pager-label { display: block; font-size: 12px; color: var(--muted-fg); }
.pager .pager-title { display: block; font-size: 14.5px; color: var(--link); font-weight: 600; margin-top: 2px; }
.pager .pager-next { margin-left: auto; text-align: right; }

/* 响应式 */
@media (max-width: 1400px) { .page-toc { display: none; } }
@media (max-width: 900px) {
  .sidebar { position: static; width: auto; }
  .layout { display: block; }
  .content { margin-left: 0; padding: 20px 16px 48px; }
}

/* 打印 */
@media print {
  .topbar, .sidebar, .page-toc, .search-box, .pager { display: none !important; }
  .layout { display: block; padding-top: 0; }
  .content { margin: 0; padding: 0; }
  .article { max-width: none; }
  .article pre { white-space: pre-wrap; word-break: break-all; }
  .article a { color: inherit; text-decoration: none; }
  body { font-size: 12px; }
}
"""

SEARCH_JS = """\
/* RAGSpine Docs — 极简客户端搜索 + 关键词高亮跳转（纯原生 JS，零依赖）。
 * 索引来源：search-data.js 内嵌的 window.__SEARCH_INDEX__（file:// 直开可用）；
 * 若不存在则回退 fetch search-index.json（http 服务场景）。 */
(function () {
  'use strict';

  var input = document.getElementById('search-input');
  var resultsBox = document.getElementById('search-results');
  var INDEX = null;

  function escHtml(s) {
    return s.replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function escReg(s) { return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'); }

  function loadIndex(cb) {
    if (INDEX) { cb(INDEX); return; }
    if (window.__SEARCH_INDEX__) { INDEX = window.__SEARCH_INDEX__; cb(INDEX); return; }
    fetch('search-index.json')
      .then(function (r) { return r.json(); })
      .then(function (j) { INDEX = j; cb(INDEX); })
      .catch(function () { cb(null); });
  }

  function snippet(text, q) {
    var i = text.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return '';
    var start = Math.max(0, i - 36), end = Math.min(text.length, i + q.length + 56);
    var s = (start > 0 ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '');
    return escHtml(s).replace(new RegExp(escReg(escHtml(q)), 'gi'), function (m) { return '<b>' + m + '</b>'; });
  }

  function countHits(text, q) {
    var m = text.toLowerCase().split(q.toLowerCase()).length - 1;
    return m;
  }

  function render(q) {
    if (!q) { resultsBox.hidden = true; resultsBox.innerHTML = ''; return; }
    loadIndex(function (idx) {
      if (!idx) { resultsBox.hidden = false; resultsBox.innerHTML = '<div class="sr-empty">索引加载失败</div>'; return; }
      var hits = [];
      idx.pages.forEach(function (p) {
        var n = countHits(p.title + ' ' + p.text, q);
        if (n > 0) hits.push({ page: p, n: n });
      });
      hits.sort(function (a, b) { return b.n - a.n; });
      if (!hits.length) {
        resultsBox.hidden = false;
        resultsBox.innerHTML = '<div class="sr-empty">无匹配结果</div>';
        return;
      }
      var html = hits.slice(0, 10).map(function (h) {
        var url = h.page.url + '?hl=' + encodeURIComponent(q);
        return '<a class="sr-item" href="' + url + '">' +
          '<span class="sr-title">' + escHtml(h.page.title) + '（' + h.n + ' 处）</span>' +
          '<span class="sr-snippet">' + snippet(h.page.text, q) + '</span></a>';
      }).join('');
      resultsBox.hidden = false;
      resultsBox.innerHTML = html;
    });
  }

  if (input) {
    var timer = null;
    input.addEventListener('input', function () {
      clearTimeout(timer);
      var q = input.value.trim();
      timer = setTimeout(function () { render(q); }, 120);
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.search-box')) { resultsBox.hidden = true; }
    });
  }

  /* 进入页面时若带 ?hl=关键词，则高亮正文命中并滚动到第一处 */
  function highlightFromQuery() {
    var m = location.search.match(/[?&]hl=([^&]+)/);
    if (!m) return;
    var q = decodeURIComponent(m[1]).trim();
    if (!q) return;
    var article = document.getElementById('article');
    if (!article) return;
    var re = new RegExp(escReg(q), 'gi');
    var walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    while (walker.nextNode()) {
      var node = walker.currentNode;
      if (node.parentElement.closest('script,style')) continue;
      if (re.test(node.nodeValue)) nodes.push(node);
      re.lastIndex = 0;
    }
    var first = null;
    nodes.forEach(function (node) {
      var frag = document.createDocumentFragment();
      var text = node.nodeValue, last = 0, mm;
      re.lastIndex = 0;
      while ((mm = re.exec(text)) !== null) {
        frag.appendChild(document.createTextNode(text.slice(last, mm.index)));
        var mark = document.createElement('mark');
        mark.className = 'search-hl';
        mark.textContent = mm[0];
        frag.appendChild(mark);
        if (!first) first = mark;
        last = mm.index + mm[0].length;
      }
      frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
    if (first) {
      setTimeout(function () { first.scrollIntoView({ block: 'center' }); }, 50);
    }
  }
  highlightFromQuery();
})();
"""

# ---------------------------------------------------------------- 构建

TAG_RE = re.compile(r'<[^>]+>')
WS_RE = re.compile(r'\s+')


def html_to_text(html: str) -> str:
    """HTML -> 纯文本（供搜索索引）。"""
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
    text = TAG_RE.sub(' ', text)
    text = html_mod.unescape(text)
    return WS_RE.sub(' ', text).strip()


def build_nav(pages: list[dict], current: str) -> str:
    items = []
    for i, p in enumerate(pages, 1):
        url = Path(p['file']).with_suffix('.html').name
        cls = ' class="active"' if p['file'] == current else ''
        items.append(
            f'      <li><a href="{url}"{cls}>'
            f'<span class="nav-index">{i}.</span>{html_mod.escape(p["title"])}</a></li>'
        )
    return '\n'.join(items)


def build_pager(pages: list[dict], idx: int) -> str:
    parts = []
    if idx > 0:
        p = pages[idx - 1]
        url = Path(p['file']).with_suffix('.html').name
        parts.append(
            f'      <a class="pager-prev" href="{url}">'
            f'<span class="pager-label">← 上一页</span>'
            f'<span class="pager-title">{html_mod.escape(p["title"])}</span></a>'
        )
    if idx < len(pages) - 1:
        p = pages[idx + 1]
        url = Path(p['file']).with_suffix('.html').name
        parts.append(
            f'      <a class="pager-next" href="{url}">'
            f'<span class="pager-label">下一页 →</span>'
            f'<span class="pager-title">{html_mod.escape(p["title"])}</span></a>'
        )
    return '\n'.join(parts)


MD_LINK_RE = re.compile(r'href="([A-Za-z0-9_\-./]+)\.md(#[^"]*)?"')


def rewrite_md_links(body: str) -> str:
    """站内 .md 链接改写为 .html（保留锚点）。"""
    return MD_LINK_RE.sub(lambda m: f'href="{Path(m.group(1)).name}.html{m.group(2) or ""}"', body)


def build() -> int:
    pages = json.loads((SRC_DIR / '_pages.json').read_text(encoding='utf-8'))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_time = datetime.now().strftime('%Y-%m-%d %H:%M')

    md = markdown.Markdown(
        extensions=['fenced_code', 'tables', 'toc'],
        extension_configs={'toc': {'slugify': slugify_unicode, 'toc_depth': '2-4'}},
    )

    search_pages = []
    anchors: dict[str, set[str]] = {}   # html 文件名 -> 该页所有 id
    out_links: dict[str, list[tuple[str, str]]] = {}  # html 文件名 -> [(目标文件, 锚点)]
    total_chars = 0
    generated = []

    for i, page in enumerate(pages):
        src = SRC_DIR / page['file']
        out_name = Path(page['file']).with_suffix('.html').name
        md_text = src.read_text(encoding='utf-8')

        md.reset()
        body = md.convert(md_text)
        body = rewrite_md_links(body)
        toc_html = md.toc if md.toc_tokens else '<p style="color:#888">（本页无小节）</p>'

        page_html = PAGE_TEMPLATE.format(
            title=html_mod.escape(page['title']),
            site_name=SITE_NAME,
            build_time=build_time,
            nav_items=build_nav(pages, page['file']),
            body=body,
            pager=build_pager(pages, i),
            toc=toc_html,
        )
        (OUT_DIR / out_name).write_text(page_html, encoding='utf-8')
        generated.append(out_name)

        plain = html_to_text(body)
        total_chars += len(plain)
        search_pages.append({'url': out_name, 'title': page['title'], 'text': plain})

        anchors[out_name] = set(re.findall(r'\bid="([^"]+)"', page_html))
        links = []
        for href in re.findall(r'href="([^"]+)"', page_html):
            if href.startswith(('http://', 'https://', 'mailto:')):
                continue
            if href.endswith('.css'):
                continue
            target, _, frag = href.partition('#')
            links.append((target, frag))
        out_links[out_name] = links

    # 静态资产
    (OUT_DIR / 'style.css').write_text(STYLE_CSS, encoding='utf-8')
    (OUT_DIR / 'search.js').write_text(SEARCH_JS, encoding='utf-8')
    index_json = json.dumps({'pages': search_pages}, ensure_ascii=False)
    (OUT_DIR / 'search-index.json').write_text(index_json, encoding='utf-8')
    (OUT_DIR / 'search-data.js').write_text(
        'window.__SEARCH_INDEX__ = ' + index_json + ';\n', encoding='utf-8'
    )

    # ------------------------------------------------------------ 自检
    errors = []

    # 1) 页面齐全
    expected = {Path(p['file']).with_suffix('.html').name for p in pages}
    missing = expected - {f.name for f in OUT_DIR.glob('*.html')}
    if missing:
        errors.append(f'缺失页面: {sorted(missing)}')

    # 2) 站内链接有效（文件存在 + 锚点存在）
    for src_page, links in out_links.items():
        for target, frag in links:
            t = target or src_page  # 纯锚点链接指向本页
            if t not in anchors:
                if not (OUT_DIR / t).exists():
                    errors.append(f'{src_page}: 链接目标不存在 {target}#{frag}')
                    continue
            if frag and t in anchors and frag not in anchors[t]:
                errors.append(f'{src_page}: 锚点不存在 {t}#{frag}')

    # 3) 导航两两可达：每页侧边栏必须含全部其他页面的链接
    for src_page in expected:
        targets = {t for t, _ in out_links[src_page] if t and t.endswith('.html')}
        unreachable = expected - targets - {src_page}
        if unreachable:
            errors.append(f'{src_page}: 导航缺少到 {sorted(unreachable)} 的链接')

    # 4) 搜索索引覆盖全部页面
    indexed = {p['url'] for p in search_pages}
    if indexed != expected:
        errors.append(f'搜索索引页面不全: 缺 {sorted(expected - indexed)}')

    # ------------------------------------------------------------ 报告
    print(f'生成 {len(generated)} 页 -> {OUT_DIR}')
    for name in generated:
        size = (OUT_DIR / name).stat().st_size
        print(f'  {name:28s} {size:>8,} B')
    for asset in ('style.css', 'search.js', 'search-index.json', 'search-data.js'):
        print(f'  {asset:28s} {(OUT_DIR / asset).stat().st_size:>8,} B')
    print(f'正文纯文本总字数: {total_chars:,}')
    if errors:
        print(f'\nLINK CHECK FAILED ({len(errors)}):')
        for e in errors:
            print(f'  - {e}')
        return 1
    print('LINK CHECK PASSED: 页面齐全 / 站内链接与锚点全部有效 / 导航两两可达 / 搜索索引覆盖全部页面')
    return 0


if __name__ == '__main__':
    sys.exit(build())
