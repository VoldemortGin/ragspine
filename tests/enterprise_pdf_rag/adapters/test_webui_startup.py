"""Local launcher failures must not start work or stop unrelated processes."""

import json
import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    scripts = root / "scripts" / "enterprise_pdf_rag"
    scripts.mkdir(parents=True)
    (root / ".project-root").touch()
    shutil.copyfile(
        Path("scripts/enterprise_pdf_rag/webui_preview.py"), scripts / "webui_preview.py"
    )
    return root


def test_start_requires_saved_processing_without_starting_services(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/enterprise_pdf_rag/webui_preview.py"),
            "start",
            "--require-processing",
        ],
        cwd=root,
        env={"PATH": os.defpath, "APP_ROOT_DIR": str(root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "current-processing" in result.stderr
    assert not (root / "data/open-webui-preview/processes.json").exists()


def test_start_refuses_a_pid_record_for_an_unrelated_live_process(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    state = root / "data/open-webui-preview"
    state.mkdir(parents=True)
    record = {
        "api": {"pid": os.getpid(), "marker": "enterprise_pdf_rag.adapters.http.app"},
        "webui": {"pid": os.getpid(), "marker": "unrelated"},
    }
    path = state / "processes.json"
    original = json.dumps(record)
    path.write_text(original)
    result = subprocess.run(
        [sys.executable, str(root / "scripts/enterprise_pdf_rag/webui_preview.py"), "start"],
        cwd=root,
        env={"PATH": os.defpath, "APP_ROOT_DIR": str(root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "does not match this project" in result.stderr
    assert path.read_text() == original


def _without_lsof(monkeypatch: pytest.MonkeyPatch, *, proc_cwd: str | None) -> None:
    """Shape a Linux CI runner: no ``lsof`` binary; ``/proc/<pid>/cwd`` readable or absent."""
    real_run = cast(Callable[..., object], subprocess.run)

    def run(argv: list[str], **kwargs: object) -> object:
        if Path(argv[0]).name == "lsof":
            raise FileNotFoundError(argv[0])
        return real_run(argv, **kwargs)

    def readlink(path: str, *args: object, **kwargs: object) -> str:
        if proc_cwd is None or not path.startswith("/proc/"):
            raise OSError(path)
        return proc_cwd

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(os, "readlink", readlink)


def test_unrelated_live_process_is_refused_when_lsof_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    namespace = runpy.run_path(str(root / "scripts/enterprise_pdf_rag/webui_preview.py"))
    process_cwd = cast(Callable[[int], Path | None], namespace["process_cwd"])
    matching_process = cast(Callable[[str, object], bool], namespace["matching_process"])
    record_type = cast(Callable[..., object], namespace["ProcessRecord"])
    record = record_type(pid=os.getpid(), marker="enterprise_pdf_rag.adapters.http.app")

    _without_lsof(monkeypatch, proc_cwd=None)
    assert process_cwd(os.getpid()) is None
    with pytest.raises(SystemExit, match="does not match this project"):
        matching_process("api", record)

    # Linux exposes the working directory under /proc; a foreign cwd is still refused.
    _without_lsof(monkeypatch, proc_cwd=str(tmp_path / "elsewhere"))
    assert process_cwd(os.getpid()) == tmp_path / "elsewhere"
    with pytest.raises(SystemExit, match="does not match this project"):
        matching_process("api", record)
    _without_lsof(monkeypatch, proc_cwd=str(root))
    assert process_cwd(os.getpid()) == root


def test_process_cwd_reads_the_lsof_listing_when_proc_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    namespace = runpy.run_path(str(root / "scripts/enterprise_pdf_rag/webui_preview.py"))
    process_cwd = cast(Callable[[int], Path | None], namespace["process_cwd"])
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=f"p{os.getpid()}\nfcwd\nn{root}\n")

    def no_proc(path: str, *args: object, **kwargs: object) -> str:
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "readlink", no_proc)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", run)
    assert process_cwd(os.getpid()) == root
    assert seen == [["/usr/sbin/lsof", "-a", "-p", str(os.getpid()), "-d", "cwd", "-Fn"]]


def test_explicit_missing_vendor_runtime_is_not_replaced_by_another_python(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    result = subprocess.run(
        [sys.executable, str(root / "scripts/enterprise_pdf_rag/webui_preview.py"), "start"],
        cwd=root,
        env={
            "PATH": os.defpath,
            "APP_ROOT_DIR": str(root),
            "OPEN_WEBUI_PYTHON": str(root / "missing-vendor-python"),
            "OPENAI_API_KEY": "must-never-be-printed",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "OPEN_WEBUI_PYTHON" in result.stderr
    assert "must-never-be-printed" not in result.stdout + result.stderr
    assert not (root / "data/open-webui-preview/processes.json").exists()


def test_state_dir_override_keeps_the_default_record_untouched(tmp_path: Path) -> None:
    root = _project(tmp_path)
    default_state = root / "data/open-webui-preview"
    default_state.mkdir(parents=True)
    record = {
        "api": {"pid": os.getpid(), "marker": "enterprise_pdf_rag.adapters.http.app"},
        "webui": {"pid": os.getpid(), "marker": "unrelated"},
    }
    original = json.dumps(record)
    (default_state / "processes.json").write_text(original)
    override = tmp_path / "other state"
    result = subprocess.run(
        [sys.executable, str(root / "scripts/enterprise_pdf_rag/webui_preview.py"), "start"],
        cwd=root,
        env={
            "PATH": os.defpath,
            "APP_ROOT_DIR": str(root),
            "ENTERPRISE_PREVIEW_STATE_DIR": str(override),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "does not match this project" not in result.stderr
    assert (default_state / "processes.json").read_text() == original
    assert not (override / "processes.json").exists()


def test_status_does_not_trust_http_200_without_a_project_process_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    opened: list[str] = []

    @contextmanager
    def respond_200(url: str, *, timeout: int) -> Iterator[SimpleNamespace]:
        del timeout
        opened.append(url)
        yield SimpleNamespace(status=200)

    def external_opener(*_handlers: object) -> SimpleNamespace:
        return SimpleNamespace(open=respond_200)

    monkeypatch.setattr("urllib.request.build_opener", external_opener)
    monkeypatch.setattr(sys, "argv", ["webui_preview.py", "status"])
    namespace = runpy.run_path(str(root / "scripts/enterprise_pdf_rag/webui_preview.py"))
    main = cast(Callable[[], int], namespace["main"])
    assert main() != 0
    assert opened == []


def test_status_refuses_a_missing_current_pointer_even_with_healthy_owned_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    state = root / "data/open-webui-preview"
    state.mkdir(parents=True)
    marker = str(root / "src/enterprise_pdf_rag/adapters/http")
    (state / "processes.json").write_text(
        json.dumps(
            {
                "api": {"pid": 10001, "marker": "enterprise_pdf_rag.adapters.http.app"},
                "webui": {"pid": 10002, "marker": marker},
            }
        )
    )

    def process_query(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = (
            f"enterprise_pdf_rag.adapters.http.app {marker}"
            if command[0] == "/bin/ps"
            else f"n{root}\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def body(_limit: int) -> bytes:
        return b'{"object":"list","data":[{"id":"aia-2026-interim-source-review-v1"}]}'

    @contextmanager
    def respond_200(_url: str, *, timeout: int) -> Iterator[SimpleNamespace]:
        del timeout
        yield SimpleNamespace(status=200, read=body)

    def external_opener(*_handlers: object) -> SimpleNamespace:
        return SimpleNamespace(open=respond_200)

    monkeypatch.setattr(subprocess, "run", process_query)
    monkeypatch.setattr("urllib.request.build_opener", external_opener)
    monkeypatch.setattr(sys, "argv", ["webui_preview.py", "status"])
    namespace = runpy.run_path(str(root / "scripts/enterprise_pdf_rag/webui_preview.py"))
    main = cast(Callable[[], int], namespace["main"])
    with pytest.raises(SystemExit, match="current-processing"):
        main()
    assert (state / "processes.json").is_file()


def test_launcher_passes_embedding_settings_only_to_api_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import FunctionType

    root = _project(tmp_path)
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    namespace = runpy.run_path(str(root / "scripts/enterprise_pdf_rag/webui_preview.py"))
    start_function = cast(FunctionType, namespace["start"])
    captured: list[dict[str, str]] = []

    def current_processing_id(*, required: bool) -> str:
        return "a" * 64

    def preview_python() -> Path:
        return Path(sys.executable)

    def ready(_profile: str, _processing_id: str | None) -> bool:
        return True

    def ignore(*_args: object) -> None:
        pass

    @contextmanager
    def listener(*_args: object) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(setsockopt=ignore, bind=ignore)

    def spawn(_command: list[str], *, env: dict[str, str], **_kwargs: object) -> SimpleNamespace:
        captured.append(env)
        return SimpleNamespace(pid=10000 + len(captured))

    for name, value in {
        "EMBEDDING_BASE_URL": "http://127.0.0.1:9999/v1",
        "EMBEDDING_MODEL": "test-model",
        "EMBEDDING_API_KEY": "embedding-child-secret",
        "OPENAI_API_KEY": "llm-must-not-pass",
        "RERANK_API_KEY": "rerank-must-not-pass",
        "AWS_SECRET_ACCESS_KEY": "aws-must-not-pass",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(start_function.__globals__, "current_processing_id", current_processing_id)
    monkeypatch.setitem(start_function.__globals__, "preview_python", preview_python)
    monkeypatch.setitem(start_function.__globals__, "ready", ready)
    monkeypatch.setattr("socket.socket", listener)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    start_function()
    assert len(captured) == 2
    api, webui = captured
    assert api["EMBEDDING_API_KEY"] == "embedding-child-secret"
    assert api["EMBEDDING_MODEL"] == "test-model"
    assert api["EMBEDDING_BASE_URL"] == "http://127.0.0.1:9999/v1"
    assert not any(name.startswith("EMBEDDING_") for name in webui)
    for child in captured:
        assert "OPENAI_API_KEY" not in child
        assert "RERANK_API_KEY" not in child
        assert "AWS_SECRET_ACCESS_KEY" not in child
    output = capsys.readouterr()
    retained = (
        output.out
        + output.err
        + "".join(path.read_text() for path in (root / "data/open-webui-preview").glob("*.*"))
    )
    assert "embedding-child-secret" not in retained
    assert "llm-must-not-pass" not in retained
