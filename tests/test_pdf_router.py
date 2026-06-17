"""PDF 分诊路由器测试（二期 PDF 线，TDD 红色阶段）。

只验证外部行为：给定 5 个合成 PDF fixture 与 pdf_ground_truth.json，断言
classify_page / route 的对外输出（PageInfo / RoutingDecision 字段），不触碰
任何内部实现细节。

覆盖 PRD user stories：
    #8  PDF 进管线被自动分诊（数字型/扫描型/OCR型/混合型）并按页路由到对应抽取器。
    #9  数字型 PDF 表格定位锚点前置——逐页 kind 分类是「页→数字管线」的前提条件。
    #11 PowerPoint 导出 PDF 被识别并标记 ask_for_pptx（原生优先）。
    #14 宁可少抽不抽错：损坏/加密 PDF 判为 unreadable 且不抛异常，不污染下游。
    #18/#27 file_hash 作为版本血缘 / 审计回溯锚点，确定性可复算。

红色预期：所有调用 classify_page / route 的用例因 stub raise NotImplementedError
而 FAIL；纯字段/常量断言（dataclass 结构、阈值常量）可 PASS。收集成功、无 import error。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import pypdfium2 as pdfium

from ragspine.extraction.routing.pdf_router import (
    IMG_COVER_SCAN,
    PAGE_DIGITAL,
    PAGE_IMG_SCAN,
    PAGE_OCR_SCAN,
    PIPELINE_DIGITAL,
    PIPELINE_SCANNED,
    TEXT_MIN_CHARS,
    VERDICT_DIGITAL,
    VERDICT_MIXED,
    VERDICT_OCR_SCAN,
    VERDICT_SCANNED,
    VERDICT_UNREADABLE,
    PageInfo,
    RoutingDecision,
    classify_page,
    route,
)


# ---------------------------------------------------------------------------
# 辅助：把 fixture 文件名映射到对应 path fixture，便于参数化遍历五个文件
# ---------------------------------------------------------------------------

ALL_FIXTURE_NAMES = (
    "digital.pdf",
    "scanned.pdf",
    "ocr_scan.pdf",
    "mixed.pdf",
    "ppt_export.pdf",
)


def _path_for(name, paths) -> object:
    return paths[name]


@pytest.fixture
def fixture_paths(
    digital_pdf_path,
    scanned_pdf_path,
    ocr_scan_pdf_path,
    mixed_pdf_path,
    ppt_export_pdf_path,
):
    """文件名 -> 绝对路径 的索引（参数化遍历 5 个 fixture 用）。"""
    return {
        "digital.pdf": digital_pdf_path,
        "scanned.pdf": scanned_pdf_path,
        "ocr_scan.pdf": ocr_scan_pdf_path,
        "mixed.pdf": mixed_pdf_path,
        "ppt_export.pdf": ppt_export_pdf_path,
    }


# ===========================================================================
# 契约常量与 dataclass 结构（不调用行为函数，预期 PASS——锁定冻结接口）
# ===========================================================================

def test_threshold_constants_match_prototype():
    """story #8 —— 阈值常量沿用 scripts/classify_pdfs.py，库化前后规则一致。"""
    assert TEXT_MIN_CHARS == 50
    assert IMG_COVER_SCAN == 0.55


def test_pipeline_constants_distinct():
    """story #8 —— 两条路由管线名固定且互异（数字 vs 扫描）。"""
    assert PIPELINE_DIGITAL == "digital_extractor"
    assert PIPELINE_SCANNED == "scanned_extractor"
    assert PIPELINE_DIGITAL != PIPELINE_SCANNED


def test_routing_decision_defaults_are_safe():
    """story #14 —— RoutingDecision 默认值「保守」：未知前判 unreadable、容器空。"""
    d = RoutingDecision(path="x.pdf")
    assert d.verdict == VERDICT_UNREADABLE
    assert d.pages == []
    assert d.channel_plan == {}
    assert d.ask_for_pptx is False
    assert d.origin_meta == ""
    assert d.error is None
    assert d.file_hash is None


def test_routing_decision_independent_mutable_defaults():
    """story #8 —— 两个 RoutingDecision 的 pages/channel_plan 不共享（field default_factory）。"""
    a = RoutingDecision(path="a.pdf")
    b = RoutingDecision(path="b.pdf")
    a.pages.append(PageInfo(1, PAGE_DIGITAL, 100, 0.1, 0))
    a.channel_plan[1] = PIPELINE_DIGITAL
    assert b.pages == []
    assert b.channel_plan == {}


def test_page_info_holds_signals():
    """story #9 —— PageInfo 承载逐页信号（页号/类别/字符数/覆盖率/矢量数）。"""
    p = PageInfo(page_no=2, kind=PAGE_DIGITAL, chars=120, img_cover=0.12, vector_paths=8)
    assert p.page_no == 2
    assert p.kind == PAGE_DIGITAL
    assert p.chars == 120
    assert p.img_cover == 0.12
    assert p.vector_paths == 8


# ===========================================================================
# story #8 —— route() 五个 fixture 的整文件 verdict 全对
# ===========================================================================

@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_route_verdict_matches_ground_truth(name, fixture_paths, pdf_ground_truth):
    """story #8 —— 每个 fixture 的 route().verdict 与 ground truth 完全一致。"""
    path = fixture_paths[name]
    expected = pdf_ground_truth["files"][name]["verdict"]
    assert route(path).verdict == expected


def test_route_digital_verdict(digital_pdf_path):
    """story #8 —— digital.pdf：双数字页 -> verdict='digital'。"""
    assert route(digital_pdf_path).verdict == VERDICT_DIGITAL


def test_route_scanned_verdict(scanned_pdf_path):
    """story #8 —— scanned.pdf：整页位图、无文本层 -> verdict='scanned'。"""
    assert route(scanned_pdf_path).verdict == VERDICT_SCANNED


def test_route_ocr_scan_verdict(ocr_scan_pdf_path):
    """story #8 —— ocr_scan.pdf：位图 + 隐形文本层 -> verdict='ocr_scan'。"""
    assert route(ocr_scan_pdf_path).verdict == VERDICT_OCR_SCAN


def test_route_mixed_verdict(mixed_pdf_path):
    """story #8 —— mixed.pdf：数字+扫描混杂 -> verdict='mixed'。"""
    assert route(mixed_pdf_path).verdict == VERDICT_MIXED


def test_route_ppt_export_verdict(ppt_export_pdf_path):
    """story #11 —— ppt_export.pdf 内容是数字页，verdict 仍为 'digital'（不被元数据改判）。"""
    assert route(ppt_export_pdf_path).verdict == VERDICT_DIGITAL


# ===========================================================================
# story #8 / #9 —— 逐页 kind 分类（含 mixed 的逐页形态）
# ===========================================================================

@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_route_page_kinds_match_ground_truth(name, fixture_paths, pdf_ground_truth):
    """story #9 —— 每个 fixture 的逐页 PageInfo.kind 序列与 ground truth 对齐。"""
    path = fixture_paths[name]
    expected_kinds = pdf_ground_truth["files"][name]["page_kinds"]
    pages = route(path).pages
    assert [p.kind for p in pages] == expected_kinds


@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_route_page_count_matches_ground_truth(name, fixture_paths, pdf_ground_truth):
    """story #8 —— pages 长度等于真值页数（不漏页不多页）。"""
    path = fixture_paths[name]
    expected_n = len(pdf_ground_truth["files"][name]["page_kinds"])
    assert len(route(path).pages) == expected_n


@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_route_page_numbers_are_1based_sequential(name, fixture_paths):
    """story #9 —— page_no 为 1-based 且连续递增（定位锚点前置：页号即锚点根）。"""
    pages = route(fixture_paths[name]).pages
    assert [p.page_no for p in pages] == list(range(1, len(pages) + 1))


def test_mixed_page_kinds_digital_then_scan(mixed_pdf_path):
    """story #8 —— mixed.pdf 逐页形态：前 2 页 digital，后 2 页 img_scan。"""
    pages = route(mixed_pdf_path).pages
    assert [p.kind for p in pages] == [
        PAGE_DIGITAL,
        PAGE_DIGITAL,
        PAGE_IMG_SCAN,
        PAGE_IMG_SCAN,
    ]


def test_ocr_scan_pages_classified_ocr(ocr_scan_pdf_path):
    """story #8 —— ocr_scan.pdf 每页都判为 'ocr_scan'（文本多 + 高覆盖，边界形态）。"""
    pages = route(ocr_scan_pdf_path).pages
    assert pages
    assert all(p.kind == PAGE_OCR_SCAN for p in pages)


# ===========================================================================
# story #8 —— 混合型逐页路由计划 channel_plan
# ===========================================================================

def test_mixed_channel_plan_routes_per_page(mixed_pdf_path, pdf_ground_truth):
    """story #8 —— mixed.pdf 的 channel_plan 按页分流：数字页->digital、扫描页->scanned。"""
    expected = {
        int(k): v
        for k, v in pdf_ground_truth["files"]["mixed.pdf"]["channel_plan"].items()
    }
    assert route(mixed_pdf_path).channel_plan == expected


def test_mixed_channel_plan_explicit_mapping(mixed_pdf_path):
    """story #8 —— 逐页路由目标显式正确：页 1/2->digital_extractor，页 3/4->scanned_extractor。"""
    plan = route(mixed_pdf_path).channel_plan
    assert plan[1] == PIPELINE_DIGITAL
    assert plan[2] == PIPELINE_DIGITAL
    assert plan[3] == PIPELINE_SCANNED
    assert plan[4] == PIPELINE_SCANNED


def test_mixed_channel_plan_keys_are_ints(mixed_pdf_path):
    """story #8 —— channel_plan 键是 int 页号（与 PageInfo.page_no 同型，便于消费）。"""
    plan = route(mixed_pdf_path).channel_plan
    assert plan
    assert all(isinstance(k, int) for k in plan)


def test_mixed_channel_plan_covers_every_page(mixed_pdf_path):
    """story #8 —— 混合型每一页都被分流，无遗漏页（键集合 == 全部页号）。"""
    decision = route(mixed_pdf_path)
    page_nos = {p.page_no for p in decision.pages}
    assert set(decision.channel_plan.keys()) == page_nos


def test_mixed_channel_plan_targets_are_valid_pipelines(mixed_pdf_path):
    """story #8 —— channel_plan 取值只能是两条合法管线之一（不出现野值）。"""
    plan = route(mixed_pdf_path).channel_plan
    assert set(plan.values()) <= {PIPELINE_DIGITAL, PIPELINE_SCANNED}


# ===========================================================================
# story #11 —— ask_for_pptx：仅 ppt_export.pdf 为 True，其余为 False
# ===========================================================================

def test_ppt_export_asks_for_pptx(ppt_export_pdf_path):
    """story #11 —— PowerPoint 导出 PDF（producer 命中）-> ask_for_pptx=True。"""
    assert route(ppt_export_pdf_path).ask_for_pptx is True


@pytest.mark.parametrize(
    "name", ("digital.pdf", "scanned.pdf", "ocr_scan.pdf", "mixed.pdf")
)
def test_non_ppt_files_do_not_ask_for_pptx(name, fixture_paths):
    """story #11 —— 非 PPT 导出文件一律 ask_for_pptx=False（不误报索取原生件）。"""
    assert route(fixture_paths[name]).ask_for_pptx is False


@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_ask_for_pptx_matches_ground_truth(name, fixture_paths, pdf_ground_truth):
    """story #11 —— 五个 fixture 的 ask_for_pptx 全部与 ground truth 一致。"""
    expected = pdf_ground_truth["files"][name]["ask_for_pptx"]
    assert route(fixture_paths[name]).ask_for_pptx is expected


def test_ppt_export_origin_meta_carries_producer(ppt_export_pdf_path, pdf_ground_truth):
    """story #11 —— origin_meta 携带生产者元数据，含 'PowerPoint'（索取依据可追溯）。"""
    expected_producer = pdf_ground_truth["files"]["ppt_export.pdf"]["producer"]
    meta = route(ppt_export_pdf_path).origin_meta
    assert "PowerPoint" in meta
    # producer 元数据原文应体现在 origin_meta 中
    assert expected_producer in meta


# ===========================================================================
# story #8 —— file_hash 血缘：非空、确定性、跨文件不同
# ===========================================================================

def test_file_hash_is_nonempty_string(digital_pdf_path):
    """story #18 —— route().file_hash 是非空字符串（版本血缘锚点）。"""
    h = route(digital_pdf_path).file_hash
    assert isinstance(h, str)
    assert len(h) > 0


def test_file_hash_is_deterministic(digital_pdf_path):
    """story #18 —— 同一文件两次 route 的 file_hash 一致（确定性，可复算审计）。"""
    assert route(digital_pdf_path).file_hash == route(digital_pdf_path).file_hash


def test_file_hash_differs_across_distinct_files(digital_pdf_path, scanned_pdf_path):
    """story #18 —— 内容不同的文件 file_hash 不同（版本/来源可区分）。"""
    assert route(digital_pdf_path).file_hash != route(scanned_pdf_path).file_hash


@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_every_fixture_gets_file_hash(name, fixture_paths):
    """story #27 —— 每个可读 fixture 都拿到 file_hash（审计回溯不留空）。"""
    h = route(fixture_paths[name]).file_hash
    assert h
    assert isinstance(h, str)


# ===========================================================================
# story #8 / #14 —— 损坏 / 不可读文件：verdict='unreadable'，绝不抛异常
# ===========================================================================

def test_corrupted_file_is_unreadable_not_raising(tmp_path):
    """story #14 —— 非 PDF 字节的损坏文件 -> verdict='unreadable'，不抛异常。"""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"this is definitely not a valid PDF file \x00\x01\x02 garbage")
    decision = route(bad)  # 不得抛异常
    assert decision.verdict == VERDICT_UNREADABLE


def test_corrupted_file_records_error_and_empty_pages(tmp_path):
    """story #14 —— 损坏文件：error 记录原因、pages 为空、channel_plan 为空。"""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%not-a-pdf% \xff\xfe random bytes")
    decision = route(bad)
    assert decision.error is not None
    assert decision.pages == []
    assert decision.channel_plan == {}


def test_empty_file_is_unreadable(tmp_path):
    """story #14 —— 空文件（0 字节）-> unreadable，不抛异常（边界）。"""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    decision = route(empty)
    assert decision.verdict == VERDICT_UNREADABLE
    assert decision.pages == []


def test_nonexistent_file_is_unreadable(tmp_path):
    """story #14 —— 不存在的路径 -> unreadable，不抛异常（普查不能中断）。"""
    missing = tmp_path / "does_not_exist.pdf"
    decision = route(missing)
    assert decision.verdict == VERDICT_UNREADABLE
    assert decision.error is not None


def test_truncated_pdf_header_only_is_unreadable(tmp_path):
    """story #14 —— 只有 PDF 头、无 body 的截断文件 -> unreadable，不抛异常（刁钻）。"""
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.7\n")  # 仅文件头，结构残缺
    decision = route(truncated)  # 不得抛异常
    assert decision.verdict == VERDICT_UNREADABLE


def test_unreadable_file_not_asking_for_pptx(tmp_path):
    """story #11/#14 —— 不可读文件不应误报 ask_for_pptx（无元数据可信）。"""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"garbage-not-pdf")
    assert route(bad).ask_for_pptx is False


# ===========================================================================
# story #8 —— RoutingDecision 的 path 字段回填源路径
# ===========================================================================

def test_route_decision_carries_path(digital_pdf_path):
    """story #8 —— RoutingDecision.path 携带源文件路径（血缘根，str 化可比对）。"""
    decision = route(digital_pdf_path)
    assert str(digital_pdf_path) in str(decision.path)


@pytest.mark.parametrize("name", ALL_FIXTURE_NAMES)
def test_route_returns_routing_decision_type(name, fixture_paths):
    """story #8 —— route 始终返回 RoutingDecision 实例（对外类型契约稳定）。"""
    assert isinstance(route(fixture_paths[name]), RoutingDecision)


def test_pure_digital_has_no_channel_plan_or_empty(digital_pdf_path):
    """story #8 —— 纯数字型 verdict='digital' 不是混合，channel_plan 不强制逐页（边界）。

    只断言外部可观察的弱契约：digital 文件的 verdict 不是 'mixed'，
    且如填了 channel_plan，其取值仍只能是合法管线（不出现野值）。
    """
    decision = route(digital_pdf_path)
    assert decision.verdict != VERDICT_MIXED
    assert set(decision.channel_plan.values()) <= {PIPELINE_DIGITAL, PIPELINE_SCANNED}


# ===========================================================================
# story #9 —— classify_page() 直接作用于真实 pypdfium2 页对象（逐页信号）
# ===========================================================================

def test_classify_page_digital_table_page(digital_pdf_path):
    """story #9 —— digital.pdf 第 1 页（表格，文本多+低覆盖）-> kind='digital'。"""
    doc = pdfium.PdfDocument(str(digital_pdf_path))
    try:
        info = classify_page(doc[0])
    finally:
        doc.close()
    assert info.kind == PAGE_DIGITAL
    assert info.chars >= TEXT_MIN_CHARS
    assert info.img_cover < IMG_COVER_SCAN


def test_classify_page_scanned_page(scanned_pdf_path):
    """story #9 —— scanned.pdf 任一页（无文本+高覆盖）-> kind='img_scan'。"""
    doc = pdfium.PdfDocument(str(scanned_pdf_path))
    try:
        info = classify_page(doc[0])
    finally:
        doc.close()
    assert info.kind == PAGE_IMG_SCAN
    assert info.chars < TEXT_MIN_CHARS
    assert info.img_cover >= IMG_COVER_SCAN


def test_classify_page_ocr_scan_page(ocr_scan_pdf_path):
    """story #9 —— ocr_scan.pdf 页（隐形文本多+高覆盖）-> kind='ocr_scan'（刁钻边界）。"""
    doc = pdfium.PdfDocument(str(ocr_scan_pdf_path))
    try:
        info = classify_page(doc[0])
    finally:
        doc.close()
    assert info.kind == PAGE_OCR_SCAN
    assert info.chars >= TEXT_MIN_CHARS
    assert info.img_cover >= IMG_COVER_SCAN


def test_classify_page_returns_page_info(digital_pdf_path):
    """story #9 —— classify_page 返回 PageInfo，img_cover 在 [0,1]、chars/vector_paths 非负。"""
    doc = pdfium.PdfDocument(str(digital_pdf_path))
    try:
        info = classify_page(doc[0])
    finally:
        doc.close()
    assert isinstance(info, PageInfo)
    assert 0.0 <= info.img_cover <= 1.0
    assert info.chars >= 0
    assert info.vector_paths >= 0


def test_classify_page_kind_is_valid_label(mixed_pdf_path):
    """story #9 —— 任一页的 kind 取值落在四个合法标签内（不出现野值）。"""
    valid = {PAGE_DIGITAL, PAGE_OCR_SCAN, PAGE_IMG_SCAN, "low_text"}
    doc = pdfium.PdfDocument(str(mixed_pdf_path))
    try:
        kinds = [classify_page(p).kind for p in doc]
    finally:
        doc.close()
    assert kinds
    assert set(kinds) <= valid
