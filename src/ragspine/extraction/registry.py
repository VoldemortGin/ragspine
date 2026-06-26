"""Extractor 注册表缝：mime/格式 -> Extractor 的查表分发（落地 docs/prd-breadth-via-adapters.md 的 Extractor formalize 行）。

把已存在的各格式抽取器（pdf 数字型 / pptx / xlsx，均产出 StyledGrid IR）收敛到一个
@runtime_checkable Extractor Protocol 后面，按 mime/格式登记进注册表。get_extractor(mime)
查表分发，使「新增一个 mime -> 它的抽取器」无需改任何路由代码（register_extractor 运行时登记
即可，pdf_router 的逐页数字/扫描分诊保持不变）；未登记的 mime 抛清晰、类型化的
UnsupportedFormatError（LookupError 子类，非裸 KeyError）。

零行为变化（本增量的头条）：本模块只【薄壳包装】既有 extract_grids 函数，不改写、不改进任何
抽取器。各格式的重依赖（docling / python-pptx / openpyxl）留在对应 extractor 模块内，仅在
get_extractor 真正解析到该格式时才随模块 import 进来——故 import 本注册表零额外 SDK 加载。
"""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ragspine.extraction.ir import StyledGrid

# 各内置格式的规范 mime。
PDF_MIME = "application/pdf"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# W3c 备选选择项：.pptx 默认 mime/后缀仍分发到 python-pptx 的 pptx_styled（保 color/chart/
# note）；显式用本选择项才切到家族 pptspine（更富表合并 gridSpan/rowSpan/hMerge/vMerge）。
# 这是 additive opt-in —— 默认 .pptx 分发零行为变化（铁律），pptspine 经此独立 key 选用。
PPTX_PPTSPINE_SELECTOR = "pptx+pptspine"


class UnsupportedFormatError(LookupError):
    """请求一个未登记 mime/格式的抽取器时抛出。

    继承 LookupError（而非裸 KeyError），便于上游精确捕获「格式不支持」这一语义，
    且 except KeyError 不会误吞它。
    """


@runtime_checkable
class Extractor(Protocol):
    """抽取器缝的最小结构接口：一个文件 -> StyledGrid IR 列表（承载 source_doc_id + 单元格定位血缘）。

    core 只 import 这个 Protocol；各格式的重依赖（docling / python-pptx / openpyxl）留在对应
    extractor 模块内（顶层或函数体）延迟加载。后端特定旋钮不进核心 Protocol。
    """

    def extract(self, path: str | Path) -> list[StyledGrid]: ...


class _FunctionExtractor:
    """把一个模块级 extract_grids(path) -> list[StyledGrid] 函数薄壳成 Extractor 实例。

    只做适配、不改语义：extract 原样委托给被包装函数（逐位等价，零行为变化）。name 标识被包装的
    内置抽取器，便于注册表自检与排错。
    """

    def __init__(
        self, func: Callable[[str | Path], list[StyledGrid]], *, name: str = ""
    ) -> None:
        self._func = func
        self.name = name

    def extract(self, path: str | Path) -> list[StyledGrid]:
        return self._func(path)


# ---------------------------------------------------------------------------
# 注册表两层（范式同 store.py 的 loader 表）：
#   _BUILTIN_LOADERS —— 内置 mime -> 惰性 loader（被调用时才 import 对应 extractor 模块、
#                       返回一个薄壳 Extractor 实例）。故 import 本模块零额外 SDK 加载。
#   _REGISTERED      —— 运行时显式登记的 mime -> Extractor 实例（in-test 的新格式、第三方在
#                       import 时调用 register_extractor 登记的格式）；优先级高于内置。
# 别名（规范 mime + 小写后缀）共指同一 loader；归一化大小写 / 留白不敏感。
# ---------------------------------------------------------------------------
def _load_pdf_digital() -> Extractor:
    from ragspine.extraction.extractors.pdf_digital_extractor import extract_grids

    return _FunctionExtractor(extract_grids, name="pdf_digital")


def _load_pptx_styled() -> Extractor:
    from ragspine.extraction.extractors.pptx_styled_extractor import extract_grids

    return _FunctionExtractor(extract_grids, name="pptx_styled")


def _load_xlsx_styled() -> Extractor:
    from ragspine.extraction.extractors.xlsx_styled_extractor import extract_grids

    return _FunctionExtractor(extract_grids, name="xlsx_styled")


def _load_docspine() -> Extractor:
    from ragspine.extraction.extractors.docspine_extractor import extract_grids

    return _FunctionExtractor(extract_grids, name="docspine")


def _load_pptspine() -> Extractor:
    from ragspine.extraction.extractors.pptspine_extractor import extract_grids

    return _FunctionExtractor(extract_grids, name="pptspine")


_BUILTIN_LOADERS: dict[str, Callable[[], Extractor]] = {
    PDF_MIME: _load_pdf_digital,
    ".pdf": _load_pdf_digital,
    # 默认 .pptx / PPTX_MIME -> python-pptx 的 pptx_styled（保 color/chart/note，铁律不回归）。
    PPTX_MIME: _load_pptx_styled,
    ".pptx": _load_pptx_styled,
    # W3c opt-in 备选：'pptx+pptspine' -> 家族 pptspine（更富表合并）。默认分发不受影响。
    PPTX_PPTSPINE_SELECTOR: _load_pptspine,
    XLSX_MIME: _load_xlsx_styled,
    ".xlsx": _load_xlsx_styled,
    DOCX_MIME: _load_docspine,
    ".docx": _load_docspine,
}

_REGISTERED: dict[str, Extractor] = {}


def _normalize(mime: str) -> str:
    """mime/格式键归一化：首尾 strip + 小写（大小写 / 留白不敏感）。"""
    return mime.strip().lower()


def register_extractor(mime: str, extractor: Extractor) -> None:
    """登记 mime -> Extractor，使该格式无需改任何路由代码即可被 get_extractor 分发。

    运行时登记的格式优先于内置；extractor 必须实现 Extractor.extract——由包级 beartype 运行时
    契约（ADR 0004）在调用边界对 Extractor 注解强制，传入不符的对象即被拒（非 Extractor 进不来）。
    第三方包可在自身 import 时调用本函数登记新格式（无需核心 PR）。
    """
    _REGISTERED[_normalize(mime)] = extractor


def get_extractor(mime: str) -> Extractor:
    """按 mime/格式查表分发到对应 Extractor。

    解析顺序：运行时登记表（_REGISTERED）优先，再回落到内置惰性 loader。两者皆不命中 ->
    UnsupportedFormatError（带上未知 mime 与已登记列表，类型化、可执行）。
    """
    key = _normalize(mime)
    extractor = _REGISTERED.get(key)
    if extractor is not None:
        return extractor
    loader = _BUILTIN_LOADERS.get(key)
    if loader is not None:
        return loader()
    known = " / ".join(sorted(registered_mimes())) or "无"
    raise UnsupportedFormatError(
        f"没有登记的抽取器可处理格式 {mime!r}"
        f"（已登记：{known}；"
        f"用 register_extractor(mime, extractor) 登记一个新格式即可分发，无需改路由代码）"
    )


def registered_mimes() -> set[str]:
    """当前所有可分发的 mime/格式键（内置 + 运行时登记）。不触发任何 extractor 模块 import。"""
    return set(_BUILTIN_LOADERS) | set(_REGISTERED)
