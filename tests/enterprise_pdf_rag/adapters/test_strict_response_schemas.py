"""Every response model sent as a strict schema satisfies the provider's strict contract offline.

BUG-A (ADR 0015 validation): an optional ``ModelClaim`` field made ``$defs.ModelClaim.required``
incomplete, the provider rejected every strict request with HTTP 400 and every chat answered
503 — invisible to the offline gate, whose stub never validates a schema. This guard walks the
exact schema ``_response_schema`` sends for each ``response_model`` used at a
``complete_json`` / ``complete_text_json`` call site and enforces the strict-mode rules.
"""

import pytest
from pydantic import BaseModel, ConfigDict

from enterprise_pdf_rag.adapters.chart_semantic_schemas import (
    ChartObservationsDTO,
    FigureDescriptionDTO,
)
from enterprise_pdf_rag.adapters.document_tree_extraction import TreeSummaryDTO
from enterprise_pdf_rag.adapters.http.layout_schemas import PageLayoutDTO
from enterprise_pdf_rag.adapters.page_metadata_extraction import PageMetadataDTO
from enterprise_pdf_rag.adapters.query_translation import QueryTranslationDTO
from enterprise_pdf_rag.adapters.tree_retrieval import TreeRouteDTO
from enterprise_pdf_rag.adapters.visual_semantic_schemas import (
    DiagramObservationsDTO,
    FormulaObservationsDTO,
    ImageObservationsDTO,
    VisualDescriptionDTO,
)
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from ragspine.common.evidence.providers.json_completion import _response_schema

# One entry per ``response_model`` passed to ``complete_json`` / ``complete_text_json``:
# answer_service (ModelAnswer), query_translation (QueryTranslationDTO),
# page_metadata_extraction (PageMetadataDTO), page_partition (PageLayoutDTO),
# tree_retrieval (TreeRouteDTO), document_tree_extraction (TreeSummaryDTO),
# visual_semantics (three observation DTOs + VisualDescriptionDTO) and chart_semantics
# (ChartObservationsDTO, FigureDescriptionDTO).
RESPONSE_MODELS: tuple[type[BaseModel], ...] = (
    ModelAnswer,
    QueryTranslationDTO,
    PageMetadataDTO,
    PageLayoutDTO,
    TreeRouteDTO,
    TreeSummaryDTO,
    ImageObservationsDTO,
    DiagramObservationsDTO,
    FormulaObservationsDTO,
    VisualDescriptionDTO,
    ChartObservationsDTO,
    FigureDescriptionDTO,
)

# Composition and validation keywords strict structured output does not accept.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "prefixItems",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "patternProperties",
        "unevaluatedProperties",
        "dependentRequired",
        "dependentSchemas",
        "propertyNames",
        "additionalItems",
        "contains",
        "uniqueItems",
    }
)


def strict_violations(schema: object, path: str = "$") -> list[str]:
    """Every strict-mode rule the schema breaks, with the path of each offence."""
    found: list[str] = []
    if isinstance(schema, list):
        for index, item in enumerate(schema):
            found.extend(strict_violations(item, f"{path}[{index}]"))
        return found
    if not isinstance(schema, dict):
        return found
    for keyword in sorted(_UNSUPPORTED_KEYWORDS & schema.keys()):
        found.append(f"{path}: unsupported keyword {keyword!r}")
    properties = schema.get("properties")
    if schema.get("type") == "object" or isinstance(properties, dict):
        if schema.get("additionalProperties") is not False:
            found.append(f"{path}: additionalProperties must be false")
        declared = list(properties) if isinstance(properties, dict) else []
        required = schema.get("required")
        if not isinstance(required, list) or sorted(required) != sorted(declared):
            found.append(f"{path}: required {required!r} must list every property {declared!r}")
    ref = schema.get("$ref")
    if ref is not None and not (isinstance(ref, str) and ref.startswith("#/$defs/")):
        found.append(f"{path}: external or non-$defs $ref {ref!r}")
    for key, value in schema.items():
        found.extend(strict_violations(value, f"{path}.{key}"))
    return found


def _refs(schema: object) -> set[str]:
    if isinstance(schema, dict):
        own = {schema["$ref"]} if isinstance(schema.get("$ref"), str) else set()
        return own.union(*(_refs(value) for value in schema.values()))
    if isinstance(schema, list):
        return set().union(*(_refs(item) for item in schema))
    return set()


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda model: model.__name__)
def test_response_model_schema_satisfies_strict_structured_output(model: type[BaseModel]) -> None:
    schema = _response_schema(model, None)
    assert isinstance(schema, dict) and schema.get("type") == "object"
    assert strict_violations(schema) == []
    # Every reference resolves inside the schema's own definitions.
    definitions = schema.get("$defs", {})
    assert isinstance(definitions, dict)
    assert {ref.removeprefix("#/$defs/") for ref in _refs(schema)} <= set(definitions)


def test_optional_claim_fields_are_nullable_but_still_required() -> None:
    # The BUG-A shape: pydantic omits a defaulted field from ``required``; the sent schema
    # keeps it nullable and lists it anyway.
    raw = ModelClaim.model_json_schema()
    assert {"row", "col", "header"} <= set(raw["properties"])
    assert not {"row", "col", "header"} & set(raw.get("required", []))
    sent = _response_schema(ModelAnswer, None)
    assert isinstance(sent, dict)
    claim = sent["$defs"]["ModelClaim"]
    assert sorted(claim["required"]) == sorted(claim["properties"])
    assert {"type": "null"} in claim["properties"]["row"]["anyOf"]


def test_the_guard_flags_a_loose_model() -> None:
    class Loose(BaseModel):
        model_config = ConfigDict(strict=True)  # no extra="forbid"

        text: str
        pair: tuple[int, str]  # heterogeneous: refused before it is ever sent

    class Open(BaseModel):
        model_config = ConfigDict(strict=True)

        text: str

    with pytest.raises(Exception, match="unsupported_heterogeneous_model_schema"):
        _response_schema(Loose, None)
    violations = strict_violations(_response_schema(Open, None))
    assert violations == ["$: additionalProperties must be false"]
    assert strict_violations({"type": "object", "properties": {"a": {}}, "required": []}) == [
        "$: additionalProperties must be false",
        "$: required [] must list every property ['a']",
    ]
