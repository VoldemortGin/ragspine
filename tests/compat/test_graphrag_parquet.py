"""Microsoft GraphRAG artifacts（parquet）双向互通的公开契约测试。

钉住「别人建好的图能吃进来、我们的图能吐出去」，同时钉住家族不变量在跨越工具边界时不失守：
导入的每个节点/边都必须带血缘（外部工件缺血缘时由导入方按受控规则补齐，绝不留空），
RESTRICTED 绝不因导出而外泄。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

pd = pytest.importorskip("pandas", reason="GraphRAG parquet 互通需要 [graphrag-compat] extra")

from ragspine.compat.graphrag import (
    export_graphrag_artifacts,
    import_graphrag_artifacts,
)
from ragspine.graph.store import RESTRICTED_SENSITIVITY, GraphEdge, GraphNode, InProcessGraphStore


def _write_artifacts(directory) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"id": "e1", "title": "渠道扩张", "type": "driver", "text_unit_ids": ["t1"]},
            {"id": "e2", "title": "交付风险", "type": "risk", "text_unit_ids": ["t2"]},
        ]
    ).to_parquet(directory / "entities.parquet")
    pd.DataFrame(
        [
            {
                "id": "r1",
                "source": "渠道扩张",
                "target": "交付风险",
                "description": "扩张带来交付压力",
                "text_unit_ids": ["t1"],
            }
        ]
    ).to_parquet(directory / "relationships.parquet")
    pd.DataFrame(
        [
            {"id": "t1", "document_ids": ["doc-a.pdf"], "text": "渠道扩张的原文"},
            {"id": "t2", "document_ids": ["doc-b.pdf"], "text": "交付风险的原文"},
        ]
    ).to_parquet(directory / "text_units.parquet")


def test_import_loads_entities_and_relationships(tmp_path) -> None:
    _write_artifacts(tmp_path / "output")
    store = InProcessGraphStore()

    report = import_graphrag_artifacts(tmp_path / "output", store)

    assert report.nodes == 2
    assert report.edges == 1
    assert store.get_node("渠道扩张") is not None


def test_imported_nodes_carry_lineage(tmp_path) -> None:
    """外部工件跨进家族边界后，血缘不得为空——由 text_units 回溯出原始文档。"""
    _write_artifacts(tmp_path / "output")
    store = InProcessGraphStore()

    import_graphrag_artifacts(tmp_path / "output", store)

    node = store.get_node("渠道扩张")
    assert node is not None
    assert node.metadata["source_doc_id"] == "doc-a.pdf"
    assert node.metadata["source_locator"]


def test_imported_records_are_marked_model_derived(tmp_path) -> None:
    """GraphRAG 的实体关系是 LLM 抽的，导入后必须带「模型派生 / 未核实」两枚戳。"""
    _write_artifacts(tmp_path / "output")
    store = InProcessGraphStore()

    import_graphrag_artifacts(tmp_path / "output", store)

    node = store.get_node("交付风险")
    assert node is not None
    assert node.metadata["derived"] == "model-derived"
    assert node.metadata["verified"] == "unverified"


def test_import_is_deterministic(tmp_path) -> None:
    _write_artifacts(tmp_path / "output")
    first, second = InProcessGraphStore(), InProcessGraphStore()

    import_graphrag_artifacts(tmp_path / "output", first)
    import_graphrag_artifacts(tmp_path / "output", second)

    assert first.subgraph(["渠道扩张"], depth=2) == second.subgraph(["渠道扩张"], depth=2)


def test_missing_directory_raises_clear_error(tmp_path) -> None:
    store = InProcessGraphStore()

    with pytest.raises(FileNotFoundError, match="entities.parquet"):
        import_graphrag_artifacts(tmp_path / "nope", store)


def test_export_writes_graphrag_shaped_parquet(tmp_path) -> None:
    store = InProcessGraphStore()
    store.upsert_nodes(
        [
            GraphNode(
                id="ACME",
                type="entity",
                label="ACME",
                metadata={"source_doc_id": "company_profile", "source_locator": "profile"},
            ),
            GraphNode(
                id="REVENUE",
                type="metric",
                label="REVENUE",
                metadata={"source_doc_id": "company_profile", "source_locator": "profile"},
            ),
        ]
    )
    store.upsert_edges(
        [
            GraphEdge(
                src="ACME",
                dst="REVENUE",
                type="mentions",
                metadata={"source_doc_id": "fy2024.pdf", "source_locator": "fy2024.pdf#page=1"},
            )
        ]
    )

    written = export_graphrag_artifacts(store, tmp_path / "out", seeds=["ACME"])

    assert (tmp_path / "out" / "entities.parquet").exists()
    assert (tmp_path / "out" / "relationships.parquet").exists()
    assert written.nodes == 2 and written.edges == 1
    entities = pd.read_parquet(tmp_path / "out" / "entities.parquet")
    assert set(entities.columns) >= {"id", "title", "type"}
    assert sorted(entities["title"]) == ["ACME", "REVENUE"]


def test_export_never_leaks_restricted_nodes(tmp_path) -> None:
    """RESTRICTED 绝不因导出到外部工具而外泄。"""
    store = InProcessGraphStore()
    store.upsert_nodes(
        [
            GraphNode(
                id="public",
                type="entity",
                metadata={"source_doc_id": "d", "source_locator": "l"},
            ),
            GraphNode(
                id="secret",
                type="doc",
                metadata={
                    "source_doc_id": "d",
                    "source_locator": "l",
                    "sensitivity": RESTRICTED_SENSITIVITY,
                },
            ),
        ]
    )

    export_graphrag_artifacts(store, tmp_path / "out", seeds=["public", "secret"])

    entities = pd.read_parquet(tmp_path / "out" / "entities.parquet")
    assert "secret" not in set(entities["id"])


def test_roundtrip_preserves_entity_identity(tmp_path) -> None:
    _write_artifacts(tmp_path / "output")
    store = InProcessGraphStore()
    import_graphrag_artifacts(tmp_path / "output", store)

    export_graphrag_artifacts(store, tmp_path / "roundtrip", seeds=["渠道扩张"])

    entities = pd.read_parquet(tmp_path / "roundtrip" / "entities.parquet")
    assert "渠道扩张" in set(entities["id"])
