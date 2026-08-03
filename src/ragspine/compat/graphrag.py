"""Microsoft GraphRAG artifacts（parquet）双向互通：吃进别人的图 / 吐出我们的图。

MS GraphRAG 没有公开 Python API——它的对外契约实际上是 `graphrag index` 产出的一组 parquet
工件。故与它「兼容」的正确形态不是模仿命令行，而是**在工件层互通**：

    import_graphrag_artifacts(Path("./output"), store)   # 别人建好的图 → GraphStore
    export_graphrag_artifacts(store, Path("./out"), seeds=[...])   # 我们的图 → 别人的工具

读取的工件（缺失即报错，多余的工件一律忽略）：
- `entities.parquet` —— 列 `id` / `title` / `type` / `text_unit_ids`
- `relationships.parquet` —— 列 `source` / `target` / `description` / `text_unit_ids`
- `text_units.parquet` —— 列 `id` / `document_ids`，用于把实体回溯到原始文档（血缘根）

**跨边界时不变量如何守住**

- **血缘不得为空**：GraphRAG 的实体本身不带 doc 血缘，只带 `text_unit_ids`。导入方经
  `text_units.parquet` 回溯出 `document_ids` 作为 `source_doc_id`，并用工件内的稳定 id 组成
  `source_locator`；回溯不到时退回工件文件自身作为血缘（`graphrag:<artifact>#<id>`），**绝不留空**。
- **模型派生必须显形**：GraphRAG 的实体/关系是 LLM 抽的，导入后一律戳 `derived=model-derived`
  + `verified=unverified`，与 `graph/extractor.py` 的 LLM 抽取边同口径——外来断言绝不静默取信。
- **RESTRICTED 绝不外泄**：导出走 `GraphStore.subgraph`，RESTRICTED 节点在存储层即被剔除，
  故不可能进入写给外部工具的 parquet。
- **确定性**：节点/边按 id 升序写出，同一张图重复导出逐字节一致。

pandas / pyarrow 经 `[graphrag-compat]` extra 延迟 import，缺 extra 给友好报错——默认安装
不因这条互通路径变重。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ragspine.graph.store import GraphEdge, GraphNode, GraphStore

__all__ = [
    "ArtifactReport",
    "export_graphrag_artifacts",
    "import_graphrag_artifacts",
]

# 导入记录一律戳的两枚血缘标记（与 graph/extractor.py 同口径）。
PROVENANCE_MODEL_DERIVED = "model-derived"
PROVENANCE_UNVERIFIED = "unverified"

_ENTITIES = "entities.parquet"
_RELATIONSHIPS = "relationships.parquet"
_TEXT_UNITS = "text_units.parquet"

# 导入实体的节点类型（GraphRAG 的 type 列是自由文本，不直接当家族节点类型用）。
_IMPORTED_NODE_TYPE = "entity"
_IMPORTED_EDGE_TYPE = "related_to"


@dataclass(frozen=True)
class ArtifactReport:
    """一次导入/导出的计数汇总。"""

    nodes: int
    edges: int


def _pandas() -> Any:
    try:
        import pandas
    except ImportError as exc:  # pragma: no cover - 取决于安装的 extra
        raise ImportError(
            "未安装 pandas/pyarrow：pip install 'rag-spine[graphrag-compat]' "
            "或 pip install pandas pyarrow"
        ) from exc
    return pandas


def _require(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.is_file():
        raise FileNotFoundError(f"缺少 GraphRAG 工件 {name}：{path}")
    return path


def _first_str(value: Any) -> str:
    """GraphRAG 的 *_ids 列是数组；取升序首个作为确定性代表值，空则空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        items = sorted(str(item) for item in value)
    except TypeError:
        return str(value)
    return items[0] if items else ""


def import_graphrag_artifacts(directory: str | Path, store: GraphStore) -> ArtifactReport:
    """把 `graphrag index` 的 parquet 工件导入 `GraphStore`。

    Args:
        directory: MS GraphRAG 的 output 目录（含 entities/relationships/text_units）。
        store: 目标图存储。

    Returns:
        写入的节点数与边数。

    Raises:
        FileNotFoundError: 必需工件缺失。
        ImportError: 未安装 `[graphrag-compat]` extra。
    """
    pandas = _pandas()
    directory = Path(directory)
    entities = pandas.read_parquet(_require(directory, _ENTITIES))
    relationships = pandas.read_parquet(_require(directory, _RELATIONSHIPS))

    # text_unit -> document 的回溯表（血缘根）。工件缺失时留空表，血缘退回工件自身。
    unit_to_doc: dict[str, str] = {}
    text_units_path = directory / _TEXT_UNITS
    if text_units_path.is_file():
        for row in pandas.read_parquet(text_units_path).to_dict("records"):
            unit_id = str(row.get("id", ""))
            if unit_id:
                unit_to_doc[unit_id] = _first_str(row.get("document_ids"))

    def lineage(unit_ids: Any, fallback_id: str, artifact: str) -> dict[str, str]:
        unit = _first_str(unit_ids)
        doc = unit_to_doc.get(unit, "")
        if doc:
            return {"source_doc_id": doc, "source_locator": f"{doc}#text_unit={unit}"}
        # 回溯不到 → 血缘退回工件自身，绝不留空。
        return {
            "source_doc_id": f"graphrag:{artifact}",
            "source_locator": f"graphrag:{artifact}#{fallback_id}",
        }

    nodes: list[GraphNode] = []
    for row in entities.to_dict("records"):
        title = str(row.get("title") or row.get("id") or "").strip()
        if not title:
            continue
        metadata = lineage(row.get("text_unit_ids"), str(row.get("id", "")), _ENTITIES)
        metadata["derived"] = PROVENANCE_MODEL_DERIVED
        metadata["verified"] = PROVENANCE_UNVERIFIED
        graphrag_type = str(row.get("type") or "").strip()
        if graphrag_type:
            metadata["graphrag_type"] = graphrag_type
        nodes.append(GraphNode(id=title, type=_IMPORTED_NODE_TYPE, label=title, metadata=metadata))

    edges: list[GraphEdge] = []
    for row in relationships.to_dict("records"):
        src = str(row.get("source") or "").strip()
        dst = str(row.get("target") or "").strip()
        if not src or not dst:
            continue
        metadata = lineage(row.get("text_unit_ids"), str(row.get("id", "")), _RELATIONSHIPS)
        metadata["derived"] = PROVENANCE_MODEL_DERIVED
        metadata["verified"] = PROVENANCE_UNVERIFIED
        description = str(row.get("description") or "").strip()
        if description:
            metadata["description"] = description
        edges.append(GraphEdge(src=src, dst=dst, type=_IMPORTED_EDGE_TYPE, metadata=metadata))

    nodes.sort(key=lambda node: node.id)
    edges.sort(key=lambda edge: (edge.src, edge.dst, edge.type))
    written_nodes = store.upsert_nodes(nodes)
    written_edges = store.upsert_edges(edges)
    return ArtifactReport(nodes=written_nodes, edges=written_edges)


def export_graphrag_artifacts(
    store: GraphStore,
    directory: str | Path,
    *,
    seeds: list[str],
    depth: int = 2,
) -> ArtifactReport:
    """把 `GraphStore` 的一块子图写成 MS GraphRAG 形状的 parquet 工件。

    RESTRICTED 节点由存储层的 `subgraph` 直接剔除，不可能出现在产物里。

    Args:
        store: 源图存储。
        directory: 输出目录（不存在则创建）。
        seeds: 子图种子节点 id。
        depth: 子图展开深度。

    Returns:
        写出的节点数与边数。
    """
    pandas = _pandas()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    subgraph = store.subgraph(seeds, depth=depth)
    nodes = sorted(subgraph.nodes, key=lambda node: node.id)
    edges = sorted(subgraph.edges, key=lambda edge: (edge.src, edge.dst, edge.type))

    pandas.DataFrame(
        [
            {
                "id": node.id,
                "title": node.label or node.id,
                "type": node.type,
                "description": "",
                "source_doc_id": node.metadata.get("source_doc_id", ""),
                "source_locator": node.metadata.get("source_locator", ""),
            }
            for node in nodes
        ],
        columns=["id", "title", "type", "description", "source_doc_id", "source_locator"],
    ).to_parquet(directory / _ENTITIES)

    pandas.DataFrame(
        [
            {
                "id": f"{edge.src}->{edge.dst}:{edge.type}",
                "source": edge.src,
                "target": edge.dst,
                "description": edge.metadata.get("description", edge.type),
                "source_doc_id": edge.metadata.get("source_doc_id", ""),
                "source_locator": edge.metadata.get("source_locator", ""),
            }
            for edge in edges
        ],
        columns=["id", "source", "target", "description", "source_doc_id", "source_locator"],
    ).to_parquet(directory / _RELATIONSHIPS)

    return ArtifactReport(nodes=len(nodes), edges=len(edges))
