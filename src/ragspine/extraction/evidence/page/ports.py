"""Ports for source-bound inference; implementations live in adapters."""

from typing import Protocol, runtime_checkable

from ragspine.extraction.evidence.page.models import (
    LayoutObject,
    ObjectProcessingRecord,
    PageInput,
    PagePartition,
)


@runtime_checkable
class PagePartitioner(Protocol):
    fingerprint: str

    def partition(self, page: PageInput) -> PagePartition: ...


@runtime_checkable
class ObjectProcessor(Protocol):
    def process(self, page: PageInput, item: LayoutObject) -> ObjectProcessingRecord: ...
