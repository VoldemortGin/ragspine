"""Configuration for a project-owned, loopback-only SSH model tunnel."""

import os
import re
import signal
import socket
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class TunnelConfigurationError(ValueError):
    """Tunnel configuration is absent or unsafe."""


class TunnelStateError(RuntimeError):
    """A tunnel process cannot be safely started, identified or stopped."""


_HOST = re.compile(r"[A-Za-z0-9_.@:-]+")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value or "\n" in value or "\r" in value:
        raise TunnelConfigurationError(f"Missing or invalid setting: {name}")
    return value


def _port(environment: Mapping[str, str], name: str) -> int:
    value = _required(environment, name)
    try:
        port = int(value)
    except ValueError:
        raise TunnelConfigurationError(f"Invalid TCP port: {name}") from None
    if str(port) != value or not 1 <= port <= 65_535:
        raise TunnelConfigurationError(f"Invalid TCP port: {name}")
    return port


@dataclass(frozen=True, slots=True)
class LocalModelTunnelConfig:
    ssh_host: str
    ssh_port: int
    embedding_local_port: int
    embedding_remote_port: int
    rerank_local_port: int
    rerank_remote_port: int

    @property
    def ssh_command(self) -> tuple[str, ...]:
        return (
            "/usr/bin/ssh",
            "-N",
            "-T",
            "-p",
            str(self.ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{self.embedding_local_port}:127.0.0.1:{self.embedding_remote_port}",
            "-L",
            f"127.0.0.1:{self.rerank_local_port}:127.0.0.1:{self.rerank_remote_port}",
            "--",
            self.ssh_host,
        )

    @property
    def embedding_base_url(self) -> str:
        return f"http://127.0.0.1:{self.embedding_local_port}"

    @property
    def rerank_base_url(self) -> str:
        return f"http://127.0.0.1:{self.rerank_local_port}"


class TunnelStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    running: bool
    pid: int | None
    embedding_base_url: str
    rerank_base_url: str


def load_tunnel_config(
    environment: Mapping[str, str] | None = None,
) -> LocalModelTunnelConfig:
    env = os.environ if environment is None else environment
    host = _required(env, "LOCAL_MODELS_SSH_HOST")
    if not _HOST.fullmatch(host) or host.startswith("-"):
        raise TunnelConfigurationError("Invalid SSH host")
    config = LocalModelTunnelConfig(
        ssh_host=host,
        ssh_port=_port(env, "LOCAL_MODELS_SSH_PORT"),
        embedding_local_port=_port(env, "EMBEDDING_LOCAL_PORT"),
        embedding_remote_port=_port(env, "EMBEDDING_REMOTE_PORT"),
        rerank_local_port=_port(env, "RERANK_LOCAL_PORT"),
        rerank_remote_port=_port(env, "RERANK_REMOTE_PORT"),
    )
    if config.embedding_local_port == config.rerank_local_port:
        raise TunnelConfigurationError("Embedding and rerank local ports must differ")
    return config


def _state_file(state_directory: Path) -> Path:
    return state_directory / "tunnel.pid"


def _process_args(pid: int) -> str | None:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "args="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _read_pid(state_directory: Path) -> int | None:
    path = _state_file(state_directory)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        raise TunnelStateError("Tunnel PID record is invalid") from None
    if pid <= 0:
        raise TunnelStateError("Tunnel PID record is invalid")
    return pid


def _matches(pid: int, config: LocalModelTunnelConfig) -> bool:
    args = _process_args(pid)
    markers = (
        "/usr/bin/ssh",
        f"127.0.0.1:{config.embedding_local_port}:127.0.0.1:{config.embedding_remote_port}",
        f"127.0.0.1:{config.rerank_local_port}:127.0.0.1:{config.rerank_remote_port}",
        config.ssh_host,
    )
    return args is not None and all(marker in args for marker in markers)


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def tunnel_status(config: LocalModelTunnelConfig, state_directory: Path) -> TunnelStatus:
    pid = _read_pid(state_directory)
    running = (
        pid is not None
        and _matches(pid, config)
        and _port_ready(config.embedding_local_port)
        and _port_ready(config.rerank_local_port)
    )
    return TunnelStatus(
        running=running,
        pid=pid if running else None,
        embedding_base_url=config.embedding_base_url,
        rerank_base_url=config.rerank_base_url,
    )


def start_tunnel(
    config: LocalModelTunnelConfig, state_directory: Path, *, cwd: Path
) -> TunnelStatus:
    if _state_file(state_directory).exists():
        raise TunnelStateError("Tunnel PID record already exists; stop it first")
    if _port_ready(config.embedding_local_port) or _port_ready(config.rerank_local_port):
        raise TunnelStateError("A requested loopback port is already in use")
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_directory.chmod(0o700)
    environment = {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LOGNAME", "SSH_AUTH_SOCK", "USER")
        if name in os.environ
    }
    environment["PATH"] = "/usr/bin:/bin"
    log_path = state_directory / "tunnel.log"
    log_path.touch(mode=0o600, exist_ok=True)
    log_path.chmod(0o600)
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            config.ssh_command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 10.0
    while process.poll() is None and time.monotonic() < deadline:
        if _port_ready(config.embedding_local_port) and _port_ready(config.rerank_local_port):
            pending_state = state_directory / "tunnel.pid.tmp"
            pending_state.write_text(f"{process.pid}\n", encoding="ascii")
            pending_state.chmod(0o600)
            pending_state.replace(_state_file(state_directory))
            return tunnel_status(config, state_directory)
        time.sleep(0.1)
    with suppress(ProcessLookupError):
        process.terminate()
    process.wait(timeout=3)
    raise TunnelStateError("SSH tunnel did not become ready; inspect tunnel.log")


def stop_tunnel(config: LocalModelTunnelConfig, state_directory: Path) -> TunnelStatus:
    pid = _read_pid(state_directory)
    if pid is None:
        return tunnel_status(config, state_directory)
    if not _matches(pid, config):
        raise TunnelStateError("Recorded PID is not this configured SSH tunnel")
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10.0
    while _process_args(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    if _process_args(pid) is not None:
        raise TunnelStateError("SSH tunnel is still stopping; retry shortly")
    _state_file(state_directory).unlink()
    return tunnel_status(config, state_directory)
