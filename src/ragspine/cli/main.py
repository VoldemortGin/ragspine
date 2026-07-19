"""ragspine 控制台 CLI 的 argparse 入口与子命令实现（stdlib only，零新依赖）。

子命令均为包内 API 的薄封装（不 shell 调 scripts/，那些不进 wheel）：
- quickstart —— headline：全程离线、零 key。在临时目录建一个微型 KB，用 MockProvider
  跑两次 answer_question：①命中 → 打印数值 + 来源血缘；②查不到 → 打印确定性诚实拒答
  （绝不提供推测数字）。让首次用户一眼看到"会引用真实来源、且绝不臆造"。
- ask —— 镜像 scripts/ask.py：从 --db 建 FactStore、按 --provider 选 provider
  （默认 mock 离线确定性；anthropic 仅在装了 [llm] extra + 有 key 时延迟接入），
  跑 answer_question 并打印答案 + 来源。库路径不存在时诚实报错、非零退出。
- version —— 打印分发版本号。
- dify —— Dify 工作流 YAML 编译器（需 [dify] extra）：`dify compile <path>` 把 .yml 编译成
  纯 Python 脚本并打印（默认附静态优化建议到 stderr）；`dify analyze <path>` 只跑零-API
  静态优化分析、打印建议列表。延迟 import（不装 [dify] 时其它子命令照常可用）。

# `workflow serve` 需要 [service] extra（fastapi/uvicorn 延迟 import，未装时诚实报错并
# 提示安装），本地起 API + Studio 并经 launch-session 自动加载工作流。
# TODO: worker / demo / topology 子命令暂不提供——它们需要随仓库分发的 scripts/，
# 与"零-SDK 离线核心自包含"的设计相悖；待需要时再补。
"""

import argparse
import json
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
import unicodedata
import webbrowser
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp, mkstemp
from typing import Any, Literal, cast

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.common.core import DEFAULT_FACT_DB
from ragspine.storage.fact_store import Fact, SqliteFactStore
from ragspine.workflows.matching import TemplateMatcher

# 分发名（PEP 621 [project] name = "rag-spine"，import 名仍是 ragspine）。
_DIST_NAME = "rag-spine"

_KNOWN_COMMANDS = frozenset({"ask", "dify", "quickstart", "version", "workflow"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_WINDOWS_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WorkflowOutputFormat = Literal["yaml", "json"]
# workflow preview/run 把参数认作本地文件（而非 catalog template id）的判定要素：
# 已存在的文件、含路径分隔符、或带受支持的 workflow 后缀。catalog id 只含 [a-z0-9-]，
# 永不含 "." / "/"，故两个命名空间不会相互遮蔽。
_WORKFLOW_FILE_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml"})

# quickstart 用的微型合成事实（虚构公司 ACME，确定性、可重建；与 qa_eval 的口径一致）。
# 命中演示：ACME_CN FY2024 REVENUE 1320 USD_M，带完整来源血缘。
_QUICKSTART_FACT = Fact(
    metric_code="REVENUE",
    entity="ACME_CN",
    geography="CN",
    channel="TOTAL",
    period_type="FY",
    period="2024",
    value=1320.0,
    unit="USD_M",
    source_doc_id="ACME_FY2024_Results.pptx",
    source_locator="slide=6,table=1,row=2,col=3",
)
# 命中问法（事实存在）与查不到问法（事实缺失，演示坦白拒答）。
_QUICKSTART_FOUND_Q = "中国内地FY2024的REVENUE是多少"
_QUICKSTART_MISSING_Q = "中国内地FY2030的REVENUE是多少"


def _print_result(result: AgentResult) -> None:
    """打印一次问答的答案与数据血缘（与 scripts/ask.py 输出格式一致）。"""
    print(result.answer)
    if result.sources:
        print("\n数据血缘：")
        for src in result.sources:
            print(f"  - {src['doc']} · {src['locator']}")


def _cmd_quickstart(args: argparse.Namespace) -> int:
    """headline 离线演示：建临时 KB → 命中（带血缘）+ 查不到（坦白拒答）→ 清理。"""
    print("RAGSpine quickstart —— 全程离线、零 API key，演示反幻觉 + 来源溯源。\n")
    with TemporaryDirectory(prefix="ragspine_quickstart_") as tmp:
        store = SqliteFactStore(Path(tmp) / "facts.db")
        store.init_schema()
        store.upsert_facts([_QUICKSTART_FACT])
        provider = MockProvider()
        try:
            print(f"① 命中（事实存在，回答必带来源）\n   问：{_QUICKSTART_FOUND_Q}")
            _print_result(answer_question(_QUICKSTART_FOUND_Q, store, provider))
            print(f"\n② 查不到（事实缺失，坦白拒答、绝不臆造）\n   问：{_QUICKSTART_MISSING_Q}")
            _print_result(answer_question(_QUICKSTART_MISSING_Q, store, provider))
        finally:
            store.close()
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    """单条提问：从 --db 建 FactStore、选 provider、跑 answer_question、打印答案 + 来源。"""
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"error: fact 库不存在：{db_path}\n"
            "请用 --db 指定有效的 fact_metric sqlite 路径，"
            "或先用 `ragspine quickstart` 体验离线演示。",
            file=sys.stderr,
        )
        return 2

    if args.provider == "anthropic":
        # 真实 Claude：SDK 延迟 import（缺 [llm] extra 或 key 时由 provider 自身报错）。
        from ragspine.agent.llm_provider import AnthropicProvider

        provider: object = AnthropicProvider()
    else:
        provider = MockProvider()

    store = SqliteFactStore(db_path)
    store.init_schema()
    try:
        result = answer_question(args.question, store, provider)  # type: ignore[arg-type]
    finally:
        store.close()

    _print_result(result)
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    """打印分发版本号（未安装为可分发包时回退到 unknown）。"""
    try:
        print(version(_DIST_NAME))
    except PackageNotFoundError:
        print("unknown")
    return 0


def _cmd_dify_compile(args: argparse.Namespace) -> int:
    """编译 JSON/YAML/TOML Dify 工作流为纯 Python。"""
    from ragspine.dify.api import compile_dify_yaml
    from ragspine.dify.errors import DifyCompileError
    from ragspine.workflows.errors import WorkflowFormatError
    from ragspine.workflows.formats import dump_dify_yaml, load_workflow

    path = Path(args.path)
    if not path.exists():
        print(f"error: 文件不存在：{path}", file=sys.stderr)
        return 2
    try:
        source = dump_dify_yaml(load_workflow(path))
        result = compile_dify_yaml(source, analyze=not args.no_analyze)
    except WorkflowFormatError as exc:
        print(f"error: 工作流格式无效（{exc.code}）：{exc}", file=sys.stderr)
        return 1
    except DifyCompileError as exc:
        print(f"error: 编译失败（{exc.code}）：{exc}", file=sys.stderr)
        return 1

    print(result.code.source)
    if result.code.warnings:
        print("\n# 警告（不支持的节点已生成骨架钩子，请补全）：", file=sys.stderr)
        for w in result.code.warnings:
            print(f"#   - {w}", file=sys.stderr)
    if not args.no_analyze:
        print("\n# 静态优化建议：", file=sys.stderr)
        for s in result.suggestions:
            print(f"#   [{s.severity.value}] {s.rule_id} {s.title}", file=sys.stderr)
    return 0


def _cmd_dify_analyze(args: argparse.Namespace) -> int:
    """只对 JSON/YAML/TOML Dify 工作流跑静态优化分析。"""
    from ragspine.dify.api import analyze as dify_analyze
    from ragspine.dify.errors import DifyCompileError
    from ragspine.workflows.errors import WorkflowFormatError
    from ragspine.workflows.formats import dump_dify_yaml, load_workflow

    path = Path(args.path)
    if not path.exists():
        print(f"error: 文件不存在：{path}", file=sys.stderr)
        return 2
    try:
        source = dump_dify_yaml(load_workflow(path))
        suggestions = dify_analyze(source)
    except WorkflowFormatError as exc:
        print(f"error: 工作流格式无效（{exc.code}）：{exc}", file=sys.stderr)
        return 1
    except DifyCompileError as exc:
        print(f"error: 分析失败（{exc.code}）：{exc}", file=sys.stderr)
        return 1
    if not suggestions:
        print("（无静态优化建议）")
    for s in suggestions:
        print(f"  [{s.severity.value:6s}] {s.rule_id:12s} {s.title}")
        if s.node_ids:
            print(f"            节点：{', '.join(s.node_ids)}")
    return 0


def _serialize_workflow(workflow: dict[str, object], output_format: _WorkflowOutputFormat) -> str:
    """Serialize one canonical workflow mapping for CLI output."""

    from ragspine.workflows.formats import dump_dify_yaml, dump_json

    if output_format == "json":
        return dump_json(workflow)
    return dump_dify_yaml(workflow)


def _safe_workflow_slug(value: str) -> str:
    """Build a portable ASCII filename stem from untrusted natural language."""

    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-.")[:64].rstrip("-.")
    if not slug:
        slug = "workflow"
    if slug.upper() in _WINDOWS_RESERVED_NAMES:
        slug = f"workflow-{slug}"
    return slug


def _is_windows_reserved_path(path: Path) -> bool:
    """Return whether a filename is invalid on Windows, on every host OS."""

    name = path.name
    return (
        not name
        or _WINDOWS_INVALID_FILENAME.search(name) is not None
        or name.endswith((" ", "."))
        or name.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED_NAMES
    )


def _write_new_workflow(path: Path, content: str, *, force: bool) -> None:
    """Write UTF-8 using exclusive create, rejecting links and special files."""

    from ragspine.workflows.errors import WorkflowInputError

    if _is_windows_reserved_path(path):
        raise WorkflowInputError(f"输出文件名不兼容 Windows: {path.name!r}")
    if not path.parent.is_dir():
        raise WorkflowInputError(f"输出目录不存在: {path.parent}")

    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise WorkflowInputError(f"无法检查输出路径: {path}: {exc}") from exc

    if before is not None:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or (reparse_flag and attributes & reparse_flag)
        ):
            raise WorkflowInputError(f"拒绝写入链接或非普通文件: {path}")
        if not force:
            raise WorkflowInputError(f"输出文件已存在: {path}；如需覆盖请加 --force")

    data = content.encode("utf-8")
    if force:
        _atomic_replace_workflow(path, data)
        return

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    opened_identity: tuple[int, int] | None = None
    try:
        fd = os.open(path, flags, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkflowInputError(f"输出路径不是普通文件: {path}")
        opened_identity = (opened.st_dev, opened.st_ino)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise WorkflowInputError(f"输出文件已存在: {path}；如需覆盖请加 --force") from exc
    except Exception:
        if fd >= 0:
            os.close(fd)
        if opened_identity is not None:
            _unlink_if_identity(path, opened_identity)
        raise


def _atomic_replace_workflow(path: Path, data: bytes) -> None:
    """Write a same-directory temporary file, then atomically replace target."""

    from ragspine.workflows.errors import WorkflowInputError

    fd, temporary_name = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise WorkflowInputError("临时输出不是普通文件")
        temporary_identity = (opened.st_dev, opened.st_ino)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1

        try:
            temporary_stat = temporary.lstat()
        except OSError as exc:
            raise WorkflowInputError("临时输出在替换前丢失") from exc
        if (
            not stat.S_ISREG(temporary_stat.st_mode)
            or (
                temporary_stat.st_dev,
                temporary_stat.st_ino,
            )
            != temporary_identity
        ):
            raise WorkflowInputError("临时输出在写入期间被替换")

        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if current is not None:
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            attributes = getattr(current, "st_file_attributes", 0)
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (reparse_flag and attributes & reparse_flag)
            ):
                raise WorkflowInputError(f"拒绝覆盖链接或非普通文件: {path}")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    """Write all bytes, treating a zero-byte write as an I/O failure."""

    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("写入 workflow 时没有取得进展")
        offset += written


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> None:
    """Remove our partial file only if the path still names the same inode."""

    try:
        current = path.lstat()
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            path.unlink()
            _fsync_directory(path.parent)
    except FileNotFoundError:
        pass


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync; some platforms do not support it."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _workflow_matcher(name: str) -> TemplateMatcher:
    """Compatibility seam delegating to the shared workflow matcher factory."""

    from ragspine.workflows.matching import make_template_matcher

    return make_template_matcher(name)


def _cmd_workflow_create(args: argparse.Namespace) -> int:
    """Reuse or generate a Dify workflow from a natural-language description."""

    from ragspine.workflows.catalog import load_builtin_catalog
    from ragspine.workflows.errors import WorkflowError, WorkflowMatcherError
    from ragspine.workflows.matching import LexicalTemplateMatcher
    from ragspine.workflows.scaffold import scaffold_workflow

    description = " ".join(args.description).strip()
    try:
        if not description and args.template:
            description = load_builtin_catalog().get(args.template).name
        matcher_fell_back = False
        matcher: TemplateMatcher
        if args.template or args.no_reuse:
            matcher = LexicalTemplateMatcher()
        else:
            try:
                matcher = _workflow_matcher(args.matcher)
            except Exception:
                if args.matcher != "auto":
                    raise
                matcher_fell_back = True
                matcher = LexicalTemplateMatcher()
                print(
                    "warning: 语义 matcher 初始化失败，已回退 lexical",
                    file=sys.stderr,
                )
        if (
            args.matcher == "auto"
            and matcher.name == "lexical"
            and not args.template
            and not args.no_reuse
            and not matcher_fell_back
        ):
            print(
                "warning: 未安装可用的语义 embedding，已离线回退 lexical matcher",
                file=sys.stderr,
            )
        try:
            result = scaffold_workflow(
                description,
                matcher=matcher,
                template_id=args.template,
                reuse=not args.no_reuse,
            )
        except Exception as exc:
            if args.matcher != "auto" or (
                isinstance(exc, WorkflowError) and not isinstance(exc, WorkflowMatcherError)
            ):
                raise
            print("warning: 语义匹配不可用，已回退 lexical", file=sys.stderr)
            result = scaffold_workflow(
                description,
                matcher=LexicalTemplateMatcher(),
                template_id=args.template,
                reuse=not args.no_reuse,
            )
        content = _serialize_workflow(result.workflow, args.format)
        if args.stdout:
            sys.stdout.write(content)
            print(f"matcher={result.matcher}", file=sys.stderr)
            return 0

        suffix = ".json" if args.format == "json" else ".yml"
        output = (
            Path(args.output)
            if args.output
            else Path(f"{_safe_workflow_slug(args.template or description)}{suffix}")
        )
        _write_new_workflow(output, content, force=args.force)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: workflow 输出失败: {exc}", file=sys.stderr)
        return 2
    except (ImportError, RuntimeError, ValueError) as exc:
        print(f"error: workflow matcher 不可用: {exc}", file=sys.stderr)
        return 2

    print(
        f"created {output} ({result.origin}"
        + (f":{result.template_id}" if result.template_id else "")
        + f", matcher={result.matcher})"
    )
    return 0


def _cmd_workflow_list(args: argparse.Namespace) -> int:
    """Print the bundled, release-validated workflow catalog."""

    from ragspine.workflows.catalog import load_builtin_catalog
    from ragspine.workflows.errors import WorkflowError

    try:
        templates = load_builtin_catalog().list()
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for template in templates:
        categories = ",".join(template.categories)
        print(f"{template.id}\t{template.name}\t{categories}")
    return 0


def _cmd_workflow_show(args: argparse.Namespace) -> int:
    """Print one allowlisted workflow template as YAML or JSON."""

    from ragspine.workflows.catalog import load_builtin_catalog
    from ragspine.workflows.errors import WorkflowError

    try:
        template = load_builtin_catalog().get(args.template_id)
        sys.stdout.write(_serialize_workflow(template.workflow, args.format))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _is_workflow_file_argument(value: str) -> bool:
    """Return whether a workflow source argument names a local file, not a catalog id."""

    if "/" in value or "\\" in value:
        return True
    path = Path(value)
    return path.is_file() or path.suffix.lower() in _WORKFLOW_FILE_SUFFIXES


def _cmd_workflow_preview(args: argparse.Namespace) -> int:
    """Print a versioned, graph-only preview JSON for a template id or a local file."""

    from ragspine.workflows.catalog import load_builtin_catalog
    from ragspine.workflows.errors import WorkflowError
    from ragspine.workflows.formats import dump_json, load_workflow
    from ragspine.workflows.preview import build_workflow_preview

    try:
        if _is_workflow_file_argument(args.source):
            path = Path(args.source)
            if not path.exists():
                print(f"error: 文件不存在：{path}", file=sys.stderr)
                return 2
            workflow: dict[str, object] = load_workflow(path)
        else:
            workflow = load_builtin_catalog().get(args.source).workflow
        preview = build_workflow_preview(workflow)
        sys.stdout.write(dump_json(preview.to_dict()))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _print_node_trace_summary(traces: object) -> None:
    """把节点 trace 摘要打到 stderr（执行顺序/状态/耗时；完整 inputs/outputs 在 stdout JSON）。"""

    if not isinstance(traces, list) or not traces:
        return
    print(f"\n节点 trace（共 {len(traces)} 条，按执行顺序）：", file=sys.stderr)
    for raw in traces:
        trace: dict[str, object] = raw if isinstance(raw, dict) else {}
        elapsed = trace.get("elapsed_ms", 0.0)
        elapsed_text = f"{elapsed:.1f}ms" if isinstance(elapsed, int | float) else "?"
        line = (
            f"  [{trace.get('index', '?')}] {trace.get('node_id', '?')}"
            f" · {trace.get('title', '')}（{trace.get('node_type', '?')}）"
            f" — {trace.get('status', '?')}，{elapsed_text}"
        )
        error = trace.get("error")
        if error:
            line += f"，error: {error}"
        print(line, file=sys.stderr)


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    """本地一次性执行工作流文件：编译（带节点 trace）→ L0 安全门 → L1 受限沙箱执行。

    执行栈与 HTTP `/v1/dify/run` 完全同源（compile_dify_yaml + service.dify.runner），
    不引入第二套执行器；provider 本地选择（默认离线 mock），无需起 FastAPI 服务。
    """

    from corespine import LLMProvider

    from ragspine.dify.api import compile_dify_yaml
    from ragspine.dify.errors import DifyCompileError
    from ragspine.service.dify.runner import (
        DEFAULT_TIMEOUT_S,
        DifyRunError,
        DifyTimeoutError,
        run_generated,
    )
    from ragspine.service.dify.safety import DifyUnsafeError
    from ragspine.workflows.errors import WorkflowFormatError
    from ragspine.workflows.formats import dump_dify_yaml, load_workflow

    path = Path(args.path)
    if not path.exists():
        print(f"error: 文件不存在：{path}", file=sys.stderr)
        return 2
    try:
        inputs = json.loads(args.inputs)
    except json.JSONDecodeError as exc:
        print(f"error: --inputs 不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    if not isinstance(inputs, dict):
        print('error: --inputs 必须是 JSON object，如 \'{"query": "hello"}\'', file=sys.stderr)
        return 2

    provider: LLMProvider
    if args.provider == "anthropic":
        # 真实 Claude：SDK 延迟 import（缺 [llm] extra 或 key 时由 provider 自身报错）。
        from ragspine.agent.llm_provider import AnthropicProvider

        provider = AnthropicProvider()
    else:
        provider = MockProvider()

    try:
        source = dump_dify_yaml(load_workflow(path))
        compiled = compile_dify_yaml(source, analyze=False, emit_node_traces=True)
    except WorkflowFormatError as exc:
        print(f"error: 工作流格式无效（{exc.code}）：{exc}", file=sys.stderr)
        return 1
    except DifyCompileError as exc:
        print(f"error: 编译失败（{exc.code}）：{exc}", file=sys.stderr)
        return 1

    timeout_s = DEFAULT_TIMEOUT_S if args.timeout is None else args.timeout
    try:
        result = run_generated(compiled.code, inputs, provider, timeout_s=timeout_s)
    except DifyUnsafeError as exc:
        # L0 静态闸：不支持节点 / tool 占位骨架 / 越权 import 一律在执行前被拒。
        print(f"error: L0 安全门拒绝执行（{exc.code}）：{exc}", file=sys.stderr)
        return 1
    except (DifyRunError, DifyTimeoutError) as exc:
        # 执行失败也把已执行节点的 trace（runner 净化后附在 context）给到用户。
        _print_node_trace_summary(exc.context.get("node_traces"))
        print(f"error: 执行失败（{exc.code}）：{exc}", file=sys.stderr)
        return 1

    node_traces = result.pop("__node_traces__", None)
    payload = {"result": result, "node_traces": node_traces}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=repr))
    _print_node_trace_summary(node_traces)
    return 0


def _cmd_workflow_check(args: argparse.Namespace) -> int:
    """Print the stable readiness JSON for one local workflow."""

    from ragspine.workflows.readiness import check_workflow

    source = Path(args.source)
    try:
        opened = source.lstat()
    except OSError:
        print("error: workflow 文件不可读", file=sys.stderr)
        return 2
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(opened, "st_file_attributes", 0)
    if (
        source.suffix.lower() not in _WORKFLOW_FILE_SUFFIXES
        or stat.S_ISLNK(opened.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or (reparse_flag and attributes & reparse_flag)
    ):
        print("error: workflow 文件必须是受支持的普通非链接文件", file=sys.stderr)
        return 2

    result = check_workflow(source)
    print(json.dumps(result.report, ensure_ascii=False, indent=2, sort_keys=True))
    status = result.report["status"]
    print(f"workflow readiness: {status}", file=sys.stderr)
    return 0 if status == "ready" else 1


def _publish_package_directory(staging: Path, output: Path, *, force: bool) -> None:
    """Atomically publish staging, optionally replacing one real directory."""

    from ragspine.workflows.errors import WorkflowInputError

    try:
        before = output.lstat()
    except FileNotFoundError:
        staging.replace(output)
        return
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise WorkflowInputError(f"拒绝覆盖链接或非目录: {output}")
    if not force:
        raise WorkflowInputError(f"输出目录已存在: {output}")

    identity = (before.st_dev, before.st_ino)
    current = output.lstat()
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        raise WorkflowInputError(f"输出目录在打包期间被替换: {output}")

    backup = Path(mkdtemp(prefix=f".{output.name}.backup.", dir=output.parent))
    backup.rmdir()
    output.replace(backup)
    try:
        staging.replace(output)
    except Exception:
        backup.replace(output)
        raise
    shutil.rmtree(backup)


def _cmd_workflow_package(args: argparse.Namespace) -> int:
    """Preflight one workflow and publish a self-contained Compose package."""

    from ragspine.workflows.errors import WorkflowError, WorkflowInputError
    from ragspine.workflows.packaging import package_workflow_readiness
    from ragspine.workflows.readiness import check_workflow

    source = Path(args.source)
    output = Path(args.output)
    try:
        if not source.is_file():
            raise WorkflowInputError("workflow 文件不存在或不可读")
        package = package_workflow_readiness(check_workflow(source))
        if not package.ready:
            raise WorkflowInputError("workflow readiness blocked")
        if not output.parent.is_dir():
            raise WorkflowInputError(f"输出目录的父目录不存在: {output.parent}")
        with TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as temporary:
            staging = Path(temporary)
            for name, content in package.files:
                _write_new_workflow(staging / name, content.decode("utf-8"), force=False)
            _publish_package_directory(staging, output, force=args.force)
    except WorkflowError as exc:
        print(f"error: workflow package 失败（{exc.code}）：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: workflow package 输出失败: {exc}", file=sys.stderr)
        return 2

    print(f"created {output}")
    return 0


def _port_available(host: str, port: int) -> bool:
    """端口预检：能 bind 即视为可用（占用时确定性报错，而非留给 uvicorn 晚失败）。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _open_browser_when_ready(url: str, *, host: str, port: int, timeout_s: float = 10.0) -> None:
    """轮询端口就绪后恰好打开一次浏览器；超时放弃（服务本身不受影响）。"""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        return
    webbrowser.open(url)


def _serve_app(app: object, *, host: str, port: int) -> None:
    """真正阻塞的服务启动（可测性 seam：测试 monkeypatch 本函数即可不真起端口）。"""

    import uvicorn

    uvicorn.run(cast(Any, app), host=host, port=port)


def _cmd_workflow_serve(args: argparse.Namespace) -> int:
    """本地一键打开 Studio：解析工作流 → 注册 launch session → 起 API + Studio。

    host 固定 127.0.0.1（PRD 要求，不提供 --host）。URL/query string 只带不透明
    token，工作流内容、文件路径、凭据永不进入。serve 绝不改动
    RAGSPINE_DIFY_RUN_ENABLED——打开图形不等于开启执行。
    """

    from ragspine.workflows.catalog import load_builtin_catalog
    from ragspine.workflows.errors import WorkflowError
    from ragspine.workflows.formats import dump_dify_yaml, load_workflow

    try:
        # [service] extra 延迟 import：未装 fastapi/uvicorn 时其它子命令照常可用。
        from ragspine.service.api.app import create_app
        from ragspine.service.config import ServiceConfig
        from ragspine.service.studio.launch import LaunchSessionRegistry
    except ImportError:
        print(
            'error: workflow serve 需要 [service] extra，请先安装：pip install "rag-spine[service]"',
            file=sys.stderr,
        )
        return 2

    try:
        if _is_workflow_file_argument(args.source):
            path = Path(args.source)
            if not path.exists():
                print(f"error: 文件不存在：{path}", file=sys.stderr)
                return 2
            workflow: dict[str, object] = load_workflow(path)
            name = path.stem
        else:
            template = load_builtin_catalog().get(args.source)
            workflow = template.workflow
            name = template.name
        yaml_text = dump_dify_yaml(workflow)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    host = "127.0.0.1"
    port: int = args.port
    if not _port_available(host, port):
        print(
            f"error: 端口 {port} 已被占用，请用 --port 指定其它端口。",
            file=sys.stderr,
        )
        return 2

    registry = LaunchSessionRegistry()
    session = registry.register(name=name, yaml=yaml_text)
    app = create_app(ServiceConfig.from_env(), launch_sessions=registry)
    url = f"http://{host}:{port}/studio/?launch={session.session_id}"
    print(f"服务监听：http://{host}:{port}")
    print(f"Studio（已自动加载「{name}」）：{url}")
    print("工作流执行默认关闭（RAGSPINE_DIFY_RUN_ENABLED 显式开启才放行）；serve 不改动该配置。")
    if args.open:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(url,),
            kwargs={"host": host, "port": port},
            daemon=True,
        ).start()
    _serve_app(app, host=host, port=port)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragspine",
        description="RAGSpine —— 无框架后端 RAG 引擎的控制台入口（离线优先）。",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True  # 无子命令时报错并非零退出（不静默成功）

    p_quick = sub.add_parser(
        "quickstart",
        help="离线演示：反幻觉 + 来源溯源（零 API key，秒级）",
    )
    p_quick.set_defaults(func=_cmd_quickstart)

    p_ask = sub.add_parser("ask", help="单条提问（默认离线 mock，确定性）")
    p_ask.add_argument("question", help="用户问题，如：中国内地FY2024的REVENUE是多少")
    p_ask.add_argument(
        "--db",
        default=str(DEFAULT_FACT_DB),
        help="fact_metric sqlite 路径（默认 data/fact_metric.db）",
    )
    p_ask.add_argument(
        "--provider",
        choices=["mock", "anthropic"],
        default="mock",
        help="mock=离线确定性（默认）；anthropic=真实 Claude（需装 [llm] + key）",
    )
    p_ask.set_defaults(func=_cmd_ask)

    p_ver = sub.add_parser("version", help="打印分发版本号")
    p_ver.set_defaults(func=_cmd_version)

    # dify：Dify 工作流 JSON/YAML/TOML → 纯 Python 编译器 + 静态优化建议器。
    p_dify = sub.add_parser("dify", help="Dify 工作流编译器（compile / analyze）")
    dify_sub = p_dify.add_subparsers(dest="dify_command", metavar="<subcommand>")
    dify_sub.required = True

    p_compile = dify_sub.add_parser(
        "compile", help="编译 .yml 为纯 Python（默认附静态优化建议到 stderr）"
    )
    p_compile.add_argument("path", help="Dify 工作流 .json/.yaml/.yml/.toml 文件路径")
    p_compile.add_argument("--no-analyze", action="store_true", help="跳过静态优化分析（只出代码）")
    p_compile.set_defaults(func=_cmd_dify_compile)

    p_analyze = dify_sub.add_parser(
        "analyze", help="只跑静态优化分析，打印建议列表（零代码生成、零 API）"
    )
    p_analyze.add_argument("path", help="Dify 工作流 .json/.yaml/.yml/.toml 文件路径")
    p_analyze.set_defaults(func=_cmd_dify_analyze)

    p_workflow = sub.add_parser("workflow", help="从自然语言创建或浏览 Dify 0.6 工作流")
    workflow_sub = p_workflow.add_subparsers(dest="workflow_command", metavar="<subcommand>")
    workflow_sub.required = True

    p_workflow_create = workflow_sub.add_parser(
        "create", help="按描述复用相近模板，否则生成安全的 start→llm→end 工作流"
    )
    p_workflow_create.add_argument(
        "description", nargs="*", help='工作流功能描述（可直接使用 `ragspine "描述"`）'
    )
    p_workflow_create.add_argument("--template", help="跳过匹配，按 catalog template id 创建")
    p_workflow_create.add_argument(
        "--no-reuse", action="store_true", help="不复用模板，强制生成基础工作流"
    )
    p_workflow_create.add_argument(
        "--matcher",
        choices=["lexical", "auto", "onnx"],
        default="auto",
        help=(
            "模板匹配器（默认 auto：装有 [embed-onnx] 时语义匹配并在首次使用时下载/缓存模型，"
            "否则离线回退 lexical；onnx 强制要求该 extra）"
        ),
    )
    p_workflow_create.add_argument(
        "--format", choices=["yaml", "json"], default="yaml", help="输出格式"
    )
    output_group = p_workflow_create.add_mutually_exclusive_group()
    output_group.add_argument("-o", "--output", help="输出文件路径")
    output_group.add_argument("--stdout", action="store_true", help="只把工作流文档写到 stdout")
    p_workflow_create.add_argument(
        "--force", action="store_true", help="覆盖已存在的普通文件（仍拒绝链接）"
    )
    p_workflow_create.set_defaults(func=_cmd_workflow_create)

    p_workflow_list = workflow_sub.add_parser("list", help="列出内置工作流模板")
    p_workflow_list.set_defaults(func=_cmd_workflow_list)

    p_workflow_show = workflow_sub.add_parser("show", help="查看一个内置模板")
    p_workflow_show.add_argument("template_id", help="catalog template id")
    p_workflow_show.add_argument(
        "--format", choices=["yaml", "json"], default="yaml", help="输出格式"
    )
    p_workflow_show.set_defaults(func=_cmd_workflow_show)

    p_workflow_preview = workflow_sub.add_parser(
        "preview", help="输出内置模板或本地工作流文件的版本化只读流程图 JSON"
    )
    p_workflow_preview.add_argument(
        "source", help="catalog template id，或本地 .json/.yaml/.yml/.toml 工作流文件路径"
    )
    p_workflow_preview.set_defaults(func=_cmd_workflow_preview)

    p_workflow_run = workflow_sub.add_parser(
        "run", help="本地一次性执行工作流文件（L0 安全门 + 受限沙箱），输出结果与节点 trace"
    )
    p_workflow_run.add_argument("path", help="本地 .json/.yaml/.yml/.toml 工作流文件路径")
    p_workflow_run.add_argument(
        "--inputs",
        default="{}",
        help='工作流输入，JSON object 字符串（如 \'{"query": "hello"}\'，默认 {}）',
    )
    p_workflow_run.add_argument(
        "--provider",
        choices=["mock", "anthropic"],
        default="mock",
        help="mock=离线确定性（默认）；anthropic=真实 Claude（需装 [llm] + key）",
    )
    p_workflow_run.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="执行超时秒数（默认 10s，与 HTTP 执行面同源）",
    )
    p_workflow_run.set_defaults(func=_cmd_workflow_run)

    p_workflow_check = workflow_sub.add_parser(
        "check",
        help="输出工作流格式、编译与 L0 可运行性 readiness JSON",
    )
    p_workflow_check.add_argument(
        "source", help="本地 .json/.yaml/.yml/.toml 工作流文件路径"
    )
    p_workflow_check.set_defaults(func=_cmd_workflow_check)

    p_workflow_package = workflow_sub.add_parser(
        "package",
        help="校验现有工作流并生成可用 Docker Compose 启动的部署目录",
    )
    p_workflow_package.add_argument(
        "source", help="本地 .json/.yaml/.yml/.toml 工作流文件路径"
    )
    p_workflow_package.add_argument(
        "-o", "--output", required=True, help="新部署目录（默认拒绝覆盖）"
    )
    p_workflow_package.add_argument(
        "--force", action="store_true", help="安全替换已存在的普通目录（仍拒绝链接）"
    )
    p_workflow_package.set_defaults(func=_cmd_workflow_package)

    p_workflow_serve = workflow_sub.add_parser(
        "serve",
        help="本地起 API + Studio 并自动加载工作流（需 [service] extra；host 固定 127.0.0.1）",
    )
    p_workflow_serve.add_argument(
        "source", help="catalog template id，或本地 .json/.yaml/.yml/.toml 工作流文件路径"
    )
    p_workflow_serve.add_argument(
        "--port", type=int, default=8000, help="监听端口（默认 8000；被占用时确定性报错）"
    )
    p_workflow_serve.add_argument(
        "--open", action="store_true", help="服务就绪后自动打开浏览器（恰好一次）"
    )
    p_workflow_serve.set_defaults(func=_cmd_workflow_serve)

    return parser


def _normalize_implicit_workflow(argv: list[str]) -> list[str]:
    """Treat the first unknown positional as a natural-language workflow request."""

    if argv and not argv[0].startswith("-") and argv[0] not in _KNOWN_COMMANDS:
        return ["workflow", "create", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并分发到子命令处理器，返回进程退出码。"""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(_normalize_implicit_workflow(raw_argv))
    func = args.func
    result: int = func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
