"""变量表达式双向转换单测：每类模式的正向/反向 + 转不动原样保留且出 warning。"""

from __future__ import annotations

from ragspine.n8n.convert.variables import (
    dify_text_to_n8n,
    n8n_expr_to_selector,
    n8n_value_to_dify,
)

NAME_TO_ID = {"AI Agent": "ai_agent", "开始": "start_1", "Process": "process"}
ID_TO_NAME = {v: k for k, v in NAME_TO_ID.items()}
LLM_IDS = frozenset({"ai_agent"})


def _to_dify(value: object, *, upstream: str | None = "prev_node") -> tuple[object, list[str]]:
    return n8n_value_to_dify(
        value, upstream_id=upstream, name_to_id=NAME_TO_ID, llm_node_ids=LLM_IDS
    )


class TestN8nToDify:
    def test_json_dot_field(self) -> None:
        converted, warnings = _to_dify("={{ $json.question }}")
        assert converted == "{{#prev_node.question#}}"
        assert warnings == []

    def test_json_bracket_field(self) -> None:
        converted, warnings = _to_dify('={{ $json["score"] }}')
        assert converted == "{{#prev_node.score#}}"
        assert warnings == []

    def test_node_bracket_field_with_llm_output_swap(self) -> None:
        converted, warnings = _to_dify('={{ $node["AI Agent"].json["output"] }}')
        assert converted == "{{#ai_agent.text#}}"
        assert warnings == []

    def test_node_dot_field(self) -> None:
        converted, _ = _to_dify('={{ $node["Process"].json.result }}')
        assert converted == "{{#process.result#}}"

    def test_call_item_form(self) -> None:
        converted, _ = _to_dify("={{ $('AI Agent').item.json.output }}")
        assert converted == "{{#ai_agent.text#}}"

    def test_call_first_form(self) -> None:
        converted, _ = _to_dify("={{ $('AI Agent').first().json.answer }}")
        assert converted == "{{#ai_agent.answer#}}"

    def test_mixed_text(self) -> None:
        converted, warnings = _to_dify("=Hello {{ $json.name }}!")
        assert converted == "Hello {{#prev_node.name#}}!"
        assert warnings == []

    def test_unknown_node_name_preserved_with_warning(self) -> None:
        original = '={{ $node["Missing"].json["x"] }}'
        converted, warnings = _to_dify(original)
        assert converted == original
        assert len(warnings) == 1

    def test_other_dollar_expression_preserved_with_warning(self) -> None:
        original = "={{ $now }}"
        converted, warnings = _to_dify(original)
        assert converted == original
        assert len(warnings) == 1
        assert "{{ $now }}" in warnings[0]

    def test_json_without_upstream_preserved_with_warning(self) -> None:
        original = "={{ $json.a }}"
        converted, warnings = _to_dify(original, upstream=None)
        assert converted == original
        assert len(warnings) == 1

    def test_non_expression_passthrough(self) -> None:
        assert _to_dify("plain text") == ("plain text", [])
        assert _to_dify(42) == (42, [])
        assert _to_dify(None) == (None, [])

    def test_static_expression_strips_equals(self) -> None:
        converted, warnings = _to_dify("=static text")
        assert converted == "static text"
        assert warnings == []


class TestExprToSelector:
    def test_json_ref(self) -> None:
        selector = n8n_expr_to_selector(
            "={{ $json.score }}", upstream_id="webhook", name_to_id=NAME_TO_ID,
            llm_node_ids=LLM_IDS,
        )
        assert selector == ["webhook", "score"]

    def test_node_ref_with_llm_swap(self) -> None:
        selector = n8n_expr_to_selector(
            '={{ $node["AI Agent"].json["output"] }}', upstream_id=None,
            name_to_id=NAME_TO_ID, llm_node_ids=LLM_IDS,
        )
        assert selector == ["ai_agent", "text"]

    def test_static_and_non_str_give_none(self) -> None:
        common = {"upstream_id": "x", "name_to_id": NAME_TO_ID, "llm_node_ids": LLM_IDS}
        assert n8n_expr_to_selector("=static", **common) is None
        assert n8n_expr_to_selector("no equals", **common) is None
        assert n8n_expr_to_selector(60, **common) is None


class TestDifyToN8n:
    def test_ref_to_node_expression_with_llm_swap(self) -> None:
        converted, warnings = dify_text_to_n8n(
            "{{#ai_agent.text#}}", id_to_name=ID_TO_NAME, llm_node_ids=LLM_IDS
        )
        assert converted == '={{ $node["AI Agent"].json["output"] }}'
        assert warnings == []

    def test_mixed_text_prefixed_with_equals(self) -> None:
        converted, warnings = dify_text_to_n8n(
            "回答：{{#process.result#}}", id_to_name=ID_TO_NAME, llm_node_ids=LLM_IDS
        )
        assert converted == '=回答：{{ $node["Process"].json["result"] }}'
        assert warnings == []

    def test_unknown_id_preserved_with_warning(self) -> None:
        converted, warnings = dify_text_to_n8n(
            "{{#missing.f#}}", id_to_name=ID_TO_NAME, llm_node_ids=LLM_IDS
        )
        assert "{{#missing.f#}}" in converted
        assert len(warnings) == 1

    def test_plain_text_untouched(self) -> None:
        assert dify_text_to_n8n("no refs", id_to_name=ID_TO_NAME) == ("no refs", [])
