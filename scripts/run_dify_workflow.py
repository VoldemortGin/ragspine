"""L2 子进程隔离执行入口：在独立进程里跑一段已编译的 Dify 工作流（受限、可硬杀）。

由 ragspine.service.dify.runner._run_subprocess 以子进程启动；stdin 收一份 JSON spec，
stdout 回一份 JSON 结果。子进程隔离的价值：父进程可对它设硬超时并 SIGKILL（L1 线程软超时
杀不掉失控线程），且（Linux）执行前用 resource.setrlimit 封顶 CPU 时间 / 地址空间 / 文件
大小，越界由内核杀进程——纵深防御的最外层。

spec（JSON，纯可序列化）：
    {"source": str, "entrypoint": str, "imports": [str], "warnings": [str],
     "inputs": {...}, "timeout_s": float,
     "provider_type": "mock"|"anthropic", "model": str, "base_url": str|null,
     "reference_date": str|null, "tokens_per_minute": int,
     "rlimit_cpu_s": int|null, "rlimit_as_bytes": int|null}

输出（JSON）：成功 {"ok": true, "result": {...}, "warnings": [...]}；
            失败 {"ok": false, "error": {"type": str, "message": str}}。

用法（一般不直接手跑；runner 子进程调用）：
    echo '<spec-json>' | .venv/bin/python scripts/run_dify_workflow.py
"""

import json
import sys
from pathlib import Path

import rootutils

# 锚定项目根用本脚本自身位置（而非 os.getcwd）：runner 以 cwd=临时目录 spawn 本进程
# （进程私有 chdir(tmp) 语义），cwd 不指向项目根，故必须按 __file__ 找根。
ROOT_DIR = rootutils.setup_root(
    Path(__file__).resolve().parent, indicator=".project-root", pythonpath=True
)

from corespine import error_to_dict

from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.service.config import ServiceConfig, build_provider
from ragspine.service.dify.runner import run_generated


def _apply_rlimits(cpu_s: int | None, as_bytes: int | None) -> None:
    """（仅 Linux）封顶 CPU 时间 / 地址空间 / 不可新建文件。非 Linux 静默跳过。

    macOS 的 RLIMIT_AS 行为不可靠（常被忽略 / 报错），故只在 Linux 施加内存上限；
    跨平台的硬隔离主要靠"子进程 + 父进程 SIGKILL 超时"，rlimit 是 Linux 上的加固层。
    """
    if not sys.platform.startswith("linux"):
        return
    import resource

    if cpu_s is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    if as_bytes is not None:
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    # 禁止新建文件（写盘逃逸面）：文件大小上限 0。
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def main() -> int:
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": {
            "type": "dify.spec_error", "message": f"无法解析 spec：{exc}"}}))
        return 0

    try:
        _apply_rlimits(spec.get("rlimit_cpu_s"), spec.get("rlimit_as_bytes"))
        code = GeneratedCode(
            source=spec["source"],
            entrypoint=spec.get("entrypoint", "run_workflow"),
            imports=tuple(spec.get("imports", ())),
            warnings=tuple(spec.get("warnings", ())),
        )
        config = ServiceConfig(
            db_path=spec.get("db_path", ":memory:"),
            provider_type=spec.get("provider_type", "mock"),
            model=spec.get("model", ServiceConfig.model),
            base_url=spec.get("base_url"),
            reference_date=spec.get("reference_date"),
            tokens_per_minute=spec.get("tokens_per_minute", 0),
        )
        provider = build_provider(config)
        # 子进程内仍走 L1 受限沙箱（run_generated 含 L0 闸 + 受限 builtins）：
        # 子进程隔离与受限沙箱是叠加的两层，不是二选一。
        result = run_generated(
            code, spec.get("inputs", {}), provider,
            timeout_s=spec.get("timeout_s", 10.0),
        )
        sys.stdout.write(json.dumps(
            {"ok": True, "result": result, "warnings": list(code.warnings)},
            ensure_ascii=False,
        ))
    except BaseException as exc:  # noqa: BLE001 — 任何失败都整形为 JSON 回父进程
        normalized = error_to_dict(exc)
        # 优先回稳定 code（如 dify.unsafe / dify.timeout），与 HTTP 层错误整形口径一致；
        # 非 CorespineError 无 code -> 回退类名。
        error = {
            "type": normalized.get("code") or normalized.get("type", type(exc).__name__),
            "message": normalized.get("message", str(exc)),
        }
        # 节点级 trace（runner 已净化、JSON-safe）随 error 过界，父进程附着回 context。
        context = normalized.get("context")
        if isinstance(context, dict) and context.get("node_traces"):
            error["node_traces"] = context["node_traces"]
        sys.stdout.write(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
