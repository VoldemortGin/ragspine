"""CLI acceptance tests for natural-language Dify workflow scaffolding."""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import webbrowser
from importlib import import_module
from pathlib import Path

import pytest

from ragspine.cli import main

cli_module = import_module("ragspine.cli.main")

DIFY_FIXTURES = Path(__file__).resolve().parents[1] / "dify" / "fixtures"


def test_implicit_description_reuses_paper_template(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["A rag form understanding paper of CNN", "--stdout"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "name: Paper RAG Q&A" in output
    assert "version: 0.6.0" in output


def test_implicit_command_defaults_to_auto_and_discloses_lexical_fallback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    embedding_module = import_module("ragspine.retrieval.vector.embedding_backends")
    requested: list[str | None] = []

    def no_semantic_backend(spec: str | None = None, **kwargs: object) -> None:
        del kwargs
        requested.append(spec)
        return None

    monkeypatch.setattr(embedding_module, "make_embedding_backend", no_semantic_backend)

    rc = main(["A rag form understanding paper of CNN", "--stdout"])
    captured = capsys.readouterr()

    assert rc == 0
    assert requested == ["auto"]
    assert "lexical" in captured.err.lower()
    assert "name: Paper RAG Q&A" in captured.out


def test_auto_uses_injected_semantic_backend_and_reports_actual_matcher(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_module = import_module("ragspine.retrieval.vector.embedding_backends")

    class FakeSemanticBackend:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [
                [1.0, 0.0],
                *([1.0, 0.0] if index == 0 else [0.0, 1.0] for index, _ in enumerate(texts[1:])),
            ]

    monkeypatch.setattr(
        embedding_module,
        "make_embedding_backend",
        lambda spec, **kwargs: FakeSemanticBackend(),
    )
    output = tmp_path / "semantic.yml"

    rc = main(["semantic paper understanding", "-o", str(output)])
    captured = capsys.readouterr()

    assert rc == 0
    assert output.exists()
    assert "matcher=onnx" in captured.out.lower() or "matcher=onnx" in captured.err.lower()


def test_auto_falls_back_when_embedding_backend_returns_invalid_vectors(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    embedding_module = import_module("ragspine.retrieval.vector.embedding_backends")

    class InvalidSemanticBackend:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            del texts
            return [[1.0, 0.0]]

    monkeypatch.setattr(
        embedding_module,
        "make_embedding_backend",
        lambda spec, **kwargs: InvalidSemanticBackend(),
    )

    rc = main(["A rag form understanding paper of CNN", "--stdout"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "回退 lexical" in captured.err
    assert "name: Paper RAG Q&A" in captured.out


@pytest.mark.parametrize(
    "failure", [ImportError("backend missing"), RuntimeError("backend broken")]
)
def test_auto_falls_back_when_matcher_construction_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def unavailable_matcher(name: str) -> object:
        assert name == "auto"
        raise failure

    monkeypatch.setattr(cli_module, "_workflow_matcher", unavailable_matcher)

    rc = main(["A rag form understanding paper of CNN", "--stdout"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "lexical" in captured.err.lower()
    assert "name: Paper RAG Q&A" in captured.out


def test_stdout_contains_only_workflow_document_and_status_uses_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragspine.workflows.matching import LexicalTemplateMatcher

    monkeypatch.setattr(cli_module, "_workflow_matcher", lambda name: LexicalTemplateMatcher())

    rc = main(["A rag form understanding paper of CNN", "--stdout"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "matcher=" not in captured.out.lower()
    assert "matcher=lexical" in captured.err.lower()
    assert "name: Paper RAG Q&A" in captured.out


def test_implicit_description_requests_auto_matcher_by_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragspine.workflows.matching import LexicalTemplateMatcher

    requested: list[str] = []

    def matcher(name: str) -> LexicalTemplateMatcher:
        requested.append(name)
        return LexicalTemplateMatcher()

    monkeypatch.setattr(cli_module, "_workflow_matcher", matcher)

    assert main(["A rag form understanding paper of CNN", "--stdout"]) == 0
    assert requested == ["auto"]
    assert "name: Paper RAG Q&A" in capsys.readouterr().out


def test_create_json_file_and_reject_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "paper.json"

    assert (
        main(
            [
                "workflow",
                "create",
                "A rag form understanding paper of CNN",
                "--format",
                "json",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["version"] == "0.6.0"
    assert main(["workflow", "create", "another flow", "-o", str(output)]) == 2
    assert "已存在" in capsys.readouterr().err


def test_force_replaces_regular_file_atomically(tmp_path: Path) -> None:
    output = tmp_path / "workflow.yml"
    output.write_text("old", encoding="utf-8")

    rc = main(
        [
            "workflow",
            "create",
            "new private workflow",
            "--no-reuse",
            "--force",
            "-o",
            str(output),
        ]
    )

    assert rc == 0
    assert output.read_text(encoding="utf-8").startswith("app:")
    assert not tuple(tmp_path.glob(".workflow.yml.*.tmp"))


def test_force_rejects_symlink_without_touching_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "target.yml"
    target.write_text("secret", encoding="utf-8")
    output = tmp_path / "workflow.yml"
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")

    rc = main(
        [
            "workflow",
            "create",
            "new workflow",
            "--no-reuse",
            "--force",
            "-o",
            str(output),
        ]
    )

    assert rc == 2
    assert target.read_text(encoding="utf-8") == "secret"
    assert "链接" in capsys.readouterr().err


def test_force_detects_target_swapped_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.yml"
    target.write_text("secret", encoding="utf-8")
    output = tmp_path / "workflow.yml"
    output.write_text("old", encoding="utf-8")
    real_fsync = os.fsync
    swapped = False

    def swap_on_file_fsync(fd: int) -> None:
        nonlocal swapped
        real_fsync(fd)
        if not swapped:
            output.unlink()
            output.symlink_to(target)
            swapped = True

    monkeypatch.setattr(cli_module.os, "fsync", swap_on_file_fsync)

    rc = main(
        [
            "workflow",
            "create",
            "new workflow",
            "--no-reuse",
            "--force",
            "-o",
            str(output),
        ]
    )

    assert rc == 2
    assert target.read_text(encoding="utf-8") == "secret"
    assert output.is_symlink()
    assert not tuple(tmp_path.glob(".workflow.yml.*.tmp"))


def test_force_detects_temporary_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "workflow.yml"
    output.write_text("old", encoding="utf-8")
    real_fsync = os.fsync
    swapped = False

    def swap_temp_on_file_fsync(fd: int) -> None:
        nonlocal swapped
        real_fsync(fd)
        if not swapped:
            temporary = next(tmp_path.glob(".workflow.yml.*.tmp"))
            temporary.unlink()
            temporary.write_text("attacker content", encoding="utf-8")
            swapped = True

    monkeypatch.setattr(cli_module.os, "fsync", swap_temp_on_file_fsync)

    rc = main(
        [
            "workflow",
            "create",
            "new workflow",
            "--no-reuse",
            "--force",
            "-o",
            str(output),
        ]
    )

    assert rc == 2
    assert output.read_text(encoding="utf-8") == "old"
    assert not tuple(tmp_path.glob(".workflow.yml.*.tmp"))


def test_failed_non_force_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "partial.yml"

    def fail_after_partial_write(fd: int, data: bytes) -> None:
        os.write(fd, data[:10])
        raise OSError("simulated write failure")

    monkeypatch.setattr(cli_module, "_write_all", fail_after_partial_write)

    rc = main(
        [
            "workflow",
            "create",
            "new workflow",
            "--no-reuse",
            "-o",
            str(output),
        ]
    )

    assert rc == 2
    assert not output.exists()


def test_template_create_does_not_initialize_requested_onnx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_matcher(name: str) -> object:
        raise AssertionError(f"matcher must not initialize: {name}")

    monkeypatch.setattr(cli_module, "_workflow_matcher", unexpected_matcher)
    output = tmp_path / "template.yml"

    rc = main(
        [
            "workflow",
            "create",
            "--template",
            "rag-paper-qa",
            "--matcher",
            "onnx",
            "-o",
            str(output),
        ]
    )

    assert rc == 0
    assert "Paper RAG Q&A" in output.read_text(encoding="utf-8")


def test_list_and_show_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["workflow", "list"]) == 0
    listing = capsys.readouterr().out
    assert "rag-paper-qa\tPaper RAG Q&A" in listing

    assert main(["workflow", "show", "rag-paper-qa", "--format", "json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["app"]["name"] == "Paper RAG Q&A"


def test_preview_catalog_template_as_versioned_graph_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["workflow", "preview", "rag-paper-qa"]) == 0

    output = capsys.readouterr().out
    preview = json.loads(output)
    assert preview["preview_schema_version"] == 1
    assert len(preview["nodes"]) == 4
    assert len(preview["edges"]) == 3
    assert preview["nodes"][0]["type"] == "start"
    assert "prompt_template" not in output
    assert "completion_params" not in output


def test_preview_local_yaml_file_outputs_graph_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = tmp_path / "my-flow.yml"
    flow.write_text((DIFY_FIXTURES / "seq.yml").read_text(encoding="utf-8"), encoding="utf-8")

    assert main(["workflow", "preview", str(flow)]) == 0

    output = capsys.readouterr().out
    preview = json.loads(output)
    assert preview["preview_schema_version"] == 1
    assert [node["id"] for node in preview["nodes"]] == ["start_1", "llm_1", "tt_1", "end_1"]
    assert len(preview["edges"]) == 3
    assert "prompt_template" not in output
    assert "completion_params" not in output


def test_preview_local_json_file_outputs_graph_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ragspine.workflows.formats import dump_json, load_workflow

    flow = tmp_path / "my-flow.json"
    flow.write_text(dump_json(load_workflow(DIFY_FIXTURES / "seq.yml")), encoding="utf-8")

    assert main(["workflow", "preview", str(flow)]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview["preview_schema_version"] == 1
    assert len(preview["nodes"]) == 4


def test_preview_missing_local_file_is_honest_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 带受支持后缀 → 走文件分支（不会被当成 catalog id），缺文件是诚实错误。
    assert main(["workflow", "preview", "no-such-flow.yml"]) == 2

    captured = capsys.readouterr()
    assert "文件不存在" in captured.err
    assert captured.out == ""


def test_preview_local_file_with_unsupported_suffix_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = tmp_path / "flow.txt"
    flow.write_text("app: {}\n", encoding="utf-8")

    assert main(["workflow", "preview", str(flow)]) == 2
    assert "后缀" in capsys.readouterr().err


def test_preview_invalid_local_workflow_document_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = tmp_path / "broken.yml"
    flow.write_text("app: not-a-workflow\n", encoding="utf-8")

    assert main(["workflow", "preview", str(flow)]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert captured.out == ""


def test_run_local_workflow_prints_result_and_ordered_node_traces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "workflow",
            "run",
            str(DIFY_FIXTURES / "seq.yml"),
            "--inputs",
            '{"question": "hello"}',
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload["result"]["result"], str) and payload["result"]["result"]
    traces = payload["node_traces"]
    assert [trace["index"] for trace in traces] == [0, 1, 2, 3]
    assert [trace["node_id"] for trace in traces] == ["start_1", "llm_1", "tt_1", "end_1"]
    assert all(trace["status"] == "succeeded" for trace in traces)
    assert all("inputs" in trace and "outputs" in trace for trace in traces)
    # 人读摘要走 stderr，stdout 只有 JSON（可脚本化）。
    assert "节点 trace" in captured.err
    assert "节点 trace" not in captured.out


def test_run_unsupported_node_is_rejected_by_l0_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # agent_tool.yml 编译出 tool 占位骨架（warnings 非空）→ L0 安全门拒跑，清晰报错不崩溃。
    rc = main(["workflow", "run", str(DIFY_FIXTURES / "agent_tool.yml")])

    assert rc == 1
    captured = capsys.readouterr()
    assert "dify.unsafe" in captured.err
    assert "拒绝执行" in captured.err
    assert captured.out == ""


_FAILING_CODE_NODE_YAML = """
app:
  mode: workflow
  name: fail-demo
kind: app
version: "0.1.5"
workflow:
  graph:
    nodes:
      - id: start_1
        data:
          type: start
          title: 开始
          variables:
            - variable: question
              label: 问题
              type: text-input
              required: true
      - id: code_1
        data:
          type: code
          title: 会炸的代码
          code: "def main(x):\\n    raise ValueError('boom')\\n"
          code_language: python3
          variables:
            - variable: x
              value_selector: [start_1, question]
          outputs:
            out: {type: string}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - variable: out
              value_selector: [code_1, out]
    edges:
      - {source: start_1, target: code_1, sourceHandle: source}
      - {source: code_1, target: end_1, sourceHandle: source}
"""


def test_run_failure_reports_error_and_partial_traces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = tmp_path / "fail.yml"
    flow.write_text(_FAILING_CODE_NODE_YAML, encoding="utf-8")

    rc = main(["workflow", "run", str(flow), "--inputs", '{"question": "q"}'])

    assert rc == 1
    captured = capsys.readouterr()
    assert "执行失败" in captured.err
    # 失败前已执行节点的 trace 摘要仍给到用户（runner 净化后附在异常 context）。
    assert "start_1" in captured.err
    assert "code_1" in captured.err
    assert captured.out == ""


def test_run_invalid_inputs_json_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["workflow", "run", str(DIFY_FIXTURES / "seq.yml"), "--inputs", "not-json"])

    assert rc == 2
    assert "--inputs" in capsys.readouterr().err


def test_run_non_object_inputs_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["workflow", "run", str(DIFY_FIXTURES / "seq.yml"), "--inputs", "[1, 2]"])

    assert rc == 2
    assert "JSON object" in capsys.readouterr().err


def test_run_missing_file_is_honest_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["workflow", "run", "no-such-flow.yml"]) == 2
    assert "文件不存在" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["show", "preview"])
def test_catalog_output_unknown_secret_shaped_template_id_does_not_echo_it(
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    secret_id = "SG." + "A" * 22 + "." + "B" * 43

    assert main(["workflow", command, secret_id]) == 2

    captured = capsys.readouterr()
    assert secret_id not in captured.out
    assert secret_id not in captured.err


def test_windows_reserved_output_name_is_rejected_on_every_os(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "CON.yml"

    rc = main(["workflow", "create", "a flow", "-o", str(output)])

    assert rc == 2
    assert not output.exists()
    assert "Windows" in capsys.readouterr().err


@pytest.mark.parametrize("filename", ["bad:name.yml", "bad?name.yml", 'bad"name.yml'])
def test_windows_invalid_output_characters_are_rejected_on_every_os(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    filename: str,
) -> None:
    output = tmp_path / filename

    rc = main(["workflow", "create", "a flow", "-o", str(output)])

    assert rc == 2
    assert not output.exists()
    assert "Windows" in capsys.readouterr().err


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


_LAUNCH_URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/studio/\?launch=([A-Za-z0-9_-]+)")


def _capture_serve(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_serve(app: object, *, host: str, port: int) -> None:
        captured.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli_module, "_serve_app", fake_serve)
    return captured


def test_serve_missing_local_file_is_honest_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["workflow", "serve", "no-such-flow.yml"]) == 2
    assert "文件不存在" in capsys.readouterr().err


def test_serve_occupied_port_is_deterministic_error(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]

        rc = main(["workflow", "serve", str(DIFY_FIXTURES / "seq.yml"), "--port", str(port)])

    assert rc == 2
    error = capsys.readouterr().err
    assert "--port" in error
    assert str(port) in error


def test_serve_local_file_registers_launch_session_and_prints_opaque_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from ragspine.workflows.formats import dump_dify_yaml, load_workflow

    captured = _capture_serve(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    source = DIFY_FIXTURES / "seq.yml"

    rc = main(["workflow", "serve", str(source), "--port", str(_free_port())])

    assert rc == 0
    out = capsys.readouterr().out
    match = _LAUNCH_URL_RE.search(out)
    assert match, out
    assert int(match.group(1)) == captured["port"]
    assert captured["host"] == "127.0.0.1"
    # 隐私：URL/query string 只带不透明 token，绝不带文件路径或工作流内容。
    assert str(source) not in match.group(0)

    client = TestClient(captured["app"])  # type: ignore[arg-type]
    resp = client.get(f"/v1/launch-sessions/{match.group(2)}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "seq"
    assert body["yaml"] == dump_dify_yaml(load_workflow(source))
    # 打开图形绝不悄悄开启执行（PRD）：serve 不改动 RAGSPINE_DIFY_RUN_ENABLED。
    assert captured["app"].state.config.dify_run_enabled is False  # type: ignore[attr-defined]
    assert opened == []  # 未加 --open：零次浏览器打开


def test_serve_catalog_template_id_uses_template_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from ragspine.workflows.catalog import load_builtin_catalog
    from ragspine.workflows.formats import dump_dify_yaml

    captured = _capture_serve(monkeypatch)

    rc = main(["workflow", "serve", "rag-paper-qa", "--port", str(_free_port())])

    assert rc == 0
    match = _LAUNCH_URL_RE.search(capsys.readouterr().out)
    assert match

    client = TestClient(captured["app"])  # type: ignore[arg-type]
    body = client.get(f"/v1/launch-sessions/{match.group(2)}").json()
    template = load_builtin_catalog().get("rag-paper-qa")
    assert body["name"] == template.name
    assert body["yaml"] == dump_dify_yaml(template.workflow)


def test_serve_open_opens_browser_exactly_once_after_port_is_ready(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    opened_event = threading.Event()

    def record_open(url: str) -> bool:
        opened.append(url)
        opened_event.set()
        return True

    monkeypatch.setattr(webbrowser, "open", record_open)

    def fake_serve(app: object, *, host: str, port: int) -> None:
        del app
        # 模拟服务就绪：监听端口，等浏览器打开线程完成其恰好一次的 open。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(1)
            assert opened_event.wait(timeout=10.0)

    monkeypatch.setattr(cli_module, "_serve_app", fake_serve)

    rc = main(["workflow", "serve", str(DIFY_FIXTURES / "seq.yml"), "--port", str(_free_port()), "--open"])

    assert rc == 0
    assert len(opened) == 1
    match = _LAUNCH_URL_RE.search(capsys.readouterr().out)
    assert match
    assert opened[0] == match.group(0)


def test_output_io_error_is_not_misreported_as_matcher_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "workflow.yml"

    def fail_write(path: Path, content: str, *, force: bool) -> None:
        del path, content, force
        raise OSError("disk unavailable")

    monkeypatch.setattr(cli_module, "_write_new_workflow", fail_write)

    rc = main(["workflow", "create", "a flow", "--no-reuse", "-o", str(output)])
    error = capsys.readouterr().err

    assert rc == 2
    assert "输出" in error
    assert "matcher" not in error.lower()
