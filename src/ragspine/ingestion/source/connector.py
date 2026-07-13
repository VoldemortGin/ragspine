"""SourceConnector 缝：可插拔的「原始文档入口」+ 入口处绑定 provenance 不变量（落地 docs/prd-breadth-via-adapters.md）。

这条缝只担一件事：**回答「原始文档从哪进 ingestion」**——把 S3 / Drive / Notion / HTTP later
都收敛到一个 Protocol 后面。它【不】做抽取 / 切块 / 入库（那些在 ingestion 下游），只惰性产出
带齐血缘的 RawDoc，让 provenance 不变量从【入口】就被 tests/conformance 绑死：任何 connector
（含第三方）只要登记进 conformance，就必须证明每个 RawDoc 都带非空 source_doc_id + locator。

FilesystemConnector：零三方依赖、确定性的默认实现，pathlib 递归走盘，source_doc_id 取
path.name（与 narrative_ingest 今日 `doc_id = path.name` 识别文件的口径【逐字一致】），让
`pip install ragspine` 的精简默认（ADR 0009）就能枚举本地语料、端到端跑。

provenance 如何被【实现真正保证】（非注释）：
- 每个 RawDoc 携带 source_doc_id（血缘根，= 文件名，口径同 StyledGrid.source_doc_id /
  fact.source_doc_id）与 locator（原始位置串：文件系统下 = 文件路径；S3 下 = 's3://…' URI）。
- FilesystemConnector 从真实路径派生这两者，既不臆造也不丢；conformance 的 provenance pack
  对【每个注册 connector】断言二者非空，并以一个故意丢血缘的 stub 反证该断言非空泛。
"""

import hashlib
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 store.VECTOR_STORE_ENV）。
SOURCE_CONNECTOR_ENV = "RAGSPINE_SOURCE_CONNECTOR"

# 第三方连接器自动发现的 entry-point group：一个包在此 group 下注册一行
# （pyproject `[project.entry-points."ragspine.source_connectors"]`），make_source_connector
# 就能按名字选中它——核心零改动、零 SDK import（范式同 store.VECTOR_STORE_ENTRY_POINT_GROUP）。
SOURCE_CONNECTOR_ENTRY_POINT_GROUP = "ragspine.source_connectors"


@dataclass(frozen=True)
class RawDoc:
    """一份刚进入 ingestion 的原始文档 + 其身份 + 下游路由所需元数据。承载血缘。

    字段语义约定：
        source_doc_id: 源文档标识（血缘根，= 文件名，口径同 StyledGrid.source_doc_id /
                       fact.source_doc_id）；下游抽取产出的每个单元都继承它。
        locator:       原始位置串（provenance：文档【从哪来】）。文件系统下 = 文件路径串；
                       S3 下 = 's3://bucket/key'；HTTP 下 = URL。非空即可回溯到源。
        content:       原始字节（二进制安全：pptx/pdf 等本就是二进制；文本源自行 encode）。
        content_type:  格式提示，供下游路由到对应抽取器。文件系统下 = 小写后缀（'.pdf' / '.pptx'）。
        metadata:      下游需要的附加元数据（如 file_hash 版本血缘）；默认空。frozen 保证入流后不被就地改写。
    """

    source_doc_id: str
    locator: str
    content: bytes
    content_type: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class SourceConnector(Protocol):
    """原始文档入口缝的最小结构接口（core 只 import 这个 Protocol，不 import 任何 SDK）。

    刻意保持在 provenance 不变量所需的最低公约数：iter_documents() 惰性产出 RawDoc 流。后端特定
    旋钮（S3 分页、Drive OAuth、Notion 游标等）属各 adapter 自己的配置，不进核心 Protocol。
    """

    def iter_documents(self) -> Iterable[RawDoc]: ...


class FilesystemConnector:
    """零依赖、确定性的本地文件系统默认实现：pathlib 递归走盘产出 RawDoc。

    枚举语义（忽略口径与 narrative_ingest._resolve_inputs 一致）：
        - 递归收 root 下所有文件；忽略隐藏文件（'.' 前缀）与 Office 临时文件（'~$' 前缀）。
        - 可选 suffixes 过滤（大小写不敏感的后缀集合，如 {'.pdf', '.pptx'}）；None 表示不限格式。
        - **确定性顺序**：按相对 root 的 POSIX 路径升序排序，跨平台稳定、不受 OS 遍历序影响。
    source_doc_id 取 path.name（与 narrative_ingest 今日 `doc_id = path.name` 逐字一致）；
    locator 取文件路径串；content_type 取小写后缀；metadata 带 file_hash（sha256，口径同
    extraction.compute_file_hash——同一文件内容得同一 hash，作版本血缘）。
    """

    def __init__(self, root: str | Path, *, suffixes: Iterable[str] | None = None) -> None:
        self.root = Path(root)
        self.suffixes = None if suffixes is None else {s.lower() for s in suffixes}

    def iter_documents(self) -> Iterator[RawDoc]:
        """惰性产出 root 下每个文件的 RawDoc（确定性顺序；缺目录抛 NotADirectoryError）。"""
        if not self.root.is_dir():
            raise NotADirectoryError(f"FilesystemConnector.root 不是目录：{self.root}")
        files = [
            f
            for f in self.root.rglob("*")
            if f.is_file()
            and not f.name.startswith(("~$", "."))
            and (self.suffixes is None or f.suffix.lower() in self.suffixes)
        ]
        # 确定性顺序：按相对 root 的 POSIX 路径升序（跨平台一致，不依赖底层文件系统遍历序）。
        files.sort(key=lambda p: p.relative_to(self.root).as_posix())
        for path in files:
            content = path.read_bytes()
            yield RawDoc(
                source_doc_id=path.name,
                locator=str(path),
                content=content,
                content_type=path.suffix.lower(),
                metadata={"file_hash": hashlib.sha256(content).hexdigest()},
            )


# ---------------------------------------------------------------------------
# 注册表：内置连接器名字 -> 惰性 loader（返回 SourceConnector【类】，尚不实例化）。范式同 store.py：
# 核心 import 本模块零 SDK；第三方连接器【不】登记此表，而是经 entry-point 自动发现，无需核心 PR。
# 别名共指同一 loader（大小写 / 留白由 make_source_connector 归一化）。
# ---------------------------------------------------------------------------
def _load_filesystem() -> type[SourceConnector]:
    return FilesystemConnector


def _load_in_memory() -> type[SourceConnector]:
    from ragspine.ingestion.source.memory import InMemoryConnector

    return InMemoryConnector


def _load_http() -> type[SourceConnector]:
    # remote.py 在函数体内 import（httpx 在其方法体内再惰性 import）：解析 http/notion 名字
    # 不触发 httpx，离线默认路径零三方依赖。
    from ragspine.ingestion.source.remote import HttpConnector

    return HttpConnector


def _load_notion() -> type[SourceConnector]:
    from ragspine.ingestion.source.remote import NotionConnector

    return NotionConnector


_BUILTIN_LOADERS: dict[str, Any] = {
    "filesystem": _load_filesystem,
    "fs": _load_filesystem,
    "local": _load_filesystem,
    "in_memory": _load_in_memory,
    "memory": _load_in_memory,
    "fixture": _load_in_memory,
    "http": _load_http,
    "https": _load_http,
    "url": _load_http,
    "notion": _load_notion,
}

# 错误信息中展示的内置规范名（别名不重复列出，保持可读）。
_BUILTIN_DISPLAY_NAMES = ("none", "filesystem", "in_memory", "http", "notion")


def _discover_entry_points() -> Sequence[Any]:
    """发现第三方在 SOURCE_CONNECTOR_ENTRY_POINT_GROUP 下注册的 SourceConnector 后端。

    返回若干 EntryPoint（各有 .name 与 .load()）。在函数内 import entry_points，使
    monkeypatch importlib.metadata.entry_points 在测试中生效，也让发现成本只在真正回落时付出。
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=SOURCE_CONNECTOR_ENTRY_POINT_GROUP))


def _resolve_factory(normalized: str) -> Any:
    """归一化后的名字 -> 一个可 **kwargs 调用得到 SourceConnector 的工厂（内置类或 entry-point 目标）。

    先查内置注册表（内置名字优先于同名 entry point，第三方不能劫持内置语义）；未命中再回落到
    entry-point 自动发现，按名字（大小写 / 留白不敏感）匹配后 .load()。两者皆不命中 -> ValueError，
    列出内置 + 已发现的 entry-point 名字。本函数只【解析】不【实例化】。
    """
    loader = _BUILTIN_LOADERS.get(normalized)
    if loader is not None:
        return loader()
    discovered = _discover_entry_points()
    for entry_point in discovered:
        if entry_point.name.strip().lower() == normalized:
            return entry_point.load()
    names = sorted({entry_point.name for entry_point in discovered})
    raise ValueError(
        f"未知 source connector spec：{normalized!r}"
        f"（内置可选 {' / '.join(_BUILTIN_DISPLAY_NAMES)}；"
        f"已发现的 entry-point 后端：{names or '无'}；"
        f"第三方包可在 entry-point group {SOURCE_CONNECTOR_ENTRY_POINT_GROUP!r} 下注册一个连接器）"
    )


def make_source_connector(spec: str | None = None, **kwargs: Any) -> SourceConnector | None:
    """原始文档入口工厂：把「用哪个 connector」从改代码降为一个 spec/env（范式同 make_vector_store）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_SOURCE_CONNECTOR）：
        - None / 'none'                 -> None（未配置连接器）。
        - 'filesystem' / 'fs' / 'local' -> FilesystemConnector（零依赖确定性本地默认；需 kwargs `root=`）。
        - 其余                          -> entry-point 自动发现（第三方包在
          SOURCE_CONNECTOR_ENTRY_POINT_GROUP 下注册即可被选中）；都不命中 -> ValueError 列出可选名字。

    名字经注册表解析（内置 loader 或 entry point），再以 **kwargs 实例化。返回 SourceConnector 实例或 None。
    """
    if spec is None:
        spec = os.environ.get(SOURCE_CONNECTOR_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    factory = _resolve_factory(normalized)
    connector: SourceConnector = factory(**kwargs)
    return connector
