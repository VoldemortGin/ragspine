"""CLI acceptance tests for natural-language Dify workflow scaffolding."""

from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path

import pytest

from ragspine.cli import main

cli_module = import_module("ragspine.cli.main")


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
