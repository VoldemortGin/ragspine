"""L0 静态安全闸单元测试：warnings 拒跑 + import 白名单 AST 校验。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.dify.api import compile_dify_yaml
from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.service.dify.safety import (
    ALLOWED_IMPORT_ROOTS,
    DifyUnsafeError,
    assert_runnable,
)

FIXTURES = ROOT_DIR / "tests" / "dify" / "fixtures"


def _compile(name: str) -> GeneratedCode:
    return compile_dify_yaml((FIXTURES / name).read_text(encoding="utf-8")).code


# ---------------------------------------------------------------------------
# 闸 1：warnings 非空 -> 拒跑
# ---------------------------------------------------------------------------
def test_warnings_rejected():
    code = _compile("agent_tool.yml")  # tool 占位 -> warning
    assert code.warnings
    with pytest.raises(DifyUnsafeError) as exc:
        assert_runnable(code)
    assert exc.value.code == "dify.unsafe"


def test_clean_workflow_passes():
    # seq.yml：纯 start->llm->template-transform->end，无 warning、import 全在白名单
    assert_runnable(_compile("seq.yml"))


def test_parallel_iteration_knowledge_pass_allowlist():
    # 这些 fixture 会收集 concurrent/json/ragspine.* 等 import，均在白名单内
    for name in ("parallel.yml", "iteration.yml", "knowledge.yml", "qa_fold.yml"):
        assert_runnable(_compile(name))


# ---------------------------------------------------------------------------
# 闸 2：import 白名单
# ---------------------------------------------------------------------------
def test_disallowed_import_rejected():
    src = (
        "import os\n"
        "from dataclasses import dataclass\n"
        "def run_workflow(inputs, *, provider=None):\n"
        "    return {}\n"
    )
    code = GeneratedCode(source=src)
    with pytest.raises(DifyUnsafeError) as exc:
        assert_runnable(code)
    assert "os" in str(exc.value)
    assert exc.value.code == "dify.unsafe"


def test_disallowed_subprocess_socket_rejected():
    for mod in ("subprocess", "socket", "shutil", "importlib", "pathlib", "sys"):
        src = f"import {mod}\ndef run_workflow(inputs, *, provider=None):\n    return {{}}\n"
        with pytest.raises(DifyUnsafeError):
            assert_runnable(GeneratedCode(source=src))


def test_allowlist_roots_are_safe():
    # 白名单刻意不含任何 I/O / 进程 / 网络模块
    forbidden = {"os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib"}
    assert ALLOWED_IMPORT_ROOTS.isdisjoint(forbidden)
