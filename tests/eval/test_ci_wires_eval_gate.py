"""门禁守护：四命门 QA 评测必须真正接进 scripts/ci.sh（drift-guard 风格）。

eval/CLAUDE.md 的"基线只升不降"是 RAGSpine 最硬的差异化不变量——但若它只靠
约定、不进 ci.sh，反编造/引用/拒答/澄清任一指标退化都不会让 pre-push 失败，等于
没有门禁。本测试钉住"评测确实被 CI 调用"这一事实：删掉 ci.sh 里的 eval 步骤会让
这条测试变红，使该差异化能力无法被悄悄绕过。

与 scripts/check_doc_drift.py / check_docstring_refs.py 同源的"机器强制约定"思路。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

CI_SH = ROOT_DIR / "scripts" / "ci.sh"


def _ci_text() -> str:
    assert CI_SH.exists(), f"找不到本地 CI 门禁脚本：{CI_SH}"
    return CI_SH.read_text(encoding="utf-8")


def test_ci_invokes_qa_eval():
    text = _ci_text()
    assert "run_qa_eval.py" in text, (
        "scripts/ci.sh 未调用 run_qa_eval.py——四命门 + 基线 ratchet 没有进门禁，"
        "anti-fabrication/citation 的回归不会让 CI 失败。"
    )


def test_ci_gates_both_eval_modes():
    text = _ci_text()
    for mode in ("--mode tool", "--mode agent"):
        assert mode in text, (
            f"scripts/ci.sh 缺少 QA 评测的 {mode}——tool（绕 LLM 确定性直测）与 "
            f"agent（answer_question + MockProvider）两种模式都应被门禁覆盖。"
        )
