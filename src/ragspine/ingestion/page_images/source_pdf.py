"""DI markdown 与原 PDF 的显式关联：解析、校验、记 sha256。

来源（优先级从高到低）：
1. 显式参数：``RAGSpine.ingest(md, source_pdf=...)``、CLI ``--source-pdf``、服务端 job 的 ``source_pdf``；
   只能配一个 ``.md`` 输入。
2. sidecar：与 markdown 同名的 ``<stem>.meta.json`` 里的 ``"source_pdf"`` 字段（pdf_to_di_markdown 生成器
   已写这个字段）。相对路径先按 sidecar 所在目录解析，找不到再按当前工作目录解析。
3. 都没有：不关联，纯文本入库，行为与之前完全一样。

校验：PDF 页数必须等于 markdown 的页数（``DiPage.index`` 的最大值，即物理页序）。关联是调用方显式声明的，
页数不一致通常意味着配错了文件（例如另一版 PDF），这时发出去的页图会与文本错位、把错误的数字带进上下文，
比不发图更糟，所以直接抛 :class:`SourcePdfError`，在任何写入之前失败，而不是悄悄降级。

marker 模式（markdown 含 ``<!-- page: N -->``，``DiPage.index`` 即真实 PDF 页码，ADR 0027）：只分析了部分页时
md 页数可以少于 PDF，所以放宽为 ``max(index) <= PDF 页数``，页号超出 PDF 明确报错。放宽削弱了错配保护，由
sidecar 补偿（字段缺失即跳过该项；sidecar 缺失或不可解析则全部跳过）。两种来源的页数语义不同，分开读：

- SuperIndex azure_di ``extract_corpus`` 的整数 ``pages`` = **本次分析的页数**，必须等于 md 中不重复的页标记
  页号个数（校验 md 与抽取结果一致，不与 PDF 总页数比）；
- ragspine ``pdf_to_di_markdown`` 的 ``page_count`` = **原 PDF 总页数**，必须等于 PDF 页数（它的 ``pages`` 是
  逐页列表，不是计数）；
- 原 PDF 的 sha256（``source_pdf_sha256``，兼容 ``pdf_sha256`` / ``source_sha256``，不分大小写）必须等于
  实际 PDF 的 sha256。
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ragspine.extraction.di_markdown.parse import (
    has_page_markers,
    page_marker_numbers,
    parse_di_markdown,
)
from ragspine.ingestion.page_images.render import pdf_page_count

SIDECAR_SUFFIX = ".meta.json"
SIDECAR_SOURCE_PDF_KEY = "source_pdf"
SIDECAR_PDF_TOTAL_PAGES_KEY = "page_count"  # ragspine pdf_to_di_markdown：原 PDF 总页数
SIDECAR_ANALYZED_PAGES_KEY = "pages"  # SuperIndex azure_di extract_corpus：本次分析的页数
SIDECAR_PDF_SHA256_KEYS = ("source_pdf_sha256", "pdf_sha256", "source_sha256")
MARKDOWN_SUFFIX = ".md"


class SourcePdfError(ValueError):
    """source PDF 缺失、不可读、越界或页数与 markdown 不一致。"""


@dataclass(frozen=True)
class SourcePdf:
    """校验通过的原 PDF。"""

    path: Path
    sha256: str
    page_count: int


def sidecar_path(md_path: str | Path) -> Path:
    return Path(md_path).with_suffix(SIDECAR_SUFFIX)


def markdown_page_count(md_path: str | Path) -> int:
    return _page_count(Path(md_path).read_text(encoding="utf-8"))


def _page_count(text: str) -> int:
    return max((page.index for page in parse_di_markdown(text).pages), default=0)


@dataclass(frozen=True)
class SidecarFacts:
    """sidecar 里供 marker 模式校验的字段，按来源区分语义；没有的字段为 None。"""

    pdf_total_pages: int | None  # ragspine：原 PDF 总页数
    analyzed_pages: int | None  # SuperIndex：本次分析的页数
    pdf_sha256: tuple[str, ...]  # 记录的原 PDF sha256（小写；可能来自多个兼容字段）


def read_sidecar_facts(md_path: str | Path) -> SidecarFacts:
    """读 sidecar；缺失或不可解析时各项为空（校验跳过，与 ``resolve_source_pdf`` 的报错互不影响）。"""
    try:
        data = json.loads(sidecar_path(md_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return SidecarFacts(pdf_total_pages=None, analyzed_pages=None, pdf_sha256=())
    return SidecarFacts(
        pdf_total_pages=_ragspine_pdf_total_pages(data),
        analyzed_pages=_superindex_analyzed_pages(data),
        pdf_sha256=tuple(
            value.strip().lower()
            for key in SIDECAR_PDF_SHA256_KEYS
            if isinstance(value := data.get(key), str) and value.strip()
        ),
    )


def _ragspine_pdf_total_pages(data: dict[str, object]) -> int | None:
    return _int(data.get(SIDECAR_PDF_TOTAL_PAGES_KEY))


def _superindex_analyzed_pages(data: dict[str, object]) -> int | None:
    # ragspine sidecar 的 pages 是逐页列表，_int 不认，只有 SuperIndex 的整数 pages 进来
    return _int(data.get(SIDECAR_ANALYZED_PAGES_KEY))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def resolve_source_pdf(md_path: str | Path, explicit: str | Path | None = None) -> Path | None:
    """显式参数 > sidecar 字段 > None。给了但文件不存在时抛 SourcePdfError。"""
    md = Path(md_path)
    if explicit is not None:
        return _existing(Path(explicit), origin="source_pdf 参数")
    sidecar = sidecar_path(md)
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourcePdfError(f"sidecar 无法解析：{sidecar}（{exc}）") from exc
    value = data.get(SIDECAR_SOURCE_PDF_KEY) if isinstance(data, dict) else None
    if not value:
        return None
    raw = Path(str(value))
    if not raw.is_absolute() and (sidecar.parent / raw).is_file():
        raw = sidecar.parent / raw
    return _existing(raw, origin=f"sidecar {sidecar.name}")


def validate_source_pdf(md_path: str | Path, pdf_path: str | Path) -> SourcePdf:
    """页数校验 + sha256。"""
    pdf = Path(pdf_path).resolve()
    try:
        pages = pdf_page_count(pdf)
    except Exception as exc:  # noqa: BLE001 — pdfspine 的各类解析异常统一归为关联错误
        raise SourcePdfError(f"source PDF 无法打开：{pdf}（{type(exc).__name__}: {exc}）") from exc
    text = Path(md_path).read_text(encoding="utf-8")
    expected = _page_count(text)
    name = Path(md_path).name
    if not has_page_markers(text):
        if pages != expected:
            raise SourcePdfError(
                f"source PDF 页数不一致：{pdf.name} 有 {pages} 页，{name} 有 {expected} 页（物理页序）"
            )
    elif expected > pages:
        raise SourcePdfError(
            f"source PDF 页数不足：{name} 的页标记页号 {expected} 超出 {pdf.name} 的 {pages} 页"
        )
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if has_page_markers(text):
        _check_sidecar(md_path, text, pdf, pages, sha)
    return SourcePdf(path=pdf, sha256=sha, page_count=pages)


def _check_sidecar(md_path: str | Path, text: str, pdf: Path, pages: int, sha: str) -> None:
    facts = read_sidecar_facts(md_path)
    sidecar = sidecar_path(md_path).name
    if facts.pdf_total_pages is not None and facts.pdf_total_pages != pages:
        raise SourcePdfError(
            f"source PDF 页数与 sidecar 不一致：{pdf.name} 有 {pages} 页，"
            f"{sidecar} 的 {SIDECAR_PDF_TOTAL_PAGES_KEY} 记录 {facts.pdf_total_pages} 页"
        )
    marked = len(page_marker_numbers(text))
    if facts.analyzed_pages is not None and facts.analyzed_pages != marked:
        raise SourcePdfError(
            f"markdown 与 sidecar 不一致：{Path(md_path).name} 有 {marked} 个不重复页标记，"
            f"{sidecar} 的 {SIDECAR_ANALYZED_PAGES_KEY} 记录分析了 {facts.analyzed_pages} 页"
        )
    if any(recorded != sha for recorded in facts.pdf_sha256):
        raise SourcePdfError(f"source PDF 的 sha256 与 sidecar {sidecar} 记录的不一致：{pdf.name}")


def prepare_source_pdfs(
    paths: Iterable[str | Path],
    explicit: str | Path | None = None,
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, SourcePdf]:
    """入库前为本批 markdown 解析并校验原 PDF，返回 ``{doc_id: SourcePdf}``（doc_id = 文件名）。

    显式 PDF 要求本批恰好一个 ``.md``；非 markdown 输入忽略。任何问题都在写入前抛 SourcePdfError。
    """
    markdown = [Path(p) for p in paths if Path(p).suffix.lower() == MARKDOWN_SUFFIX]
    if explicit is not None and len(markdown) != 1:
        raise SourcePdfError(
            f"source_pdf 只能配一个 .md 输入，本批有 {len(markdown)} 个；多个文件请用 sidecar"
            f" {SIDECAR_SUFFIX} 的 {SIDECAR_SOURCE_PDF_KEY!r} 字段"
        )
    root = Path(allowed_root).resolve() if allowed_root is not None else None
    sources: dict[str, SourcePdf] = {}
    owners: dict[str, set[Path]] = {}
    for md in markdown:
        owners.setdefault(md.name, set()).add(md.resolve())
    for md in markdown:
        pdf = resolve_source_pdf(md, explicit)
        if pdf is None:
            continue
        if root is not None and not pdf.is_relative_to(root):
            raise SourcePdfError(f"source PDF 不在 allowed_upload_root 内：{pdf}")
        if len(owners[md.name]) > 1:
            raise SourcePdfError(
                f"本批有多个同名 {md.name!r}（doc_id 相同），无法确定页图挂哪份 PDF；请改名后再入库"
            )
        sources[md.name] = validate_source_pdf(md, pdf)
    return sources


def _existing(path: Path, *, origin: str) -> Path:
    if not path.is_file():
        raise SourcePdfError(f"{origin} 指向的 PDF 不存在：{path}")
    return path.resolve()
