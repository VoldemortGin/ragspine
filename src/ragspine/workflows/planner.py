"""Constrained deterministic fallback renderer for Dify DSL 0.6.0.

The fallback intentionally supports one safe blueprint only: start -> llm ->
end.  User text is encoded as a JSON string (valid YAML scalar), so newlines,
Unicode, document markers, template-like text, and ``${...}`` cannot alter the
document structure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ragspine.workflows.errors import WorkflowInputError

MAX_DESCRIPTION_CHARS = 4096
DIFY_DSL_VERSION = "0.6.0"


@dataclass(frozen=True)
class WorkflowBlueprint:
    """The only model-independent graph the fallback renderer accepts."""

    name: str
    goal: str
    input_name: str = "input"
    output_name: str = "result"


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

    name = _yaml_scalar(blueprint.name)
    goal_prompt = _yaml_scalar(
        "You are the specialist for this workflow. "
        f"Goal: {blueprint.goal}\n"
        "Follow the goal precisely, state material uncertainty, and return a focused result."
    )
    input_name = _yaml_scalar(blueprint.input_name)
    output_name = _yaml_scalar(blueprint.output_name)
    user_prompt = _yaml_scalar(f"{{{{#start_1.{blueprint.input_name}#}}}}")
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
            - variable: {input_name}
              label: Input
              max_length: null
              options: []
              type: paragraph
              required: true
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


def _yaml_scalar(value: str) -> str:
    """JSON strings are valid YAML double-quoted scalars."""

    return json.dumps(value, ensure_ascii=False)


def _neutralize_template_markers(value: str) -> str:
    """Keep user text literal when imported into Dify/Jinja-aware fields."""

    return value.replace("{{", "{ {").replace("}}", "} }")


__all__ = [
    "DIFY_DSL_VERSION",
    "MAX_DESCRIPTION_CHARS",
    "WorkflowBlueprint",
    "normalize_description",
    "make_blueprint",
    "render_blueprint",
    "generate_dify_yaml",
]
