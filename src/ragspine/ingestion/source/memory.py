"""InMemoryConnector —— 零依赖、确定性的「只读外部库 / fixture」SourceConnector。

把一批已在内存里的 RawDoc（测试夹具、外部库预取的文档、上游产物）收敛到 SourceConnector
缝后面：不走盘、不联网，按【调用方给定顺序】原样惰性产出（顺序即契约，不重排），跨调用确定性。
血缘不做臆造——RawDoc 自带 source_doc_id + locator，conformance 的 provenance pack 负责绑死。
"""

from collections.abc import Iterator, Sequence

from ragspine.ingestion.source.connector import RawDoc


class InMemoryConnector:
    """按给定顺序产出内存中的 RawDoc 流（零依赖、确定性、只读）。

    docs 在构造时快照成不可变元组：多次 iter_documents() 产出相同顺序，且外部对原序列的
    后续改动不影响已建连接器（血缘保真，同 RawDoc frozen 的取向）。
    """

    def __init__(self, docs: Sequence[RawDoc]) -> None:
        self._docs: tuple[RawDoc, ...] = tuple(docs)

    def iter_documents(self) -> Iterator[RawDoc]:
        """惰性产出构造时给定顺序的每个 RawDoc（顺序即契约，不重排、不去重）。"""
        yield from self._docs
