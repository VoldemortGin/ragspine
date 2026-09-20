"""Authored source receipts come from a separate trusted composition port."""

from dataclasses import replace

from enterprise_pdf_rag.adapters.qualification import AuthoredFixtureQualifier


def test_fixture_qualification_is_bound_to_registered_source_and_occurrences() -> None:
    qualifier = AuthoredFixtureQualifier()
    _, svg = qualifier.create_source()
    receipt = qualifier.qualification_for(svg)
    assert receipt is not None
    assert receipt.binding == svg.binding
    assert receipt.source == svg.source
    fields = {field.field_path: field.element_ids for field in receipt.fields}
    assert len(fields) == 10
    assert fields["points.revenue-2024.value"] != fields["points.revenue-2025.value"]
    assert (
        qualifier.qualification_for(replace(svg, figure_id="untrusted-figure")) is None
    )
    assert AuthoredFixtureQualifier().qualification_for(svg) is None
