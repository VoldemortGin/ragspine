"""Human-review export of a traceable SVG; no scripts or remote assets."""

from html import escape
from pathlib import Path

from enterprise_pdf_rag.figures.models import SvgArtifact


def write_review(artifact: SvgArtifact, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "figure.svg").write_text(artifact.svg, encoding="utf-8")
    warning_list = "".join(
        f"<li>{escape(warning)}</li>" for warning in artifact.warnings
    )
    # The source adapter constructs the SVG using a closed set of elements and
    # escaped text; arbitrary external SVG is not accepted by this entry point.
    page = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Figure source review</title>
<style>body{{font:16px system-ui;margin:2rem;max-width:75rem}}svg{{border:1px solid #ccc;max-width:100%;height:auto}}code{{overflow-wrap:anywhere}}</style>
<h1>Figure source review</h1><p>Status: <strong>{artifact.verification.value}</strong></p>
<p>PDF page {artifact.source.page_index + 1}; top-left bounds {escape(str(artifact.source.bbox))}.</p>
<p>Source SHA-256: <code>{artifact.source.document_sha256}</code></p>
{artifact.svg}<ul>{warning_list}</ul></html>"""
    (output / "review.html").write_text(page, encoding="utf-8")
