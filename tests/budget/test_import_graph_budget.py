"""上手宪法 rule 10（ADR 0012）：`import ragspine` 不得拉起可选层（eval/service…），
并保持惰性——连 4 个核心模块也只在首次访问对应名字时才 import。

用干净子进程跑，保证 sys.modules 无污染。这条同时锁住「opt-in 留在核心 import 之外」
与「__all__ 的 curated 暴露仍是惰性」两件事。
"""

import os
import subprocess
import sys

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)


def _run(code: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT_DIR)
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_bare_import_pulls_no_optin_and_stays_lazy():
    out = _run(
        "import sys, ragspine;"
        "opt=[m for m in ('ragspine.eval.qa_eval','ragspine.service','ragspine.service.api.routes') if m in sys.modules];"
        "core=[m for m in ('ragspine.storage.fact_store','ragspine.agent.agent','ragspine.agent.llm_provider') if m in sys.modules];"
        "print(repr(opt)+'|'+repr(core))"
    )
    opt, core = out.split("|")
    assert opt == "[]", f"import ragspine 急切拉起了可选层：{opt}"
    assert core == "[]", f"import ragspine 急切加载了核心模块（应惰性）：{core}"


def test_accessing_core_name_loads_only_core_not_optin():
    out = _run(
        "import sys, ragspine;"
        "_=ragspine.FactStore; _=ragspine.answer_question;"
        "loaded=('ragspine.storage.fact_store' in sys.modules) and ('ragspine.agent.agent' in sys.modules);"
        "opt=[m for m in ('ragspine.eval.qa_eval','ragspine.service') if m in sys.modules];"
        "print(str(loaded)+'|'+repr(opt))"
    )
    loaded, opt = out.split("|")
    assert loaded == "True", "访问 curated 名字后其源模块应已加载"
    assert opt == "[]", f"访问核心名字不应连带拉起可选层：{opt}"
