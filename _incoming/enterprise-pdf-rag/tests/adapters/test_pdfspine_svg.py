"""Offline regression against the actual, pinned AIA PDF source asset."""

from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg

SAMPLE = Path("data/samples/aia-group-2026-interim-results-presentation.pdf")
EXPECTED_SHA256 = "df902346791b300566761bfcd42bc93bf19e7ba86273dd0cf32d2bb7e9f0870e"
SVG = "http://www.w3.org/2000/svg"
VALID_ROOT = '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540" />'


def test_native_crop_retains_original_graphics_and_page_coordinates() -> None:
    if not SAMPLE.is_file():
        pytest.skip(
            "Optional real AIA corpus is absent; provision the pinned local PDF to run source acceptance. No download is performed."
        )
    source = SAMPLE.read_bytes()
    assert sha256(source).hexdigest() == EXPECTED_SHA256
    document = pdfspine.open(stream=source, filetype="pdf")
    try:
        native = document.load_page(9).get_svg_image(text_as_path=False)
    finally:
        document.close()

    cropped = crop_native_svg(
        native, width=960.0, height=540.0, bbox=(30.0, 120.0, 310.0, 330.0)
    )
    root = ElementTree.fromstring(cropped)
    embedded = root.find(f"{{{SVG}}}svg")
    original = ElementTree.fromstring(native)

    assert root.attrib["viewBox"] == "30 120 280 210"
    assert root.attrib["width"] == "280"
    assert root.attrib["height"] == "210"
    assert root.attrib["overflow"] == "hidden"
    assert embedded is not None
    assert ElementTree.tostring(embedded) == ElementTree.tostring(original)
    assert native[native.index("<svg ") :].strip() in cropped
    assert len(root.findall(f".//{{{SVG}}}clipPath")) == 104
    assert len(root.findall(f".//{{{SVG}}}path")) == 883
    assert not root.findall(f".//{{{SVG}}}image")


@pytest.mark.parametrize(
    "bbox",
    [
        (-1.0, 0.0, 10.0, 10.0),
        (0.0, 0.0, 0.0, 10.0),
        (0.0, 0.0, 961.0, 10.0),
        (0.0, float("nan"), 10.0, 10.0),
    ],
)
def test_crop_rejects_outside_empty_or_nonfinite_region(
    bbox: tuple[float, float, float, float],
) -> None:
    with pytest.raises(ValueError):
        crop_native_svg(VALID_ROOT, width=960.0, height=540.0, bbox=bbox)


@pytest.mark.parametrize(
    "native",
    [
        VALID_ROOT.replace('viewBox="0 0', 'viewBox="10 0'),
        VALID_ROOT.replace('width="960"', 'width="480"'),
        '<svg xmlns="http://www.w3.org/2000/svg">',
        '<!DOCTYPE svg [<!ENTITY label "text">]>' + VALID_ROOT,
    ],
)
def test_crop_rejects_unverified_native_coordinate_frame(native: str) -> None:
    with pytest.raises(ValueError):
        crop_native_svg(native, width=960.0, height=540.0, bbox=(1.0, 2.0, 20.0, 30.0))
