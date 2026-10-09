"""One read and one open of a source PDF per scope, however many objects re-prove from it.

Tables, formulas and charts each re-observe the pinned PDF. Outside a ``shared_pdfs()`` scope
every such proof reads the whole PDF from the store and opens (and decrypts) it again, exactly
as before. Inside one, the verified bytes and the opened document are kept per content digest
and handed to every proof in the scope, then the documents are closed when the scope ends.

Reuse is keyed by the SHA-256 of the bytes, so two proofs share a document only when they
would have opened identical bytes; the bytes themselves were digest-verified by the store when
they were first read. The scope is a context variable: nothing leaks to another thread or task,
and a nested scope uses the outer one's cache and leaves the closing to it.
"""

import hashlib
import threading
from _thread import LockType
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import pdfspine

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.pdf_password import open_pdf
from ragspine.extraction.evidence.document.models import DocumentSnapshot


@dataclass
class _Shared:
    pdfs: dict[str, bytes] = field(default_factory=dict)
    documents: dict[str, pdfspine.Document] = field(default_factory=dict)
    # Pages of one run share its scope from several threads (ADR 0045): read / open once.
    lock: LockType = field(default_factory=threading.Lock)


_SHARED: ContextVar[_Shared | None] = ContextVar("enterprise_pdf_rag_shared_pdfs", default=None)


@contextmanager
def shared_pdfs() -> Iterator[None]:
    """Share source PDF bytes and opened documents until the block (or decorated call) ends."""
    if _SHARED.get() is not None:
        yield
        return
    shared = _Shared()
    token = _SHARED.set(shared)
    try:
        yield
    finally:
        _SHARED.reset(token)
        for document in shared.documents.values():
            document.close()


def source_pdf(sources: LocalDocumentStore, snapshot: DocumentSnapshot) -> bytes:
    """The snapshot's source PDF bytes, digest-verified; read once per scope."""
    ref = snapshot.manifest.source
    shared = _SHARED.get()
    if shared is None:
        return sources.get(ref)
    with shared.lock:
        data = shared.pdfs.get(ref.sha256)
        if data is None or len(data) != ref.byte_length:
            data = sources.get(ref)
            shared.pdfs[ref.sha256] = data
    return data


@contextmanager
def opened_pdf(pdf: bytes) -> Iterator[pdfspine.Document]:
    """``open_pdf(pdf)`` for the block: closed after it, or shared while a scope is active."""
    shared = _SHARED.get()
    if shared is None:
        document = open_pdf(pdf)
        try:
            yield document
        finally:
            document.close()
        return
    key = hashlib.sha256(pdf).hexdigest()
    with shared.lock:
        reused = shared.documents.get(key)
        if reused is None:
            reused = shared.documents[key] = open_pdf(pdf)
    yield reused
