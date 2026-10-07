"""一期（Excel 线）测试共享 fixture：fixtures 路径、ground truth 加载、临时 sqlite。

6 个测试模块（ir / xlsx_styled_extractor / color_semantics / review_queue /
fact_store v2 / extraction_eval）共用这里的 pytest fixture，避免各自重复造数。
合成 fixture 缺失时自动一键再生（确定性、可重跑），不依赖任何真实敏感数据
（PRD user story 20）。
"""

import ipaddress
import json
import os
import socket

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)
# 与开发者本机项目根的 .env 隔离(configs 默认读它);要测 .env 的用例自行 delenv。
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from ragspine.fixtures.excel import GT_PATH, XLSX_PATH
from ragspine.fixtures.excel import main as make_excel_fixtures

# 开发者 shell 里的 OPENAI_* 与各家 provider key / token:测试进程一律看不到(防止偷偷联网或用真 key)。
_OPENAI_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_EMBEDDING_MODEL",
    "OPENAI_TEMPERATURE",
)
_PROVIDER_SECRET_NAMES = (
    "OPENAI_ORG_ID",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_API_KEY",
)
# 本机 HTTP 代理变量:放行回环会漏过代理流量,所以也清掉(含小写)。
_PROXY_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
# 只有显式开启这些开关的 network 用例,才会拿回真实 key 与代理(见 _isolate_ambient_... 与 no_network)。
_SMOKE_SWITCHES = ("RAGSPINE_LITELLM_SMOKE", "RAGSPINE_CLAUDE_CLI_SMOKE")
# conftest 导入期(任何清理之前)的真实环境快照,仅供 smoke 用例恢复。
_AMBIENT_ENV = {
    name: os.environ[name]
    for name in (*_OPENAI_NAMES, *_PROVIDER_SECRET_NAMES, *_PROXY_NAMES)
    if name in os.environ
}


def _is_network_case(node) -> bool:
    return node.get_closest_marker("network") is not None


def _is_smoke_case(node) -> bool:
    """带 network 标记且对应 RAGSPINE_*_SMOKE=1 已显式开启的真实调用用例。"""
    return _is_network_case(node) and any(os.environ.get(name) == "1" for name in _SMOKE_SWITCHES)


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _isolate_ambient_openai_for_scoped_fixtures():
    """OPENAI_* 是 LLM 首选名,会盖过用例设的 APP_LLM_*;module/session 级 fixture 先于下面的
    函数级清理运行,所以这里在会话级先清一遍。"""
    with pytest.MonkeyPatch.context() as patch:
        for name in (*_OPENAI_NAMES, *_PROVIDER_SECRET_NAMES, *_PROXY_NAMES):
            patch.delenv(name, raising=False)
        yield


@pytest.fixture(autouse=True)
def _forget_unsupported_sampling_parameters():
    """JsonCompletionClient 记得端点拒收的采样参数(进程级,ADR 0021);每个用例前后都清空,互不串扰。"""
    from ragspine.common.evidence.providers.json_completion import (
        forget_unsupported_sampling_parameters,
    )

    forget_unsupported_sampling_parameters()
    yield
    forget_unsupported_sampling_parameters()


@pytest.fixture(autouse=True)
def _isolate_ambient_llm_and_notebook_settings(monkeypatch, request):
    """开发者 shell 里的 OPENAI_* / provider key / 代理 / NB_* / PDF_INGEST_PASSWORD 不得影响测试(configs 会把它们读成 LLM 首选名与 notebook 路径)。

    要测这些变量的用例自己 monkeypatch.setenv;get_settings() 是进程级缓存,前后各清一次,
    让它既不带着 import 期的环境,也不把某个用例的环境漏给下一个。
    例外:带 network 标记的用例需要出网(下载模型权重),拿回代理变量;若同时显式开了
    RAGSPINE_LITELLM_SMOKE=1 / RAGSPINE_CLAUDE_CLI_SMOKE=1(真实调用 smoke),还拿回真实 key。
    """
    from ragspine.common.evidence.configs import get_settings

    for name in (
        *_OPENAI_NAMES,
        *_PROVIDER_SECRET_NAMES,
        *_PROXY_NAMES,
        "NB_PDF_DIR",
        "NB_QUESTIONS_PATH",
        "DATASET_PATH",  # questions_path 的回落别名
        "NB_REPORT_DIR",
        "PDF_INGEST_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    restore: tuple[str, ...] = ()
    if _is_smoke_case(request.node):
        restore = (*_OPENAI_NAMES, *_PROVIDER_SECRET_NAMES, *_PROXY_NAMES)
    elif _is_network_case(request.node):
        restore = _PROXY_NAMES
    for name in restore:
        if name in _AMBIENT_ENV:
            monkeypatch.setenv(name, _AMBIENT_ENV[name])
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """默认断网:对非回环地址的 connect / connect_ex / create_connection 抛 BLOCKED_OUTBOUND。

    放行回环地址与 unix socket;带 network 标记的用例(下载模型权重 / 真实调用 smoke)不拦。
    """
    if _is_network_case(request.node):
        return

    def _check(address) -> None:
        if isinstance(address, tuple) and address:
            host = address[0]
            port = address[1] if len(address) > 1 else None
            if not _is_loopback_host(host):
                raise RuntimeError(f"BLOCKED_OUTBOUND host={host} port={port}")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def connect(self, address):
        if self.family != getattr(socket, "AF_UNIX", None):
            _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family != getattr(socket, "AF_UNIX", None):
            _check(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        _check(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


@pytest.fixture(scope="session", autouse=True)
def _ensure_fixtures() -> None:
    """整轮测试前确保合成 fixture 与 ground truth 存在（缺失则一键再生）。"""
    if not XLSX_PATH.exists() or not GT_PATH.exists():
        make_excel_fixtures()


@pytest.fixture(scope="session")
def fixtures_dir():
    """data/fixtures 目录路径。"""
    return GT_PATH.parent


@pytest.fixture(scope="session")
def excel_fixture_path():
    """合成 Excel fixture（excel_styled_fixture.xlsx）的绝对路径。"""
    return XLSX_PATH


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    """加载逐格真值 JSON（原始结构：file / theme_palette / sheets / cells）。"""
    return json.loads(GT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def cell_truth(ground_truth) -> dict:
    """逐格真值索引：'sheet!cell_ref' -> 期望字段 dict，便于断言取用。"""
    return {f"{c['sheet']}!{c['cell_ref']}": c for c in ground_truth["cells"]}


@pytest.fixture
def tmp_db_path(tmp_path):
    """每个测试独立的临时 sqlite 路径（fact_store / registry / queue 共用）。"""
    return tmp_path / "test.db"


@pytest.fixture
def tmp_sqlite_factory(tmp_path):
    """生成多个互不冲突的临时 sqlite 路径（同一测试需开多个库时用）。"""
    counter = {"n": 0}

    def _make(name: str = "db") -> str:
        counter["n"] += 1
        return str(tmp_path / f"{name}_{counter['n']}.db")

    return _make


# ===========================================================================
# 二期（PDF 线）fixture：合成 PDF 路径、ground truth 加载、缺失再生（autouse）。
# 仿照上方 Excel 的写法，纯追加，不改动既有内容。
# ===========================================================================

from ragspine.fixtures.pdf import (
    DIGITAL_PATH,
    MIXED_PATH,
    OCR_SCAN_PATH,
    PPT_EXPORT_PATH,
    SCANNED_PATH,
)
from ragspine.fixtures.pdf import (
    GT_PATH as PDF_GT_PATH,
)
from ragspine.fixtures.pdf import (
    OUT_DIR as PDF_OUT_DIR,
)
from ragspine.fixtures.pdf import (
    main as make_pdf_fixtures,
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_pdf_fixtures() -> None:
    """整轮测试前确保 PDF 合成 fixture 与 ground truth 存在（缺失则一键再生）。"""
    paths = (DIGITAL_PATH, SCANNED_PATH, OCR_SCAN_PATH, MIXED_PATH, PPT_EXPORT_PATH, PDF_GT_PATH)
    if not all(p.exists() for p in paths):
        make_pdf_fixtures()


@pytest.fixture(scope="session")
def pdf_fixtures_dir():
    """data/fixtures/pdf 目录路径。"""
    return PDF_OUT_DIR


@pytest.fixture(scope="session")
def pdf_ground_truth() -> dict:
    """加载 PDF 线 ground truth（files 期望 + dual_channel 测试向量）。"""
    return json.loads(PDF_GT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def digital_pdf_path():
    """digital.pdf（数字型：表格页 + 叙述页）绝对路径。"""
    return DIGITAL_PATH


@pytest.fixture(scope="session")
def scanned_pdf_path():
    """scanned.pdf（扫描型：整页位图、无文本层）绝对路径。"""
    return SCANNED_PATH


@pytest.fixture(scope="session")
def ocr_scan_pdf_path():
    """ocr_scan.pdf（OCR 扫描型：位图 + 隐形文本层）绝对路径。"""
    return OCR_SCAN_PATH


@pytest.fixture(scope="session")
def mixed_pdf_path():
    """mixed.pdf（混合型：2 数字 + 2 扫描）绝对路径。"""
    return MIXED_PATH


@pytest.fixture(scope="session")
def ppt_export_pdf_path():
    """ppt_export.pdf（PowerPoint 导出，producer 元数据标记）绝对路径。"""
    return PPT_EXPORT_PATH


# ===========================================================================
# 三期（PPT 增强 + 扫描线）fixture：合成 pptx 路径、ground truth 加载、缺失再生
# （autouse）。仿照上方 Excel / PDF 的写法，纯追加，不改动既有内容。
# OCR fake 测试向量内嵌在 pptx ground truth 里（引用二期已生成的 scanned.pdf）。
# ===========================================================================

from ragspine.fixtures.pptx import (
    GT_PATH as PPTX_GT_PATH,
)
from ragspine.fixtures.pptx import (
    PPTX_PATH,
)
from ragspine.fixtures.pptx import (
    main as make_pptx_fixtures,
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_pptx_fixtures(_ensure_pdf_fixtures) -> None:
    """整轮测试前确保 pptx 合成 fixture 与 ground truth 存在（缺失则一键再生）。

    依赖 _ensure_pdf_fixtures 先跑：OCR fake 向量引用 scanned.pdf，须保证其已生成。
    """
    if not PPTX_PATH.exists() or not PPTX_GT_PATH.exists():
        make_pptx_fixtures()


@pytest.fixture(scope="session")
def pptx_fixtures_dir():
    """data/fixtures/pptx 目录路径。"""
    return PPTX_GT_PATH.parent


@pytest.fixture(scope="session")
def styled_deck_path():
    """styled_deck.pptx（增强表格 + 含数字文本框 / 备注 + 转置表）绝对路径。"""
    return PPTX_PATH


@pytest.fixture(scope="session")
def pptx_ground_truth() -> dict:
    """加载 PPT 线 ground truth（逐格值/填充、note fragments、OCR fake 向量）。"""
    return json.loads(PPTX_GT_PATH.read_text(encoding="utf-8"))


# ===========================================================================
# W3b（docspine .docx 线）fixture：纯 zipfile 合成最小 .docx 的工厂（不落二进制
# fixture、不引入 python-docx）。仿照上方各期写法，纯追加，不改动既有内容。
# docspine（纯 Rust DOCX 解析）只需最小 OOXML 包（[Content_Types].xml + _rels/.rels
# + word/document.xml）即可解析，故按需手写这三段 XML 合成（docspine CLAUDE.md 约定）。
# ===========================================================================

import zipfile
from xml.sax.saxutils import escape as _xml_escape

_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_DOCX_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCX_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_paragraph_xml(text: str) -> str:
    if not text:
        return "<w:p/>"
    return f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'


def _docx_cell_xml(cell) -> str:
    """cell 为 str 或 {'text','gridspan','vmerge','fill','nested'} dict。

    vmerge ∈ {'restart','continue'}；fill 为底纹色 'RRGGBB'（-> <w:shd w:fill=...>，
    docspine 解析为 cell['fill']，W3d 喂 resolved_rgb）；nested 为嵌套表列表，每项是一张
    嵌套表的 rows（list[list[cell]]），渲染为该格段落后的子 <w:tbl>（docspine 暴露为
    cell['blocks'] 里的 kind=='table' 块，W3d 产出独立 StyledGrid）。
    """
    if isinstance(cell, dict):
        text = cell.get("text", "")
        gridspan = cell.get("gridspan")
        vmerge = cell.get("vmerge")
        fill = cell.get("fill")
        nested = cell.get("nested") or []
    else:
        text, gridspan, vmerge, fill, nested = cell, None, None, None, []
    # w:tcPr 子元素按 OOXML schema 次序：gridSpan -> vMerge -> shd。
    props = []
    if gridspan:
        props.append(f'<w:gridSpan w:val="{int(gridspan)}"/>')
    if vmerge in ("restart", "continue"):
        # docspine 把无 val 的裸 <w:vMerge/> 视作 restart，故续格必须显式 val="continue"。
        props.append(f'<w:vMerge w:val="{vmerge}"/>')
    if fill:
        props.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>')
    tcpr = f"<w:tcPr>{''.join(props)}</w:tcPr>" if props else ""
    nested_xml = "".join(_docx_table_xml(rows) for rows in nested)
    return f"<w:tc>{tcpr}{_docx_paragraph_xml(text)}{nested_xml}</w:tc>"


def _docx_table_xml(rows) -> str:
    trs = "".join(f"<w:tr>{''.join(_docx_cell_xml(c) for c in row)}</w:tr>" for row in rows)
    return f"<w:tbl>{trs}</w:tbl>"


def _docx_document_xml(body) -> str:
    parts = []
    for kind, payload in body:
        if kind == "para":
            parts.append(_docx_paragraph_xml(payload))
        elif kind == "table":
            parts.append(_docx_table_xml(payload))
        else:
            raise ValueError(f"unknown docx body item kind: {kind!r}")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_DOCX_W_NS}"><w:body>{"".join(parts)}</w:body></w:document>'
    )


@pytest.fixture
def make_docx():
    """合成最小 .docx 的工厂（纯 zipfile，不依赖 python-docx）。

    body 为有序列表，每项 ('para', text) 或 ('table', rows)；rows 为 list[list[cell]]，
    cell 为 str 或 {'text','gridspan','vmerge','fill','nested'} dict（造合并 / 着色 /
    嵌套表用，见 _docx_cell_xml）。返回 build(path, body)。
    """

    def _build(path, body):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
            z.writestr("_rels/.rels", _DOCX_ROOT_RELS)
            z.writestr("word/document.xml", _docx_document_xml(body))
        return path

    return _build


# ===========================================================================
# W3c（pptspine .pptx 线）fixture：纯 zipfile 合成最小 .pptx 的工厂（不落二进制
# fixture、不引入 python-pptx）。仿照上方 make_docx 写法，纯追加，不改动既有内容。
# 一个 .pptx 是 OOXML —— 一个装着 XML 部件的 zip 包。pptspine（纯 Rust）直接走 XML，
# 故只需最小部件集（[Content_Types].xml + 根/演示文稿 rels + 每页 slide{N}.xml）即可解析。
# 单元格合并经 a:tc 上的 gridSpan/rowSpan（锚点）+ hMerge/vMerge（延续格）表达，
# 正是 pptspine 富表合并模型的来源（slide.rs 按本地名读取这些属性）。
# ===========================================================================

_PPTX_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PPTX_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PPTX_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_PPTX_TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"
_PPTX_EMPTY_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
)
_PPTX_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="ppt/presentation.xml"/>'
    "</Relationships>"
)


def _pptx_tc_xml(cell) -> str:
    """cell 为 str 或 {'text','gridspan','rowspan','hmerge','vmerge','fill'} dict（造合并 /
    着色表用）。

    gridSpan/rowSpan 标在合并锚点格上；hMerge/vMerge 标在被吞的延续格上（空文本）；
    fill 为单元格底色 'RRGGBB'（-> a:tcPr 里的 <a:solidFill><a:srgbClr val=...>，pptspine
    解析为 cell['fill']，W3d 喂 resolved_rgb）。
    """
    if isinstance(cell, dict):
        text = cell.get("text", "")
        gridspan = cell.get("gridspan")
        rowspan = cell.get("rowspan")
        hmerge = cell.get("hmerge")
        vmerge = cell.get("vmerge")
        fill = cell.get("fill")
    else:
        text, gridspan, rowspan, hmerge, vmerge, fill = cell, None, None, None, None, None
    attrs = []
    if gridspan:
        attrs.append(f'gridSpan="{int(gridspan)}"')
    if rowspan:
        attrs.append(f'rowSpan="{int(rowspan)}"')
    if hmerge:
        attrs.append('hMerge="1"')
    if vmerge:
        attrs.append('vMerge="1"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    body = f"<a:p><a:r><a:t>{_xml_escape(text)}</a:t></a:r></a:p>" if text else "<a:p/>"
    tcpr = (
        f'<a:tcPr><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill></a:tcPr>'
        if fill
        else "<a:tcPr/>"
    )
    return f"<a:tc{attr_str}><a:txBody>{body}</a:txBody>{tcpr}</a:tc>"


def _pptx_table_xml(rows) -> str:
    trs = "".join(
        f'<a:tr h="370840">{"".join(_pptx_tc_xml(c) for c in row)}</a:tr>' for row in rows
    )
    return (
        "<p:graphicFrame>"
        '<p:xfrm><a:off x="838200" y="2000250"/><a:ext cx="7772400" cy="2000250"/></p:xfrm>'
        f'<a:graphic><a:graphicData uri="{_PPTX_TABLE_URI}"><a:tbl>{trs}'
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def _pptx_textbox_xml(text: str) -> str:
    return (
        "<p:sp><p:spPr/><p:txBody>"
        f"<a:p><a:r><a:t>{_xml_escape(text)}</a:t></a:r></a:p>"
        "</p:txBody></p:sp>"
    )


def _pptx_slide_xml(shapes) -> str:
    parts = []
    for kind, payload in shapes:
        if kind == "table":
            parts.append(_pptx_table_xml(payload))
        elif kind == "text":
            parts.append(_pptx_textbox_xml(payload))
        else:
            raise ValueError(f"unknown pptx slide shape kind: {kind!r}")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:a="{_PPTX_A_NS}" xmlns:r="{_PPTX_R_NS}" xmlns:p="{_PPTX_P_NS}">'
        f"<p:cSld><p:spTree>{''.join(parts)}</p:spTree></p:cSld></p:sld>"
    )


def _pptx_content_types_xml(n_slides: int) -> str:
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, n_slides + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        f"{overrides}</Types>"
    )


def _pptx_presentation_xml(n_slides: int) -> str:
    sld_ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i}"/>' for i in range(1, n_slides + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:a="{_PPTX_A_NS}" xmlns:r="{_PPTX_R_NS}" xmlns:p="{_PPTX_P_NS}">'
        f"<p:sldIdLst>{sld_ids}</p:sldIdLst>"
        '<p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>'
        "</p:presentation>"
    )


def _pptx_presentation_rels_xml(n_slides: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{i}.xml"/>'
        for i in range(1, n_slides + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


@pytest.fixture
def make_pptx():
    """合成最小 .pptx 的工厂（纯 zipfile，不依赖 python-pptx）。

    slides 为有序列表，每项是该页的 shape 列表；shape 为 ('table', rows) 或
    ('text', str)。rows 为 list[list[cell]]，cell 为 str 或
    {'text','gridspan','rowspan','hmerge','vmerge','fill'} dict（造合并 / 着色表用，见
    _pptx_tc_xml）。返回 build(path, slides)。
    """

    def _build(path, slides):
        n = len(slides)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _pptx_content_types_xml(n))
            z.writestr("_rels/.rels", _PPTX_ROOT_RELS)
            z.writestr("ppt/presentation.xml", _pptx_presentation_xml(n))
            z.writestr("ppt/_rels/presentation.xml.rels", _pptx_presentation_rels_xml(n))
            for i, shapes in enumerate(slides, start=1):
                z.writestr(f"ppt/slides/slide{i}.xml", _pptx_slide_xml(shapes))
                z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _PPTX_EMPTY_RELS)
        return path

    return _build
