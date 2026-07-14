"""P1 parse 验收：4 fixture 解析、字段归一、拒非法 mode、宽松未知字段、异常归一。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ragspine.dify.errors import DifyCompileError, UnsupportedAppMode
from ragspine.dify.parse.loader import parse_dify_yaml
from ragspine.dify.parse.schema import DifyDoc
from ragspine.workflows.errors import WorkflowFormatError
from ragspine.workflows.formats import MAX_WORKFLOW_BYTES, MAX_YAML_ALIASES


@pytest.mark.parametrize(
    ("name", "mode", "n_nodes", "n_edges"),
    [
        ("seq", "workflow", 4, 3),
        ("branch", "advanced-chat", 5, 5),
        ("parallel", "workflow", 5, 5),
        ("iteration", "workflow", 4, 2),
    ],
)
def test_parse_fixtures(
    fixture_text: Callable[[str], str],
    name: str,
    mode: str,
    n_nodes: int,
    n_edges: int,
) -> None:
    """四 fixture 均解析为 DifyDoc，mode/节点数/边数符合预期。"""
    doc = parse_dify_yaml(fixture_text(name))
    assert isinstance(doc, DifyDoc)
    assert doc.mode == mode
    assert len(doc.nodes) == n_nodes
    assert len(doc.edges) == n_edges


def test_parse_from_path(fixtures_dir: Path) -> None:
    """只有显式 Path 才读取文件；正文 str 与 Path 解析结果等价。"""
    path = fixtures_dir / "seq.yml"
    by_path = parse_dify_yaml(path)
    by_text = parse_dify_yaml(path.read_text(encoding="utf-8"))
    assert by_path.mode == by_text.mode == "workflow"


def test_existing_path_string_is_always_treated_as_yaml_body(fixtures_dir: Path) -> None:
    """str 即使恰好命中服务端文件，也不得触发本地文件读取。"""
    path = fixtures_dir / "seq.yml"

    with pytest.raises(DifyCompileError):
        parse_dify_yaml(str(path))


def test_explicit_path_rejects_symlink(
    tmp_path: Path,
    fixture_text: Callable[[str], str],
) -> None:
    target = tmp_path / "target.yml"
    target.write_text(fixture_text("seq"), encoding="utf-8")
    link = tmp_path / "link.yml"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"当前平台不可创建 symlink: {exc}")

    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml(link)

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_explicit_path_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "oversized.yml"
    path.write_bytes(b"x" * (MAX_WORKFLOW_BYTES + 1))

    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml(path)

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_explicit_path_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.yml"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml(path)

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_explicit_missing_path_normalizes_os_error(tmp_path: Path) -> None:
    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml(tmp_path / "missing.yml")

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_node_type_and_data_normalized(fixture_text: Callable[[str], str]) -> None:
    """节点 data 收成 dict，node_type 取 data.type；edge.source_handle 由 sourceHandle 归一。"""
    doc = parse_dify_yaml(fixture_text("branch"))
    types = {n.id: n.node_type for n in doc.nodes}
    assert types["start_1"] == "start"
    assert types["ifelse_1"] == "if-else"
    assert types["answer_1"] == "answer"

    handles = {(e.source, e.target): e.source_handle for e in doc.edges}
    assert handles[("ifelse_1", "llm_yes")] == "true"
    assert handles[("ifelse_1", "llm_no")] == "false"


def test_unknown_fields_are_lenient(fixture_text: Callable[[str], str]) -> None:
    """顶层与节点的未知字段（kind/version、node.data 任意键）不脆断，原样保留可取。"""
    doc = parse_dify_yaml(fixture_text("seq"))
    # 顶层 kind/version 经 extra='allow' 保留在 model_extra。
    extra = doc.model_extra or {}
    assert extra.get("kind") == "app"
    assert extra.get("version") == "0.1.5"
    # 节点 data 内任意键透传（如 llm 节点的 model 配置）。
    llm = next(n for n in doc.nodes if n.node_type == "llm")
    assert "model" in llm.data
    assert llm.data["model"]["name"] == "claude-opus-4-8"


def test_reject_unsupported_mode() -> None:
    """app.mode 不在支持集合 → UnsupportedAppMode（带 mode 上下文，code 稳定）。"""
    dsl = "app:\n  mode: chat\nworkflow:\n  graph:\n    nodes: []\n    edges: []\n"
    with pytest.raises(UnsupportedAppMode) as ei:
        parse_dify_yaml(dsl)
    assert ei.value.code == "dify.unsupported_app_mode"
    assert ei.value.context.get("mode") == "chat"


def test_reject_missing_app() -> None:
    """缺 app 段 → DifyCompileError（校验失败归一，不外泄 pydantic 异常）。"""
    with pytest.raises(DifyCompileError):
        parse_dify_yaml("workflow:\n  graph:\n    nodes: []\n")


def test_reject_bad_yaml() -> None:
    """YAML 语法错 → DifyCompileError。"""
    with pytest.raises(DifyCompileError):
        parse_dify_yaml("app: : : not valid yaml\n  - broken")


def test_reject_duplicate_yaml_keys() -> None:
    dsl = (
        "app:\n  mode: workflow\n"
        "app:\n  mode: workflow\n"
        "workflow:\n  graph:\n    nodes: []\n    edges: []\n"
    )

    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml(dsl)

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_reject_excessive_yaml_aliases() -> None:
    aliases = "\n".join("  - *app" for _ in range(MAX_YAML_ALIASES + 1))
    dsl = (
        "app: &app\n  mode: workflow\n"
        "workflow:\n  graph:\n    nodes: []\n    edges: []\n"
        f"copies:\n{aliases}\n"
    )

    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml(dsl)

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_reject_oversized_yaml_body() -> None:
    with pytest.raises(DifyCompileError) as error:
        parse_dify_yaml("#" * (MAX_WORKFLOW_BYTES + 1))

    assert isinstance(error.value.__cause__, WorkflowFormatError)


def test_reject_non_mapping_top_level() -> None:
    """顶层不是映射（如纯列表）→ DifyCompileError。"""
    with pytest.raises(DifyCompileError):
        parse_dify_yaml("- a\n- b\n")


def test_reject_empty() -> None:
    """空内容 → DifyCompileError。"""
    with pytest.raises(DifyCompileError):
        parse_dify_yaml("")
