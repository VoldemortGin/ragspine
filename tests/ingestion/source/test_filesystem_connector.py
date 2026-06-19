"""FilesystemConnector 默认实现 + make_source_connector 工厂的行为契约。

SourceConnector 缝的零依赖默认（pathlib 递归走盘）与其 spec/env 选择工厂。provenance 不变量
本身在 tests/conformance/test_source_connector_provenance.py 对【每个注册 connector】绑定；
这里测 FilesystemConnector 的具体语义（忽略规则 / 确定性序 / 内容与 hash / 后缀过滤）与工厂解析。
"""

import hashlib
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.extractors.pptx_styled_extractor import compute_file_hash
from ragspine.ingestion.source.connector import (
    SOURCE_CONNECTOR_ENV,
    SOURCE_CONNECTOR_ENTRY_POINT_GROUP,
    FilesystemConnector,
    RawDoc,
    SourceConnector,
    make_source_connector,
)


@pytest.fixture
def tree(tmp_path):
    """嵌套夹具树：a.pptx / sub/b.pdf / sub/c.txt + 一个隐藏文件 + 一个 Office 临时文件。"""
    (tmp_path / "a.pptx").write_bytes(b"alpha")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.pdf").write_bytes(b"%PDF-bravo")
    (tmp_path / "sub" / "c.txt").write_bytes(b"charlie")
    (tmp_path / ".hidden.pptx").write_bytes(b"nope")
    (tmp_path / "~$temp.pptx").write_bytes(b"nope")
    return tmp_path


def test_structural_conformance_to_protocol(tree):
    """FilesystemConnector 结构化满足 runtime_checkable SourceConnector。"""
    assert isinstance(FilesystemConnector(tree), SourceConnector)


def test_recursive_walk_ignores_hidden_and_office_temp(tree):
    """递归收文件；忽略 '.' 隐藏文件与 '~$' Office 临时文件（口径同 narrative_ingest）。"""
    ids = {d.source_doc_id for d in FilesystemConnector(tree).iter_documents()}
    assert ids == {"a.pptx", "b.pdf", "c.txt"}


def test_deterministic_order_by_relative_posix_path(tree):
    """确定性顺序：按相对 root 的 POSIX 路径升序（a.pptx < sub/b.pdf < sub/c.txt）。"""
    ids = [d.source_doc_id for d in FilesystemConnector(tree).iter_documents()]
    assert ids == ["a.pptx", "b.pdf", "c.txt"]


def test_rawdoc_carries_content_type_and_hash(tree):
    """RawDoc 携带原始字节、小写后缀 content_type，与 file_hash（口径同 compute_file_hash）。"""
    by_id = {d.source_doc_id: d for d in FilesystemConnector(tree).iter_documents()}
    pptx = by_id["a.pptx"]
    assert pptx.content == b"alpha"
    assert pptx.content_type == ".pptx"
    assert pptx.metadata["file_hash"] == hashlib.sha256(b"alpha").hexdigest()
    assert pptx.metadata["file_hash"] == compute_file_hash(tree / "a.pptx")


def test_source_doc_id_is_filename_and_locator_is_path(tree):
    """source_doc_id = 文件名（血缘根，同 narrative `doc_id = path.name`）；locator = 文件路径串。"""
    b = next(d for d in FilesystemConnector(tree).iter_documents() if d.source_doc_id == "b.pdf")
    assert b.source_doc_id == "b.pdf"
    assert b.locator == str(tree / "sub" / "b.pdf")


def test_suffixes_filter_case_insensitive(tree):
    """suffixes 过滤大小写不敏感：只收 .pdf / .pptx 时排除 c.txt。"""
    ids = {d.source_doc_id for d in FilesystemConnector(tree, suffixes={".PDF", ".PPTX"}).iter_documents()}
    assert ids == {"a.pptx", "b.pdf"}


def test_missing_root_raises(tmp_path):
    """root 不是目录时 iter_documents 抛 NotADirectoryError（惰性：构造不抛）。"""
    conn = FilesystemConnector(tmp_path / "nope")
    with pytest.raises(NotADirectoryError):
        list(conn.iter_documents())


def test_rawdoc_is_frozen():
    """RawDoc 冻结：入流后不可就地改写（血缘保真）。"""
    doc = RawDoc(source_doc_id="x", locator="/x", content=b"x")
    with pytest.raises(Exception):
        doc.source_doc_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# make_source_connector 工厂解析
# ---------------------------------------------------------------------------

def test_factory_none_returns_none():
    """spec None / 'none' -> None（未配置连接器）。"""
    assert make_source_connector(None) is None
    assert make_source_connector("none") is None


@pytest.mark.parametrize("spec", ["filesystem", "FileSystem", "  fs ", "local"])
def test_factory_filesystem_aliases(tree, spec):
    """'filesystem' / 'fs' / 'local' 别名（大小写 / 留白不敏感）皆解析到 FilesystemConnector。"""
    conn = make_source_connector(spec, root=tree)
    assert isinstance(conn, FilesystemConnector)
    assert {d.source_doc_id for d in conn.iter_documents()} == {"a.pptx", "b.pdf", "c.txt"}


def test_factory_reads_env(tree, monkeypatch):
    """缺省 spec 时读环境变量 RAGSPINE_SOURCE_CONNECTOR。"""
    monkeypatch.setenv(SOURCE_CONNECTOR_ENV, "filesystem")
    assert isinstance(make_source_connector(root=tree), FilesystemConnector)


def test_factory_unknown_spec_errors():
    """未知 spec -> ValueError，错误信息列出内置可选名字。"""
    with pytest.raises(ValueError, match="filesystem"):
        make_source_connector("notaconnector")


def test_factory_entry_point_discovery(tree, monkeypatch):
    """第三方在 entry-point group 下注册的 connector 可经名字被发现（无核心改动）。"""
    import ragspine.ingestion.source.connector as mod

    class _StubEntryPoint:
        name = "stub_remote"

        def load(self):
            return FilesystemConnector

    def _fake_entry_points(*, group):
        assert group == SOURCE_CONNECTOR_ENTRY_POINT_GROUP
        return [_StubEntryPoint()]

    monkeypatch.setattr("importlib.metadata.entry_points", _fake_entry_points)
    conn = mod.make_source_connector("stub_remote", root=tree)
    assert isinstance(conn, FilesystemConnector)
