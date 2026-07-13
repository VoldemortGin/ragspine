"""SourceConnector → fact_store 桥：让 lineage 端到端进 fact_store 不丢。

薄薄一层：把 connector 产出的每份 RawDoc 落成【文件名 = raw.source_doc_id】的临时文件，再委托
现成的 structured.ingestion.ingest_file 抽取入库。因为 ingest_file 用 `source_doc_id =
path.name` 认血缘根、并从字节算 file_hash，落盘文件名取 raw.source_doc_id 就让抽出的每条 Fact
的 source_doc_id 继承 raw.source_doc_id、source_file_hash 继承 raw.metadata['file_hash']——
血缘不丢是【文件名口径一致】自然带来的，本桥不加任何新血缘逻辑，只做「落盘 + 委托」。
"""

import tempfile
from pathlib import Path

from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.ingestion.source.connector import SourceConnector
from ragspine.ingestion.structured.ingestion import IngestReport, ingest_file
from ragspine.ingestion.structured.ingestion_manifest import ManifestStore
from ragspine.storage.fact_store import FactStore


def ingest_from_connector(
    connector: SourceConnector,
    store: FactStore,
    registry: MappingRegistry,
    queue: ReviewQueue,
    *,
    dry_run: bool = False,
    manifest: ManifestStore | None = None,
    batch_id: str | None = None,
    valid_as_of: str | None = None,
) -> list[IngestReport]:
    """把 connector 产出的每份 RawDoc 落成临时文件并委托 ingest_file，返回逐份 IngestReport。

    临时文件名 = raw.source_doc_id（跨平台经 pathlib），落在一个 TemporaryDirectory 内、迭代结束
    即清理。RawDoc 若 content_type 非受支持的结构化格式，ingest_file 会返回 failed/空报告——不特判，
    照原样收进结果列表（血缘保真优先于容错兜底）。
    """
    reports: list[IngestReport] = []
    with tempfile.TemporaryDirectory(prefix="ragspine_conn_") as tmpdir:
        tmp_root = Path(tmpdir)
        for raw in connector.iter_documents():
            path = tmp_root / raw.source_doc_id
            path.write_bytes(raw.content)
            reports.append(
                ingest_file(
                    path,
                    store,
                    registry,
                    queue,
                    dry_run=dry_run,
                    manifest=manifest,
                    batch_id=batch_id,
                    valid_as_of=valid_as_of,
                )
            )
    return reports
