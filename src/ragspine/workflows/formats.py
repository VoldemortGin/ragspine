"""Bounded JSON/YAML/TOML workflow ingress and deterministic wire dumps."""

from __future__ import annotations

import json
import math
import os
import stat
import tomllib
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from ragspine.workflows.errors import WorkflowFormatError

WorkflowFormat = Literal["json", "yaml", "toml"]
MAX_WORKFLOW_BYTES = 1024 * 1024
MAX_WORKFLOW_DEPTH = 32
MAX_WORKFLOW_NODES = 20_000
MAX_WORKFLOW_STRING_CHARS = 256 * 1024
MAX_YAML_ALIASES = 64


def parse_workflow(
    source: str | bytes, *, format: WorkflowFormat | None = None
) -> dict[str, object]:
    """Parse one bounded workflow document into a JSON-compatible mapping."""

    text = _bounded_text(source)
    selected = format or _detect_format(text)
    if selected not in {"json", "yaml", "toml"}:
        raise WorkflowFormatError(f"未知 workflow format: {selected!r}；可用 json/yaml/toml")
    try:
        if selected == "json":
            value = json.loads(
                text,
                object_pairs_hook=_json_object,
                parse_constant=_reject_json_constant,
            )
        elif selected == "yaml":
            aliases = sum(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text))
            if aliases > MAX_YAML_ALIASES:
                raise WorkflowFormatError(f"YAML alias 数超过上限 {MAX_YAML_ALIASES}: {aliases}")
            loader = _UniqueKeySafeLoader(text)
            try:
                value = loader.get_single_data()
            finally:
                loader.dispose()
        else:
            value = tomllib.loads(text)
    except WorkflowFormatError:
        raise
    except (yaml.YAMLError, OverflowError, ValueError) as exc:
        # Parser exceptions can embed the offending line verbatim (including an API key).
        # Preserve the exception chain for local debugging but keep the public message opaque.
        raise WorkflowFormatError(f"{selected} workflow 解析失败") from exc
    except (RecursionError, MemoryError) as exc:
        raise WorkflowFormatError(f"{selected} workflow 结构过深或资源消耗过大") from exc

    try:
        normalized = _validate_json_value(value)
    except (RecursionError, MemoryError) as exc:
        raise WorkflowFormatError("workflow 结构过深或资源消耗过大") from exc
    if not isinstance(normalized, dict):
        raise WorkflowFormatError("workflow 文档根必须是 mapping/object")
    return normalized


def load_workflow(path: str | Path) -> dict[str, object]:
    """Read a workflow file without following a symlink and parse by suffix."""

    resolved = Path(path)
    suffixes: dict[str, WorkflowFormat] = {
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
    }
    selected = suffixes.get(resolved.suffix.lower())
    if selected is None:
        raise WorkflowFormatError(
            f"不支持 workflow 后缀 {resolved.suffix!r}；可用 .json/.yaml/.yml/.toml"
        )
    data = read_bounded_file(resolved, limit=MAX_WORKFLOW_BYTES, label=str(resolved))
    return parse_workflow(data, format=selected)


def read_bounded_file(path: Path, *, limit: int, label: str) -> bytes:
    """Read a stable regular file descriptor without following links."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise WorkflowFormatError(f"读取文件失败: {label}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or (reparse_flag and file_attributes & reparse_flag)
    ):
        raise WorkflowFormatError(f"文件必须是普通非链接文件: {label}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WorkflowFormatError(f"安全打开文件失败: {label}: {exc}") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise WorkflowFormatError(f"打开后不是普通文件: {label}")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise WorkflowFormatError(f"文件在检查与打开之间被替换: {label}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise WorkflowFormatError(f"文件超过 {limit} bytes: {label}")
    return data


def dump_json(workflow: dict[str, object], *, pretty: bool = True) -> str:
    """Serialize a validated JSON-compatible mapping with NaN disabled."""

    value = _validate_json_value(workflow)
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        + "\n"
    )


def dump_dify_yaml(workflow: dict[str, object]) -> str:
    """Serialize a validated mapping as stable, alias-free UTF-8 Dify YAML."""

    value = _validate_json_value(workflow)

    class _NoAliasSafeDumper(yaml.SafeDumper):  # type: ignore[misc]
        def ignore_aliases(self, data: object) -> bool:
            return True

    dumped = yaml.dump(
        value,
        Dumper=_NoAliasSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return cast(str, dumped)


def _bounded_text(source: str | bytes) -> str:
    if isinstance(source, bytes):
        if len(source) > MAX_WORKFLOW_BYTES:
            raise WorkflowFormatError(f"workflow 超过 {MAX_WORKFLOW_BYTES} bytes")
        try:
            return source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowFormatError("workflow 必须是 UTF-8") from exc
    try:
        encoded = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkflowFormatError("workflow 必须是合法 UTF-8/Unicode 文本") from exc
    if len(encoded) > MAX_WORKFLOW_BYTES:
        raise WorkflowFormatError(f"workflow 超过 {MAX_WORKFLOW_BYTES} bytes")
    return source


def _detect_format(text: str) -> WorkflowFormat:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return "json"
    return "yaml"


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise WorkflowFormatError("JSON object 含重复键")
        out[key] = value
    return out


def _reject_json_constant(value: str) -> object:
    raise WorkflowFormatError(f"JSON 不允许 {value}")


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that rejects duplicate keys, including merged mappings."""

    def construct_mapping(
        self, node: yaml.nodes.MappingNode, deep: bool = False
    ) -> dict[object, object]:
        self.flatten_mapping(node)
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise WorkflowFormatError("YAML mapping key 必须可哈希") from exc
            if duplicate:
                raise WorkflowFormatError("YAML mapping 含重复键")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _validate_json_value(value: object) -> Any:
    count = 0

    def visit(item: object, depth: int) -> Any:
        nonlocal count
        count += 1
        if count > MAX_WORKFLOW_NODES:
            raise WorkflowFormatError(f"workflow 结构节点超过上限 {MAX_WORKFLOW_NODES}")
        if depth > MAX_WORKFLOW_DEPTH:
            raise WorkflowFormatError(f"workflow 嵌套超过上限 {MAX_WORKFLOW_DEPTH}")
        if item is None or isinstance(item, bool | int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise WorkflowFormatError("workflow 不允许 NaN/Inf")
            return item
        if isinstance(item, str):
            if len(item) > MAX_WORKFLOW_STRING_CHARS:
                raise WorkflowFormatError(f"workflow 字符串超过 {MAX_WORKFLOW_STRING_CHARS} 字符")
            return item
        if isinstance(item, list | tuple):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, dict):
            out: dict[str, object] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise WorkflowFormatError("workflow mapping key 必须是字符串")
                if len(key) > MAX_WORKFLOW_STRING_CHARS:
                    raise WorkflowFormatError("workflow mapping key 过长")
                out[key] = visit(child, depth + 1)
            return out
        raise WorkflowFormatError(f"workflow 含非 JSON-compatible 类型: {type(item).__name__}")

    return visit(value, 0)


__all__ = [
    "WorkflowFormat",
    "MAX_WORKFLOW_BYTES",
    "MAX_WORKFLOW_DEPTH",
    "MAX_WORKFLOW_NODES",
    "MAX_WORKFLOW_STRING_CHARS",
    "MAX_YAML_ALIASES",
    "parse_workflow",
    "load_workflow",
    "read_bounded_file",
    "dump_json",
    "dump_dify_yaml",
]
