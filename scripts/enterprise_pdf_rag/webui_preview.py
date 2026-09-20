"""Start/stop only this project's isolated, preinstalled Open WebUI preview."""

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from shlex import quote
from typing import Literal
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parent.parent.parent
# Logs, PID record and vendor data; override to run beside another recorded preview.
STATE = Path(
    os.environ.get("ENTERPRISE_PREVIEW_STATE_DIR") or ROOT / "data" / "open-webui-preview"
).resolve()
PROCESS_FILE = STATE / "processes.json"
GATE = ROOT / "src" / "enterprise_pdf_rag" / "adapters" / "http" / "webui_gate.py"
# Loopback ports; override both start and status with the same environment.
API_PORT = int(os.environ.get("ENTERPRISE_API_PORT", "8766"))
WEBUI_PORT = int(os.environ.get("ENTERPRISE_WEBUI_PORT", "8767"))
_EMBEDDING_SETTINGS = ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY")
# Only the API child inherits provider settings, and only those its profile can use.
_API_SETTINGS = {
    "aia-source-review": _EMBEDDING_SETTINGS,
    "document-catalog": (
        *_EMBEDDING_SETTINGS,
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        "RERANK_API_KEY",
        "APP_INGESTION_DIR",
        "APP_LEGACY_DOCUMENT_ROOTS",
        "APP_ANSWER_MAX_LIVE_CALLS",
    ),
}
_WEBUI_LOGIN_SETTINGS = ("ENTERPRISE_WEBUI_AUTH", "ENABLE_SIGNUP", "DEFAULT_USER_ROLE")


class ProcessRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    pid: int = Field(gt=1)
    marker: str


type ProcessName = Literal["api", "webui"]
RECORDS = TypeAdapter(dict[ProcessName, ProcessRecord])


def process_records() -> dict[ProcessName, ProcessRecord]:
    try:
        return RECORDS.validate_json(PROCESS_FILE.read_bytes())
    except (OSError, ValidationError):
        raise SystemExit(
            f"Invalid project PID record: {PROCESS_FILE}. No processes were changed."
        ) from None


def process_marker(name: ProcessName) -> str:
    return "enterprise_pdf_rag.adapters.http.app" if name == "api" else str(GATE.parent)


def process_cwd(pid: int) -> Path | None:
    """Working directory of a live process; ``None`` when unreadable, which never matches.

    Linux exposes it under ``/proc`` with no extra tool; macOS has no ``/proc``, so fall
    back to ``lsof`` (not on the default PATH there, hence the ``/usr/sbin`` location).
    """
    with suppress(OSError):
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        listing = subprocess.run(
            [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in listing.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def matching_process(name: ProcessName, record: ProcessRecord) -> bool:
    check = subprocess.run(
        ["/bin/ps", "-p", str(record.pid), "-o", "args="],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode:
        return False
    if (
        process_cwd(record.pid) != ROOT
        or record.marker != process_marker(name)
        or record.marker not in check.stdout
    ):
        raise SystemExit(
            f"PID for {name} does not match this project; no process was started or stopped."
        )
    return True


def current_processing_id(*, required: bool) -> str | None:
    pointer = ROOT / "data/output/aia-2026-interim/pages-001-020/current-processing"
    if not pointer.is_file():
        if required:
            raise SystemExit(
                f"Saved processing is missing: {pointer}. Restore the already processed data tree or follow README's processing instructions. Startup never processes PDFs or calls models."
            )
        return None
    from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
    from enterprise_pdf_rag.adapters.processing_store import ProcessingStore

    try:
        processing_id, manifest = ProcessingStore(pointer.parent).load_current()
        source = LocalDocumentStore(pointer.parent.parent).load_current()
        if source.manifest_id != manifest.scope.source_manifest_id:
            raise ValueError("Source and processing pointers do not match")
    except (OSError, ValueError):
        raise SystemExit(
            f"Saved current-processing is invalid or incomplete: {pointer}. Restore its matching immutable artifacts; startup will not recreate them."
        ) from None
    if required and (
        manifest.scope.physical_pages != tuple(range(1, 21)) or manifest.retrieval is None
    ):
        raise SystemExit(
            "current-processing must reference the published first-20-page processing and retrieval artifacts. See README; startup will not run ingestion or indexing."
        )
    return processing_id


def preview_python() -> Path:
    configured = os.environ.get("OPEN_WEBUI_PYTHON")
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else [
            Path(directory) / name
            for directory in os.get_exec_path()
            for name in ("python3.12", "python", "python3")
        ]
    )
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            probe = subprocess.run(
                [
                    str(candidate),
                    "-B",
                    "-I",
                    "-c",
                    "import sys; from importlib.metadata import version; sys.exit(0 if sys.version_info[:2] == (3, 12) and version('open-webui') == '0.6.5' else 1)",
                ],
                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate.absolute()
    raise SystemExit(
        "No preinstalled Python 3.12 / Open WebUI 0.6.5 runtime found. Set OPEN_WEBUI_PYTHON=/path/to/that/environment/bin/python and rerun scripts/start.sh. See docs/open-webui.md for isolated setup or the pinned Compose option; startup never installs vendor dependencies."
    )


def ready(profile: str, processing_id: str | None) -> bool:
    from enterprise_pdf_rag.adapters.http.openai_schemas import DEMO_MODEL, ModelList
    from enterprise_pdf_rag.adapters.http.processing_schemas import (
        ProcessingStatusResponse,
    )
    from enterprise_pdf_rag.adapters.http.webui_gate import (
        AIA_REVIEW_MODEL,
        DOCUMENT_CATALOG_PROFILE,
        DOCUMENT_MODEL_PREFIX,
    )

    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{API_PORT}/v1/models", timeout=3) as response:
            models = ModelList.model_validate_json(response.read(65536))
        ids = tuple(item.id for item in models.data)
        if profile == DOCUMENT_CATALOG_PROFILE:
            # One model per mounted document; an empty catalog is still the right profile.
            if not all(item.startswith(DOCUMENT_MODEL_PREFIX) for item in ids):
                return False
        else:
            expected = AIA_REVIEW_MODEL if profile == "aia-source-review" else DEMO_MODEL
            if ids != (expected,):
                return False
        with opener.open(f"http://127.0.0.1:{WEBUI_PORT}/api/config", timeout=3) as response:
            if response.status != 200:
                return False
        if processing_id is not None:
            with opener.open(
                f"http://127.0.0.1:{API_PORT}/v1/processing/status", timeout=30
            ) as response:
                saved = ProcessingStatusResponse.model_validate_json(response.read(65536))
            if saved.processing_id != processing_id:
                return False
    except (OSError, URLError, ValidationError):
        return False
    return True


def show_links(processing_id: str | None, profile: str = "aia-source-review") -> None:
    print(f"Open WebUI 0.6.5 compatibility preview: http://127.0.0.1:{WEBUI_PORT}")
    print(f"API: http://127.0.0.1:{API_PORT}")
    if processing_id is not None:
        print(f"First-20-page review: http://127.0.0.1:{API_PORT}/v1/processing/review/review.html")
        print(f"Saved processing: {processing_id}")
    print(f"Logs and PID record: {STATE}")
    ports = ""
    if (API_PORT, WEBUI_PORT) != (8766, 8767):
        ports = f"ENTERPRISE_API_PORT={API_PORT} ENTERPRISE_WEBUI_PORT={WEBUI_PORT} "
    if os.environ.get("ENTERPRISE_PREVIEW_STATE_DIR"):
        ports = f"ENTERPRISE_PREVIEW_STATE_DIR={quote(str(STATE))} {ports}"
    command = f"{ports}uv run --directory {quote(str(ROOT))} --locked python scripts/enterprise_pdf_rag/webui_preview.py"
    option = "" if profile == "aia-source-review" else f" --profile {profile}"
    print(f"Status: {command} status{option}")
    print(f"Stop: {command} stop")


def start(profile: str = "aia-source-review", *, require_processing: bool = False) -> None:
    processing_id = (
        current_processing_id(required=require_processing)
        if profile == "aia-source-review"
        else None
    )
    if PROCESS_FILE.exists():
        records = process_records()
        if set(records) != {"api", "webui"} or not all(
            matching_process(name, record) for name, record in records.items()
        ):
            raise SystemExit(
                "The project process record is stale or incomplete. Run the existing stop command, then scripts/start.sh; no automatic cleanup was attempted."
            )
        if ready(profile, processing_id):
            print("Reusing the existing project API and Open WebUI; no new processes.")
            show_links(processing_id, profile)
            return
        raise SystemExit(
            "Recorded project services are unhealthy or serve a different profile/current-processing. Inspect status/logs, then run the existing stop command before scripts/start.sh; no processes were changed."
        )
    if not (ROOT / ".venv/bin/python").is_file():
        raise SystemExit("Project Python environment is missing. Run: uv sync --locked --extra pdf")
    vendor_python = preview_python()
    for port in (API_PORT, WEBUI_PORT):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                raise SystemExit(
                    f"Loopback port {port} is occupied without a reusable project record. No process was stopped; inspect the port owner before retrying."
                ) from None
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    processes: dict[ProcessName, dict[str, str | int]] = {}
    commands: dict[ProcessName, list[str]] = {
        "api": [
            str(ROOT / ".venv" / "bin" / "python"),
            "-m",
            "uvicorn",
            "enterprise_pdf_rag.adapters.http.app:create_configured_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        "webui": [
            str(vendor_python),
            str(GATE),
            "--preview-legacy",
            "--profile",
            profile,
            "--data-dir",
            str(STATE / "vendor"),
            "--port",
            str(WEBUI_PORT),
            "--backend-port",
            str(API_PORT),
        ],
    }
    for name, command in commands.items():
        environment = {
            "PATH": f"{Path(command[0]).parent}:/usr/bin:/bin",
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if name == "api":
            environment["APP_EXECUTION_MODE"] = profile
            for setting in _API_SETTINGS.get(profile, ()):
                if setting in os.environ:
                    environment[setting] = os.environ[setting]
        elif profile == "document-catalog":
            # Explicit opt-in to the vendor's own login; the gate rebuilds the rest.
            for setting in _WEBUI_LOGIN_SETTINGS:
                if setting in os.environ:
                    environment[setting] = os.environ[setting]
        with (STATE / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        processes[name] = {
            "pid": process.pid,
            "marker": process_marker(name),
        }
        PROCESS_FILE.write_text(json.dumps(processes, indent=2) + "\n")
        PROCESS_FILE.chmod(0o600)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if ready(profile, processing_id):
            print("Project API and Open WebUI are ready.")
            show_links(processing_id, profile)
            return
        time.sleep(1)
    raise SystemExit(
        f"Project processes started but readiness failed. Inspect {STATE}/api.log and webui.log; the PID record is retained for the existing status/stop commands."
    )


def stop() -> None:
    if not PROCESS_FILE.exists():
        print("No project preview process record exists.")
        return
    processes = process_records()
    live: list[int] = []
    for name, record in processes.items():
        if not matching_process(name, record):
            continue
        live.append(record.pid)
    for pid in live:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15.0
    while live and time.monotonic() < deadline:
        live = [pid for pid in live if _running(pid)]
        if live:
            time.sleep(0.2)
    if live:
        raise SystemExit(
            "Project processes are still shutting down; PID record retained. Retry stop shortly."
        )
    PROCESS_FILE.unlink()
    print("Stopped only recorded project preview processes. Data is retained.")


def _running(pid: int) -> bool:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def status(profile: str = "aia-source-review") -> int:
    if not PROCESS_FILE.is_file():
        print("No project process record exists; other listeners are not project health.")
        return 1
    records = process_records()
    if set(records) != {"api", "webui"} or not all(
        matching_process(name, record) for name, record in records.items()
    ):
        print(f"Project process record is stale or incomplete: {PROCESS_FILE}")
        return 1
    processing_id = current_processing_id(required=True) if profile == "aia-source-review" else None
    if not ready(profile, processing_id):
        print(f"Project HTTP/profile/current-processing check failed. Logs: {STATE}")
        return 1
    print("Project API and Open WebUI: HTTP 200; PID ownership/profile/snapshot match.")
    show_links(processing_id, profile)
    return 0


@contextmanager
def management_lock() -> Iterator[None]:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "management.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "Another project start/stop is in progress. Retry shortly; no processes were changed."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument(
        "--profile",
        choices=("aia-source-review", "offline-demo", "document-catalog"),
        default="aia-source-review",
    )
    parser.add_argument(
        "--require-processing",
        action="store_true",
        help="Require an existing published first-20-page processing snapshot",
    )
    args = parser.parse_args()
    if args.action == "status":
        return status(args.profile)
    with management_lock():
        if args.action == "start":
            start(args.profile, require_processing=args.require_processing)
        else:
            stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
