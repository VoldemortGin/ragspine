"""Extractor 注册表测试（mime/格式 -> Extractor 查表分发，TDD 红色阶段）。

只验证缝的对外行为：
    - 新登记一个 mime -> 它的抽取器经注册表分发，且【不碰任何路由代码】（router 零改动）。
    - 未登记的 mime -> 清晰的、类型化的 UnsupportedFormatError（非裸 KeyError）。
    - 内置格式（pdf / pptx / xlsx）解析到各自的 StyledGrid 抽取器（薄壳包装，零行为变化）。
    - mime 归一化（大小写 / 留白不敏感）。

红色预期：ragspine.extraction.registry 尚不存在，import 即 collection ERROR（红色、逐条隔离）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.ir import StyledCell, StyledGrid
from ragspine.extraction.registry import (
    Extractor,
    UnsupportedFormatError,
    get_extractor,
    register_extractor,
    registered_mimes,
)

PDF_MIME = "application/pdf"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _DummyExtractor:
    """in-test 抽取器：产出一张带血缘的 StyledGrid（验证新 mime 经注册表分发，且不碰 router）。"""

    def extract(self, path: str) -> list[StyledGrid]:
        grid = StyledGrid(sheet="dummy1", source_doc_id=str(path))
        grid.cells["R1C1"] = StyledCell(value="hi", cell_ref="R1C1")
        return [grid]


def test_register_new_mime_dispatches_without_touching_router():
    """新登记一个 mime -> get_extractor 分发到它的抽取器，全程不改任何路由代码。"""
    mime = "application/x-dummy-format"
    register_extractor(mime, _DummyExtractor())
    extractor = get_extractor(mime)
    assert isinstance(extractor, Extractor)  # runtime_checkable 结构匹配
    grids = extractor.extract("memo.dummy")
    assert [g.sheet for g in grids] == ["dummy1"]
    # provenance 穿过缝：source_doc_id 原样保留
    assert grids[0].source_doc_id == "memo.dummy"


def test_unregistered_mime_raises_typed_unsupported_error():
    """未登记的 mime -> UnsupportedFormatError（类型化，便于上游精确捕获）。"""
    with pytest.raises(UnsupportedFormatError):
        get_extractor("application/x-not-registered")


def test_unsupported_error_is_typed_and_actionable():
    """UnsupportedFormatError 是 LookupError 子类，报错带上未知 mime（非裸 KeyError、可执行）。"""
    assert issubclass(UnsupportedFormatError, LookupError)
    with pytest.raises(UnsupportedFormatError) as exc:
        get_extractor("application/x-zzz")
    assert "application/x-zzz" in str(exc.value)


def test_builtin_mimes_resolve_to_grid_extractors():
    """内置格式 pdf / pptx / xlsx 各解析到其 StyledGrid 抽取器（无需改 router）。"""
    assert isinstance(get_extractor(PDF_MIME), Extractor)
    assert isinstance(get_extractor(PPTX_MIME), Extractor)
    assert isinstance(get_extractor(XLSX_MIME), Extractor)
    assert {PDF_MIME, PPTX_MIME, XLSX_MIME} <= registered_mimes()


def test_mime_normalization_case_and_whitespace_insensitive():
    """mime 归一化：大小写 / 首尾留白不敏感。"""
    assert isinstance(get_extractor("  Application/PDF  "), Extractor)


def test_register_rejects_non_extractor():
    """register_extractor 拒绝不实现 Extractor.extract 的对象——由包级 beartype 运行时契约强制。"""
    from beartype.roar import BeartypeCallHintParamViolation

    with pytest.raises(BeartypeCallHintParamViolation):
        register_extractor("application/x-bad", object())
