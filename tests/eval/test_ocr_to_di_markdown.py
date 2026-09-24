"""scripts/examples/ocr_to_di_markdown.py：OCR 行 → DI 风格 markdown（分页、页码、阅读顺序、分段、标题、转义）。"""

import importlib.util
import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.di_markdown.models import Heading, Paragraph
from ragspine.extraction.di_markdown.parse import parse_di_markdown
from ragspine.ingestion.page_images.source_pdf import prepare_source_pdfs
from tests.ingestion.page_images.fixtures import make_pdf


def _script():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(
        "ocr_to_di_markdown", ROOT_DIR / "scripts/examples/ocr_to_di_markdown.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(x0, y0, x1, y1, text, score=0.99):
    return {"bbox": [x0, y0, x1, y1], "text": text, "score": score}


_PAGE = [
    # 故意打乱顺序：右列先于左列、下方先于上方
    _line(220, 60, 300, 70, "VONB +17%"),
    _line(20, 10, 300, 34, "Interim Results"),  # 大字、页面上部 → 标题
    _line(20, 60, 120, 70, "Agency 72%"),
    _line(20, 72, 120, 82, "Partnerships 28%"),
    _line(20, 140, 200, 150, "Dividend <up> & more"),
]


def test_page_markdown_reading_order_paragraphs_and_title():
    md = _script().page_markdown(_PAGE, 225.0)
    blocks = md.split("\n\n")
    assert blocks[0] == "# Interim Results"
    # 同一视觉行按 x 排；行距小于行高 → 同一段
    assert blocks[1] == "Agency 72% VONB +17%\nPartnerships 28%"
    # 间距大 → 另起一段；HTML 转义
    assert blocks[2] == "Dividend &lt;up&gt; &amp; more"


def test_leading_hash_is_not_a_heading_and_low_scores_can_be_dropped():
    script = _script()
    lines = [_line(20, 150, 100, 160, "#1 MDRT"), _line(20, 170, 100, 180, "noise", score=0.2)]
    md = script.page_markdown(lines, 225.0, min_score=0.5)
    page = parse_di_markdown(md).pages[0]
    assert [type(b) for b in page.blocks] == [Paragraph]
    assert page.blocks[0].text == "#1 MDRT"


def test_document_is_parseable_with_page_breaks_and_numbers():
    ocr = {
        "pages": [{"page": 1, "lines": _PAGE}, {"page": 3, "lines": [_line(0, 100, 50, 110, "x")]}]
    }
    md = _script().ocr_to_di_markdown(ocr, [225.0, 225.0, 225.0])
    doc = parse_di_markdown(md)
    assert [p.index for p in doc.pages] == [1, 2, 3]
    assert [p.number for p in doc.pages] == [1, 2, 3]
    assert doc.pages[1].blocks == ()  # 缺页 → 空页
    assert isinstance(doc.pages[0].blocks[0], Heading)
    assert "Dividend <up> & more" in doc.pages[0].blocks[-1].text
    with pytest.raises(ValueError):
        _script().ocr_to_di_markdown({"pages": [{"page": 4, "lines": []}]}, [225.0])


def test_main_writes_markdown_and_sidecar_linked_to_pdf(tmp_path):
    pdf = make_pdf(tmp_path / "outlined.pdf", ["A", "B"])
    ocr_json = tmp_path / "ocr.json"
    ocr_json.write_text(
        json.dumps(
            {
                "engine": "test-ocr",
                "dpi": 300,
                "pages": [
                    {"page": 1, "lines": _PAGE},
                    {"page": 2, "lines": [_line(20, 100, 100, 110, "Page two")]},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "md" / "deck-ocr.md"
    assert _script().main([str(ocr_json), "--pdf", str(pdf), "--out", str(out)]) == 0
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["source_pdf"] == str(pdf.resolve()) and meta["ocr_engine"] == "test-ocr"
    # 入库侧能按 sidecar 关联上 PDF（页数一致）
    sources = prepare_source_pdfs([out])
    assert sources["deck-ocr.md"].page_count == 2


def test_main_rejects_page_count_mismatch(tmp_path, capsys):
    pdf = make_pdf(tmp_path / "one.pdf", ["A"])
    ocr_json = tmp_path / "ocr.json"
    ocr_json.write_text(json.dumps({"pages": [{"page": 1, "lines": []}, {"page": 2, "lines": []}]}))
    rc = _script().main([str(ocr_json), "--pdf", str(pdf), "--out", str(tmp_path / "o.md")])
    assert rc == 2 and "页数不一致" in capsys.readouterr().err
