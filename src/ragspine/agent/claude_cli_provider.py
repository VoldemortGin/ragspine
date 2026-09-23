"""ClaudeCliProvider：用本机 Claude Code CLI（`claude -p`）子进程模拟 LLM，供评测使用。

实现 corespine `LLMProvider.chat(messages, *, tools=None) -> ChatCompletion`（OpenAI 形状），
零 SDK，只用 subprocess。

能力与限制（如实标注）：
- 文本补全：支持。system 消息合并后经 `--system-prompt-file` 传入（**替换** Claude Code 默认系统提示）；
  其余消息渲染成一段对话记录经 stdin 传入（避开命令行长度限制）。
- tool calling：**提示词模拟**。CLI 无原生 function-calling 回传，传入 tools 时追加一段格式约束，
  要求模型只输出一个 JSON 对象（`{"tool_calls": [...]}` 或 `{"content": "..."}`），本地解析 + 校验
  （工具名必须在 tools 内、arguments 必须是对象），不合格则带纠错提示有限次重试，仍不合格抛
  ProviderError（交给编排层诚实降级）。每轮只是一次独立的无状态 CLI 调用，多轮工具循环靠重放整段
  对话记录实现。
- 不支持：流式（不实现 StreamingProvider）、采样参数（temperature / max_tokens 由 CLI 决定）、
  原生 JSON mode（JSON 由提示词约束 + 解析保证）。

隔离：每次调用在全新临时空目录里跑（不发现任何项目 CLAUDE.md）；`--setting-sources ""` 不加载
user/project/local 设置（实测这是屏蔽 ~/.claude/CLAUDE.md 与 settings 里 `language` 等全局指令的
关键——`--safe-mode` 仍会注入 settings 的 language 指令）；`--tools ""` 禁用全部内置工具；
`--strict-mcp-config` 不加载 MCP；`--disable-slash-commands` 禁用 skills；`--no-session-persistence`
不落盘会话。认证沿用 CLI 自身登录态（OAuth / keychain 不依赖 settings 文件）。

lazy：import 本模块不检查 claude 是否存在；首次 chat 时用 shutil.which 解析，找不到抛 FileNotFoundError
（配置错误，不归 ProviderError，免得被降级路径吞掉）。
"""

import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from corespine import (
    ChatCompletion,
    Choice,
    FunctionCall,
    ProviderError,
    ResponseMessage,
    ToolCall,
    Usage,
)

DEFAULT_CLAUDE_CLI_TIMEOUT_S = 300.0
DEFAULT_CLAUDE_CLI_CONCURRENCY = 4
DEFAULT_FORMAT_RETRIES = 2
# 无 system 消息时也必须显式给一段系统提示，以替换掉 Claude Code 的默认（编码代理）系统提示。
_FALLBACK_SYSTEM = "You are a helpful assistant."

_TOOL_PROTOCOL = """\

## 工具调用协议（必须严格遵守）
你可以调用以下工具（OpenAI function 形状的 JSON Schema）：
{tools}

你的整条回复必须是且只能是一个 JSON 对象，不要有任何额外文字或 Markdown 代码块：
- 需要调用工具时：{{"tool_calls": [{{"name": "<工具名>", "arguments": {{<符合该工具 parameters 的参数>}}}}]}}
- 已能给出最终回答时：{{"content": "<最终回答文本>"}}
对话记录中的 [tool_result] 段是工具返回结果；拿到所需结果后请给出最终回答，不要重复调用同一工具。"""

_FORMAT_FIX = (
    "你上一条回复不符合工具调用协议（{error}）。请只输出一个合法 JSON 对象："
    '{{"tool_calls": [...]}} 或 {{"content": "..."}}。'
)


def _render_transcript(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """OpenAI messages → (system 文本, stdin 对话记录)。单条 user 消息时原样透传其内容。"""
    system_parts: list[str] = []
    convo: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(str(m.get("content") or ""))
        else:
            convo.append(m)
    system = "\n".join(p for p in system_parts if p)
    if len(convo) == 1 and convo[0].get("role") == "user":
        return system, str(convo[0].get("content") or "")

    blocks: list[str] = []
    for m in convo:
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool":
            blocks.append(f"[tool_result id={m.get('tool_call_id', '')}]\n{content}")
        elif role == "assistant":
            parts = [content] if content else []
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                call = {"name": fn["name"], "arguments": json.loads(fn.get("arguments") or "{}")}
                parts.append(f"[tool_call id={tc['id']}] {json.dumps(call, ensure_ascii=False)}")
            blocks.append("[assistant]\n" + "\n".join(parts))
        else:
            blocks.append(f"[user]\n{content}")
    return system, "\n\n".join(blocks)


def _extract_json_object(text: str) -> dict[str, Any]:
    """从模型文本里取第一个 JSON 对象（容忍 ```json 围栏与前后杂字）；取不到抛 ValueError。"""
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    raise ValueError("回复中没有 JSON 对象")


def _parse_tool_reply(text: str, tool_names: set[str]) -> ResponseMessage:
    """按工具调用协议解析模型回复；不合格抛 ValueError（由调用方重试）。"""
    obj = _extract_json_object(text)
    calls = obj.get("tool_calls")
    if calls:
        if not isinstance(calls, list):
            raise ValueError("tool_calls 必须是数组")
        parsed: list[ToolCall] = []
        for i, call in enumerate(calls, start=1):
            if not isinstance(call, dict):
                raise ValueError("tool_calls 元素必须是对象")
            name = call.get("name")
            args = call.get("arguments", {})
            if name not in tool_names:
                raise ValueError(f"未知工具 {name!r}")
            if not isinstance(args, dict):
                raise ValueError("arguments 必须是 JSON 对象")
            parsed.append(
                ToolCall(
                    id=f"toolu_cli_{i}",
                    function=FunctionCall(
                        name=str(name), arguments=json.dumps(args, ensure_ascii=False)
                    ),
                )
            )
        return ResponseMessage(role="assistant", content=None, tool_calls=tuple(parsed))
    content = obj.get("content")
    if not isinstance(content, str):
        raise ValueError('缺少 "tool_calls" 或字符串 "content"')
    return ResponseMessage(role="assistant", content=content)


def _tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("function", tool)["name"])


class ClaudeCliProvider:
    """`claude -p` 子进程 provider（评测用）。能力边界见模块 docstring。"""

    def __init__(
        self,
        model: str | None = None,
        *,
        claude_bin: str = "claude",
        timeout: float = DEFAULT_CLAUDE_CLI_TIMEOUT_S,
        max_concurrency: int = DEFAULT_CLAUDE_CLI_CONCURRENCY,
        format_retries: int = DEFAULT_FORMAT_RETRIES,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self.model = model
        self.claude_bin = claude_bin
        self.timeout = timeout
        self.format_retries = format_retries
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def _resolve_bin(self) -> str:
        path = shutil.which(self.claude_bin)
        if path is None:
            raise FileNotFoundError(
                f"找不到 Claude Code CLI 可执行文件 {self.claude_bin!r}：请先安装并登录 "
                "Claude Code（https://docs.claude.com/claude-code），或改用 --provider mock。"
            )
        return path

    def _run(self, system: str, prompt: str) -> tuple[str, Usage | None, str]:
        """跑一次 `claude -p`，返回 (结果文本, usage, 模型名)。失败归一到 ProviderError。"""
        exe = self._resolve_bin()
        with self._slots, tempfile.TemporaryDirectory(prefix="ragspine-claude-cli-") as tmp:
            workdir = Path(tmp) / "cwd"
            workdir.mkdir()
            system_file = Path(tmp) / "system.txt"
            system_file.write_text(system or _FALLBACK_SYSTEM, encoding="utf-8")
            cmd = [
                exe,
                "-p",
                "--output-format",
                "json",
                "--system-prompt-file",
                str(system_file),
                "--tools",
                "",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-session-persistence",
            ]
            if self.model:
                cmd += ["--model", self.model]
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    cwd=workdir,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"claude -p 超时（{self.timeout}s）") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise ProviderError(f"claude -p 退出码 {proc.returncode}：{detail}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"claude -p 输出不是 JSON：{proc.stdout[:200]!r}") from exc
        # verbose 配置下 json 输出是事件数组，取其中 type=result 的那条。
        if isinstance(payload, list):
            payload = next(
                (e for e in payload if isinstance(e, dict) and e.get("type") == "result"), None
            )
        if not isinstance(payload, dict) or payload.get("is_error"):
            raise ProviderError(f"claude -p 返回错误：{str(payload)[:500]}")
        result = payload.get("result")
        if not isinstance(result, str):
            raise ProviderError("claude -p 结果缺少文本 result 字段")
        return result, _usage(payload), _model_name(payload, self.model)

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatCompletion:
        system, prompt = _render_transcript(messages)
        if not tools:
            text, usage, model = self._run(system, prompt)
            return _completion(ResponseMessage(role="assistant", content=text), usage, model)

        system = system + _TOOL_PROTOCOL.format(tools=json.dumps(tools, ensure_ascii=False))
        names = {_tool_name(t) for t in tools}
        attempt_prompt = prompt
        last_error = ""
        for _ in range(self.format_retries + 1):
            text, usage, model = self._run(system, attempt_prompt)
            try:
                message = _parse_tool_reply(text, names)
            except ValueError as exc:
                last_error = str(exc)
                attempt_prompt = (
                    f"{prompt}\n\n[assistant]\n{text}\n\n[user]\n"
                    + _FORMAT_FIX.format(error=last_error)
                )
                continue
            return _completion(message, usage, model)
        raise ProviderError(f"claude -p 回复多次不符合工具调用协议：{last_error}")


def _usage(payload: dict[str, Any]) -> Usage | None:
    u = payload.get("usage")
    if not isinstance(u, dict):
        return None
    prompt_tokens = sum(
        int(u.get(k) or 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    completion_tokens = int(u.get("output_tokens") or 0)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _model_name(payload: dict[str, Any], configured: str | None) -> str:
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        return str(next(iter(model_usage)))
    return configured or "claude-cli"


def _completion(message: ResponseMessage, usage: Usage | None, model: str) -> ChatCompletion:
    finish = "tool_calls" if message.tool_calls else "stop"
    return ChatCompletion(
        choices=(Choice(index=0, message=message, finish_reason=finish),),
        usage=usage,
        model=model,
    )
