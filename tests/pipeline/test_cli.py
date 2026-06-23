"""拓扑导出 CLI（scripts/topology.py）的行为测试（PRD 测试类目 7）。

只验证外部行为：CLI 对 --of mermaid|dot|json（× --which agent|retriever|service），
不带 --out 时把内容写到 stdout、退出码 0；带 --out 时建出文件、内容合法（Mermaid 以
`flowchart` 开头、DOT 以 `digraph` 开头、JSON 可 json.loads 且含 title/nodes/edges）。
既走子进程（真实生产调用路径），也走进程内 main(argv)->int（便于断言返回码）。

每个用例 docstring 带中文 user story。
"""

import json
import os
import subprocess
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.cli.topology import main as topology_main

PY = sys.executable
TOPOLOGY_CLI = str(ROOT_DIR / "scripts" / "topology.py")


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """从 ROOT_DIR 跑 scripts/topology.py（真实生产调用路径）。"""
    return subprocess.run(
        [PY, TOPOLOGY_CLI, *args],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )


def _assert_valid(fmt: str, text: str) -> None:
    """按格式断言导出内容合法。"""
    assert text.strip(), "导出内容不应为空"
    if fmt == "mermaid":
        assert text.lstrip().startswith("flowchart"), text[:80]
    elif fmt == "dot":
        assert text.lstrip().startswith("digraph"), text[:80]
    else:  # json
        payload = json.loads(text)
        assert {"title", "nodes", "edges"} <= payload.keys()
        assert payload["nodes"], "JSON 应含至少一个节点"


# --- stdout：三种格式都退出 0 且 stdout 非空且合法 --------------------------
@pytest.mark.parametrize("fmt", ["mermaid", "dot", "json"])
def test_cli_stdout_each_format(fmt: str) -> None:
    """作为操作者，我不带 --out 跑 CLI，应把所选格式的拓扑打到 stdout、退出码 0。"""
    proc = _run_cli(["--of", fmt])
    assert proc.returncode == 0, proc.stderr
    _assert_valid(fmt, proc.stdout)


def test_cli_default_format_is_mermaid() -> None:
    """作为操作者，我不传 --of 时默认导出 Mermaid（GitHub 内联渲染）。"""
    proc = _run_cli([])
    assert proc.returncode == 0, proc.stderr
    _assert_valid("mermaid", proc.stdout)


# --- --out：三种格式都建出文件且内容合法 ------------------------------------
@pytest.mark.parametrize("fmt", ["mermaid", "dot", "json"])
def test_cli_out_writes_file(fmt: str, tmp_path) -> None:
    """作为操作者，我带 --out 跑 CLI，应建出文件、内容为所选格式（可入 docs/generated/）。"""
    out = tmp_path / f"topo.{fmt}"
    proc = _run_cli(["--of", fmt, "--out", str(out)])
    assert proc.returncode == 0, proc.stderr
    assert out.exists(), "--out 应建出文件"
    _assert_valid(fmt, out.read_text(encoding="utf-8"))


def test_cli_out_creates_missing_parent_dirs(tmp_path) -> None:
    """作为操作者，我把 --out 指到尚不存在的目录（如 docs/generated/x/）时应自动建目录。"""
    out = tmp_path / "nested" / "deeper" / "topo.mmd"
    proc = _run_cli(["--of", "mermaid", "--out", str(out)])
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    _assert_valid("mermaid", out.read_text(encoding="utf-8"))


# --- --which：三张拓扑都可导出 ----------------------------------------------
@pytest.mark.parametrize("which", ["agent", "retriever", "service"])
def test_cli_which_each_topology(which: str) -> None:
    """作为开发者，我可用 --which 选 agent/retriever/service 任一拓扑导出（默认 agent）。"""
    proc = _run_cli(["--which", which, "--of", "json"])
    assert proc.returncode == 0, proc.stderr
    _assert_valid("json", proc.stdout)


# --- 进程内 main(argv)->int：退出 0 + 文件落地 ------------------------------
def test_cli_main_callable_in_process(tmp_path) -> None:
    """作为自动化方，我能在进程内调 main(argv)->int，成功返回 0 且写出文件。"""
    out = tmp_path / "topo.dot"
    rc = topology_main(["--of", "dot", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    _assert_valid("dot", out.read_text(encoding="utf-8"))
