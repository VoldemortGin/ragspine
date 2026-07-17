"""Constrained deterministic fallback renderer for Dify DSL 0.6.0.

The fallback intentionally supports one safe blueprint only: start -> llm ->
end.  User text is encoded as a JSON string (valid YAML scalar), so newlines,
Unicode, document markers, template-like text, and ``${...}`` cannot alter the
document structure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ragspine.workflows.errors import WorkflowInputError

MAX_DESCRIPTION_CHARS = 4096
DIFY_DSL_VERSION = "0.6.0"


@dataclass(frozen=True)
class WorkflowInput:
    """One bounded start-node input in a safe workflow blueprint."""

    variable: str
    label: str
    kind: str = "paragraph"
    required: bool = True


@dataclass(frozen=True)
class WorkflowBlueprint:
    """The only model-independent graph the fallback renderer accepts."""

    name: str
    goal: str
    input_name: str = "input"
    output_name: str = "result"
    inputs: tuple[WorkflowInput, ...] = ()


def normalize_description(description: str) -> str:
    """Validate while preserving meaningful Unicode and line breaks."""

    if not isinstance(description, str):
        raise WorkflowInputError("workflow description 必须是字符串")
    stripped = description.strip()
    if not stripped:
        raise WorkflowInputError("workflow description 不能为空")
    if len(stripped) > MAX_DESCRIPTION_CHARS:
        raise WorkflowInputError(
            f"workflow description 最长 {MAX_DESCRIPTION_CHARS} 字符，得到 {len(stripped)}"
        )
    if "\x00" in stripped:
        raise WorkflowInputError("workflow description 不得含 NUL")
    try:
        stripped.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkflowInputError("workflow description 必须是合法 Unicode 文本") from exc
    return stripped


def make_blueprint(description: str) -> WorkflowBlueprint:
    """Turn a validated request into a deterministic safe blueprint."""

    raw_goal = normalize_description(description)
    goal = _neutralize_template_markers(raw_goal)
    one_line = " ".join(goal.split())
    name = one_line[:80].rstrip() or "Generated workflow"
    return WorkflowBlueprint(name=name, goal=goal)


def render_blueprint(blueprint: WorkflowBlueprint) -> str:
    """Render a fixed Dify graph, structurally escaping every dynamic scalar."""

    inputs = _validated_inputs(blueprint)
    output_name_value = _validated_variable(blueprint.output_name, label="output_name")
    name = _yaml_scalar(blueprint.name)
    goal_prompt = _yaml_scalar(blueprint_system_prompt(blueprint))
    output_name = _yaml_scalar(output_name_value)
    variables = _render_input_variables(inputs)
    user_prompt = _yaml_scalar(_render_user_prompt(blueprint, inputs))
    return f'''app:
  description: "Spine-authored workflow generated from a natural-language request."
  icon: "🧩"
  icon_background: "#E4FBCC"
  mode: workflow
  name: {name}
  use_icon_as_answer_icon: false
dependencies:
  - type: marketplace
    current_identifier: null
    value:
      marketplace_plugin_unique_identifier: langgenius/openai:0.3.8@592c8252795b5f75807de2d609a03196ed02596b409f7642b4a07548c7ff57ef
kind: app
version: "{DIFY_DSL_VERSION}"
workflow:
  conversation_variables: []
  environment_variables: []
  features: {{}}
  graph:
    nodes:
      - id: start_1
        data:
          desc: ""
          selected: false
          type: start
          title: Start
          variables:
{variables}
        height: 90
        position: {{x: 30, y: 220}}
        positionAbsolute: {{x: 30, y: 220}}
        selected: false
        sourcePosition: right
        targetPosition: left
        type: custom
        width: 244
      - id: llm_1
        data:
          context:
            enabled: false
            variable_selector: []
          desc: ""
          type: llm
          title: Generate result
          model:
            provider: langgenius/openai/openai
            name: gpt-4o-mini
            mode: chat
            completion_params:
              max_tokens: 2048
          prompt_template:
            - id: generated-system
              role: system
              text: {goal_prompt}
            - id: generated-user
              role: user
              text: {user_prompt}
          selected: false
          vision:
            enabled: false
            configs:
              variable_selector: []
        height: 120
        position: {{x: 334, y: 220}}
        positionAbsolute: {{x: 334, y: 220}}
        selected: false
        sourcePosition: right
        targetPosition: left
        type: custom
        width: 244
      - id: end_1
        data:
          desc: ""
          type: end
          title: End
          outputs:
            - variable: {output_name}
              value_selector: [llm_1, text]
              value_type: string
          selected: false
        height: 90
        position: {{x: 638, y: 220}}
        positionAbsolute: {{x: 638, y: 220}}
        selected: false
        sourcePosition: right
        targetPosition: left
        type: custom
        width: 244
    edges:
      - data:
          isInIteration: false
          isInLoop: false
          sourceType: start
          targetType: llm
        id: start_1-source-llm_1-target
        source: start_1
        target: llm_1
        sourceHandle: source
        targetHandle: target
        type: custom
        zIndex: 0
      - data:
          isInIteration: false
          isInLoop: false
          sourceType: llm
          targetType: end
        id: llm_1-source-end_1-target
        source: llm_1
        target: end_1
        sourceHandle: source
        targetHandle: target
        type: custom
        zIndex: 0
    viewport:
      x: 0
      y: 0
      zoom: 0.7
'''


def generate_dify_yaml(description: str) -> str:
    """Validate a request and render its deterministic fallback workflow."""

    return render_blueprint(make_blueprint(description))


def blueprint_system_prompt(blueprint: WorkflowBlueprint) -> str:
    """Return the canonical prompt used by both rendering and catalog generation."""

    return (
        "You are the specialist for this workflow. "
        f"Goal: {blueprint.goal}\n"
        "Follow the goal precisely, state material uncertainty, and return a focused result."
    )


def _validated_inputs(blueprint: WorkflowBlueprint) -> tuple[WorkflowInput, ...]:
    inputs = blueprint.inputs or (
        WorkflowInput(
            variable=_validated_variable(blueprint.input_name, label="input_name"),
            label="Input",
        ),
    )
    if len(inputs) > 8:
        raise WorkflowInputError("workflow blueprint start input 最多 8 个")
    variables: set[str] = set()
    for item in inputs:
        variable = _validated_variable(item.variable, label="input variable")
        if variable in variables:
            raise WorkflowInputError("workflow blueprint input variable 不得重复")
        variables.add(variable)
        if not isinstance(item.label, str) or not item.label.strip():
            raise WorkflowInputError("workflow blueprint input label 必须是非空字符串")
        if item.kind not in {"paragraph", "text-input"}:
            raise WorkflowInputError("workflow blueprint input type 只支持 paragraph/text-input")
        if not isinstance(item.required, bool):
            raise WorkflowInputError("workflow blueprint input required 必须是 bool")
    return inputs


def _validated_variable(value: str, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value) is None:
        raise WorkflowInputError(f"workflow blueprint {label} 非法")
    return value


def _render_input_variables(inputs: tuple[WorkflowInput, ...]) -> str:
    blocks: list[str] = []
    for item in inputs:
        blocks.append(
            "\n".join(
                (
                    f"            - variable: {_yaml_scalar(item.variable)}",
                    f"              label: {_yaml_scalar(item.label)}",
                    "              max_length: null",
                    "              options: []",
                    f"              type: {item.kind}",
                    f"              required: {'true' if item.required else 'false'}",
                )
            )
        )
    return "\n".join(blocks)


def _render_user_prompt(
    blueprint: WorkflowBlueprint,
    inputs: tuple[WorkflowInput, ...],
) -> str:
    if not blueprint.inputs:
        return f"{{{{#start_1.{inputs[0].variable}#}}}}"
    return "\n\n".join(f"{item.label}:\n{{{{#start_1.{item.variable}#}}}}" for item in inputs)


def _yaml_scalar(value: str) -> str:
    """JSON strings are valid YAML double-quoted scalars."""

    return json.dumps(value, ensure_ascii=False)


def _neutralize_template_markers(value: str) -> str:
    """Keep user text literal when imported into Dify/Jinja-aware fields."""

    return value.replace("{{", "{ {").replace("}}", "} }")


__all__ = [
    "DIFY_DSL_VERSION",
    "MAX_DESCRIPTION_CHARS",
    "WorkflowInput",
    "WorkflowBlueprint",
    "normalize_description",
    "make_blueprint",
    "blueprint_system_prompt",
    "render_blueprint",
    "generate_dify_yaml",
]
