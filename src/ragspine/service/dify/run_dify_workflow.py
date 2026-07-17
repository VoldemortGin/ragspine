"""L2 子进程隔离入口：在独立进程里执行已编译的 Dify 工作流。

由 :mod:`ragspine.service.dify.runner` 启动；stdin 接收 JSON spec，stdout 返回 JSON 结果。
也可直接运行 ``python -m ragspine.service.dify.run_dify_workflow``。
"""

from __future__ import annotations

import json
import sys
from typing import Any, cast

from corespine import error_to_dict

from ragspine.dify.codegen.emitter import GeneratedCode
from ragspine.service.config import ServiceConfig, build_provider
from ragspine.service.dify.runner import run_generated


def _apply_rlimits(cpu_s: int | None, as_bytes: int | None) -> None:
    """在 Linux 封顶 CPU、地址空间和新建文件大小；其他平台静默跳过。"""
    if not sys.platform.startswith("linux"):
        return
    import resource

    if cpu_s is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    if as_bytes is not None:
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def _parse_spec(raw: str) -> dict[str, Any]:
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("spec 必须是 JSON object")
    return cast("dict[str, Any]", parsed)


def main() -> int:
    raw = sys.stdin.read()
    try:
        spec = _parse_spec(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "dify.spec_error",
                        "message": f"无法解析 spec：{exc}",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0

    try:
        _apply_rlimits(
            cast("int | None", spec.get("rlimit_cpu_s")),
            cast("int | None", spec.get("rlimit_as_bytes")),
        )
        code = GeneratedCode(
            source=cast("str", spec["source"]),
            entrypoint=cast("str", spec.get("entrypoint", "run_workflow")),
            imports=tuple(cast("list[str]", spec.get("imports", ()))),
            warnings=tuple(cast("list[str]", spec.get("warnings", ()))),
        )
        config = ServiceConfig(
            db_path=cast("str", spec.get("db_path", ":memory:")),
            provider_type=cast("str", spec.get("provider_type", "mock")),
            model=cast("str", spec.get("model", ServiceConfig.model)),
            base_url=cast("str | None", spec.get("base_url")),
            reference_date=cast("str | None", spec.get("reference_date")),
            tokens_per_minute=cast("int", spec.get("tokens_per_minute", 0)),
        )
        provider = build_provider(config)
        result = run_generated(
            code,
            cast("dict[str, Any]", spec.get("inputs", {})),
            provider,
            timeout_s=cast("float", spec.get("timeout_s", 10.0)),
        )
        sys.stdout.write(
            json.dumps(
                {"ok": True, "result": result, "warnings": list(code.warnings)},
                ensure_ascii=False,
            )
        )
    except BaseException as exc:  # noqa: BLE001 — 任何失败都整形为 JSON 回父进程
        normalized = error_to_dict(exc)
        error: dict[str, Any] = {
            "type": normalized.get("code") or normalized.get("type", type(exc).__name__),
            "message": normalized.get("message", str(exc)),
        }
        context = normalized.get("context")
        if isinstance(context, dict) and context.get("node_traces"):
            error["node_traces"] = context["node_traces"]
        sys.stdout.write(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
