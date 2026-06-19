"""SourceConnector 入口处 provenance 不变量绑定（conformance）。

落地 docs/prd-breadth-via-adapters.md「Invariant-binding conformance kit · Provenance
(SourceConnector …)」：把 provenance 从【原始文档入口】就绑死在 SourceConnector 缝上，对
【每个注册 connector】参数化断言。任何 connector（含未来 S3 / Drive / Notion）只要登记进
conftest.SOURCE_CONNECTOR_IMPLS 就必须通过——在入口丢血缘的实现直接 CI 红，而非生产事故。

非空泛证明（同 VectorStore invariants 的「诚实反证」手法）：一个故意丢 source_doc_id /
locator 的 stub connector 喂进同一断言核【必须 FAIL】，证明该断言不是空泛通过。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)


def _assert_provenance_complete(connector) -> int:
    """对一个 SourceConnector 断言：产出非空，且每个 RawDoc 都带非空 source_doc_id + locator。

    返回产出文档数。这是 provenance pack 的【单一判定核】——参数化用例与「反证 stub」共用它，
    确保两者验的是同一条不变量。
    """
    docs = list(connector.iter_documents())
    assert docs, "connector 未产出任何 RawDoc（fixture 树非空，应至少枚举到文件）"
    for doc in docs:
        assert doc.source_doc_id, f"RawDoc 缺 source_doc_id：{doc!r}"
        assert doc.locator, f"RawDoc 缺 locator：{doc!r}"
    return len(docs)


# ===========================================================================
# P · Provenance：每个注册 SourceConnector 的产出都带齐血缘
# ===========================================================================

def test_every_rawdoc_carries_provenance(source_connector):
    """每个注册 SourceConnector：产出的每个 RawDoc 都带非空 source_doc_id + locator。"""
    n = _assert_provenance_complete(source_connector)
    assert n == 3  # fixture 树三个可见文件（隐藏 / 临时文件被忽略）


def test_source_doc_id_is_filename(source_connector):
    """血缘根口径：source_doc_id = 文件名（与 narrative_ingest `doc_id = path.name` 一致）。"""
    ids = {doc.source_doc_id for doc in source_connector.iter_documents()}
    assert ids == {"a.pptx", "b.pdf", "c.txt"}  # 隐藏 / Office 临时文件被忽略


def test_deterministic_order_across_calls(source_connector):
    """确定性枚举：同一 connector 两次迭代产出相同顺序的 source_doc_id 序列。"""
    first = [d.source_doc_id for d in source_connector.iter_documents()]
    second = [d.source_doc_id for d in source_connector.iter_documents()]
    assert first == second


# ===========================================================================
# 非空泛证明：丢血缘的 stub 必须 FAIL（证明 provenance pack 非空泛）
# ===========================================================================

class _LineageDroppingConnector:
    """反证 stub：产出 source_doc_id / locator 皆空的 RawDoc——【故意】违反 provenance。"""

    def iter_documents(self):
        from ragspine.ingestion.source.connector import RawDoc

        yield RawDoc(source_doc_id="", locator="", content=b"x")


def test_lineage_dropping_stub_fails_provenance():
    """喂丢血缘 stub 进同一断言核必须 AssertionError——证明 provenance pack 非空泛。"""
    with pytest.raises(AssertionError):
        _assert_provenance_complete(_LineageDroppingConnector())
