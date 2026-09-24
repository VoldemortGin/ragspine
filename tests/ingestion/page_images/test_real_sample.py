"""真实样本（AIA 中期业绩演示稿）：DI markdown + 原 PDF 关联入库、页图渲染。样本不在时 skip。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.page_images.source_pdf import resolve_source_pdf
from ragspine.retrieval.page_images.store import PageImageStore

_MD = ROOT_DIR / "data/di-markdown/aia-group-2026-interim-results-presentation.md"
_PDF = ROOT_DIR / "data/samples/aia-group-2026-interim-results-presentation.pdf"

pytestmark = pytest.mark.skipif(
    not (_MD.is_file() and _PDF.is_file()), reason="真实样本不在（data/ 不入库）"
)


def test_sidecar_points_at_the_sample_pdf(monkeypatch):
    monkeypatch.chdir(ROOT_DIR)
    assert resolve_source_pdf(_MD) == _PDF.resolve()


def test_real_sample_pages_rendered(tmp_path):
    from ragspine import RAGSpine

    ws = tmp_path / "ws"
    result = RAGSpine.local(ws).ingest(_MD, source_pdf=_PDF)
    report = result.page_image_report
    assert report is not None
    doc = report.docs[0]
    assert (doc.status, doc.n_pages, doc.n_images) == ("rendered", 71, 71 - doc.n_withheld)
    store = PageImageStore(ws / "knowledge.db")
    try:
        p18 = store.get(_MD.name, 18)
    finally:
        store.close()
    assert p18 is not None and p18.path.is_file()
    assert (p18.width, p18.height) == (1568, 882)  # 960×540pt 幻灯片，长边封顶 1568
