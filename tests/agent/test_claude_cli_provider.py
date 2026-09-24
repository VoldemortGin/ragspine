"""ClaudeCliProvider 测试：subprocess 全部 monkeypatch，不真调用 claude。

真实调用的 smoke 用例标 network 且需 RAGSPINE_CLAUDE_CLI_SMOKE=1 显式开启，默认跳过。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ChatCompletion, ProviderError

from ragspine.agent import claude_cli_provider as mod
from ragspine.agent.claude_cli_provider import ClaudeCliProvider
from ragspine.agent.query_tools import QUERY_METRIC_TOOL_OPENAI

TOOLS = [QUERY_METRIC_TOOL_OPENAI]


def _payload(result: str, **extra: object) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": result,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3},
            "modelUsage": {"claude-test": {}},
            **extra,
        }
    )


class FakeRun:
    """记录每次 subprocess.run 调用，按脚本依次返回 stdout。"""

    def __init__(self, *outputs: str, returncode: int = 0, stderr: str = "") -> None:
        self.outputs = list(outputs)
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        sys_file = Path(cmd[cmd.index("--system-prompt-file") + 1])
        self.calls.append(
            {
                "cmd": cmd,
                "input": kwargs["input"],
                "cwd_listing": sorted(p.name for p in cwd.iterdir()),
                "system": sys_file.read_text(encoding="utf-8"),
                "timeout": kwargs["timeout"],
            }
        )
        return SimpleNamespace(
            returncode=self.returncode, stdout=self.outputs.pop(0), stderr=self.stderr
        )


@pytest.fixture
def fake_bin(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/fake/bin/{name}")


def _install(monkeypatch, fake: FakeRun) -> FakeRun:
    monkeypatch.setattr(mod.subprocess, "run", fake)
    return fake


def test_import_and_construct_do_not_touch_claude(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("不应在构造时探测/调用 claude")

    monkeypatch.setattr(mod.shutil, "which", boom)
    monkeypatch.setattr(mod.subprocess, "run", boom)
    ClaudeCliProvider(model="sonnet")


def test_missing_binary_raises_clear_error_at_call_time(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    provider = ClaudeCliProvider(claude_bin="no-such-claude")
    with pytest.raises(FileNotFoundError, match="no-such-claude"):
        provider.chat([{"role": "user", "content": "hi"}])


def test_text_chat_isolated_command_and_parsing(monkeypatch, fake_bin):
    fake = _install(monkeypatch, FakeRun(_payload("你好")))
    resp = ClaudeCliProvider(timeout=12.0).chat(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "问题"}]
    )

    assert isinstance(resp, ChatCompletion)
    msg = resp.choices[0].message
    assert msg.content == "你好" and not msg.tool_calls
    assert resp.choices[0].finish_reason == "stop"
    assert resp.model == "claude-test"
    assert resp.usage is not None and resp.usage.prompt_tokens == 15
    assert resp.usage.completion_tokens == 3 and resp.usage.total_tokens == 18

    call = fake.calls[0]
    cmd = call["cmd"]
    assert cmd[0] == "/fake/bin/claude" and "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in cmd
    assert "--model" not in cmd  # 默认不指定模型
    assert call["input"] == "问题"  # prompt 走 stdin，不进 argv
    assert "问题" not in cmd
    assert call["system"] == "SYS"
    assert call["cwd_listing"] == []  # cwd 是全新空目录
    assert call["timeout"] == 12.0


def test_model_flag_passed_when_configured(monkeypatch, fake_bin):
    fake = _install(monkeypatch, FakeRun(_payload("ok")))
    ClaudeCliProvider(model="sonnet").chat([{"role": "user", "content": "q"}])
    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_fallback_system_prompt_when_none_given(monkeypatch, fake_bin):
    fake = _install(monkeypatch, FakeRun(_payload("ok")))
    ClaudeCliProvider().chat([{"role": "user", "content": "q"}])
    assert fake.calls[0]["system"].strip()


def test_verbose_event_array_output_is_parsed(monkeypatch, fake_bin):
    events = json.dumps([{"type": "system"}, json.loads(_payload("arr"))])
    _install(monkeypatch, FakeRun(events))
    resp = ClaudeCliProvider().chat([{"role": "user", "content": "q"}])
    assert resp.choices[0].message.content == "arr"


def test_nonzero_exit_maps_to_provider_error(monkeypatch, fake_bin):
    _install(monkeypatch, FakeRun("", returncode=1, stderr="auth failed"))
    with pytest.raises(ProviderError, match="auth failed"):
        ClaudeCliProvider().chat([{"role": "user", "content": "q"}])


def test_timeout_maps_to_provider_error(monkeypatch, fake_bin):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(mod.subprocess, "run", slow)
    with pytest.raises(ProviderError, match="超时"):
        ClaudeCliProvider(timeout=1.0).chat([{"role": "user", "content": "q"}])


def test_is_error_and_garbage_output_map_to_provider_error(monkeypatch, fake_bin):
    _install(monkeypatch, FakeRun(_payload("boom", is_error=True)))
    with pytest.raises(ProviderError):
        ClaudeCliProvider().chat([{"role": "user", "content": "q"}])
    _install(monkeypatch, FakeRun("not json"))
    with pytest.raises(ProviderError):
        ClaudeCliProvider().chat([{"role": "user", "content": "q"}])


def test_tool_call_emulation(monkeypatch, fake_bin):
    reply = '```json\n{"tool_calls": [{"name": "query_metric", "arguments": {"metric": "REVENUE"}}]}\n```'
    fake = _install(monkeypatch, FakeRun(_payload(reply)))
    resp = ClaudeCliProvider().chat([{"role": "user", "content": "q"}], tools=TOOLS)

    choice = resp.choices[0]
    assert choice.finish_reason == "tool_calls"
    (tc,) = choice.message.tool_calls
    assert tc.function.name == "query_metric"
    assert json.loads(tc.function.arguments) == {"metric": "REVENUE"}
    assert "query_metric" in fake.calls[0]["system"]  # 工具 schema 进了系统提示


def test_tool_mode_final_content(monkeypatch, fake_bin):
    _install(monkeypatch, FakeRun(_payload('{"content": "最终回答"}')))
    resp = ClaudeCliProvider().chat([{"role": "user", "content": "q"}], tools=TOOLS)
    assert resp.choices[0].message.content == "最终回答"
    assert not resp.choices[0].message.tool_calls
    assert resp.choices[0].finish_reason == "stop"


def test_tool_mode_retries_bad_format_then_succeeds(monkeypatch, fake_bin):
    fake = _install(
        monkeypatch,
        FakeRun(
            _payload("我觉得是 100"),
            _payload('{"tool_calls": [{"name": "nope", "arguments": {}}]}'),
            _payload('{"content": "ok"}'),
        ),
    )
    resp = ClaudeCliProvider(format_retries=2).chat([{"role": "user", "content": "q"}], tools=TOOLS)
    assert resp.choices[0].message.content == "ok"
    assert len(fake.calls) == 3
    assert "不符合工具调用协议" in fake.calls[1]["input"]


def test_tool_mode_gives_up_after_bounded_retries(monkeypatch, fake_bin):
    fake = _install(monkeypatch, FakeRun(*[_payload("no json")] * 2))
    with pytest.raises(ProviderError, match="工具调用协议"):
        ClaudeCliProvider(format_retries=1).chat([{"role": "user", "content": "q"}], tools=TOOLS)
    assert len(fake.calls) == 2


def test_multi_turn_transcript_replays_tool_results(monkeypatch, fake_bin):
    fake = _install(monkeypatch, FakeRun(_payload('{"content": "done"}')))
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "香港去年REVENUE多少"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "toolu_cli_1",
                    "type": "function",
                    "function": {"name": "query_metric", "arguments": '{"metric": "REVENUE"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_cli_1", "content": '{"status": "found"}'},
    ]
    ClaudeCliProvider().chat(messages, tools=TOOLS)
    prompt = fake.calls[0]["input"]
    assert "香港去年REVENUE多少" in prompt
    assert "[tool_call id=toolu_cli_1]" in prompt
    assert '[tool_result id=toolu_cli_1]\n{"status": "found"}' in prompt
    assert "SYS" not in prompt  # system 只走 system prompt 文件


def test_invalid_concurrency_rejected():
    with pytest.raises(ValueError):
        ClaudeCliProvider(max_concurrency=0)


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("RAGSPINE_CLAUDE_CLI_SMOKE") != "1" or shutil.which("claude") is None,
    reason="真实 claude -p smoke：需本机已登录 claude 且 RAGSPINE_CLAUDE_CLI_SMOKE=1",
)
def test_live_claude_cli_smoke():
    provider = ClaudeCliProvider(timeout=180.0)
    resp = provider.chat(
        [
            {"role": "system", "content": "Reply in English only."},
            {"role": "user", "content": "Reply with exactly the word PONG."},
        ]
    )
    assert "PONG" in (resp.choices[0].message.content or "")
    resp = provider.chat(
        [{"role": "user", "content": "中国内地FY2024的REVENUE是多少"}], tools=TOOLS
    )
    assert resp.choices[0].message.tool_calls


# ---------------------------------------------------------------------------
# 图文混合：页图复制进临时 cwd，只放开 Read 工具，其余隔离不变
# ---------------------------------------------------------------------------


class FakeRunWithFiles(FakeRun):
    """额外记录 cwd 里每个文件的字节。"""

    def __call__(self, cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        self.files = {p.name: p.read_bytes() for p in cwd.iterdir()}
        return super().__call__(cmd, **kwargs)


def _image_messages(tmp_path):
    a = tmp_path / "store" / "aa" / "hash-a.png"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"PNG-A")
    b = tmp_path / "store" / "hash-b.png"
    b.write_bytes(b"PNG-B")
    return [
        {"role": "system", "content": "SYS"},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "问题\n[1] 片段（来源：deck.md deck.md@page=18#para1）\n图：p18.png",
                },
                {
                    "type": "image",
                    "path": str(a),
                    "name": "p18.png",
                    "doc_id": "deck.md",
                    "page": 18,
                },
                {"type": "image", "path": str(b), "name": "p3.png", "doc_id": "deck.md", "page": 3},
            ],
        },
    ]


def test_declares_image_support():
    from ragspine.agent.llm_provider import provider_supports_images

    assert provider_supports_images(ClaudeCliProvider())


def test_images_are_copied_into_empty_cwd_and_only_read_is_enabled(monkeypatch, fake_bin, tmp_path):
    fake = _install(monkeypatch, FakeRunWithFiles(_payload("72%")))
    resp = ClaudeCliProvider().chat(_image_messages(tmp_path))
    assert resp.choices[0].message.content == "72%"

    call = fake.calls[0]
    cmd = call["cmd"]
    assert call["cwd_listing"] == ["p18.png", "p3.png"]
    assert fake.files == {"p18.png": b"PNG-A", "p3.png": b"PNG-B"}
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    assert "--allowedTools" not in cmd and "--allowed-tools" not in cmd
    assert "--add-dir" not in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in cmd
    prompt = call["input"]
    assert prompt.startswith("问题\n[1] 片段（来源：deck.md deck.md@page=18#para1）\n图：p18.png")
    assert "Read" in prompt and "p18.png" in prompt and "p3.png" in prompt
    # 原图的存储路径不外泄给模型
    assert str(tmp_path) not in prompt and all(str(tmp_path) not in c for c in cmd)
    assert call["system"] == "SYS"


def test_text_only_parts_behave_like_a_string(monkeypatch, fake_bin):
    fake = _install(monkeypatch, FakeRun(_payload("ok"), _payload("ok")))
    ClaudeCliProvider().chat([{"role": "user", "content": [{"type": "text", "text": "问题"}]}])
    ClaudeCliProvider().chat([{"role": "user", "content": "问题"}])
    first, second = fake.calls

    def _argv(cmd):  # system prompt 文件在每次调用的临时目录里，路径不同
        i = cmd.index("--system-prompt-file")
        return cmd[:i] + cmd[i + 2 :]

    assert _argv(first["cmd"]) == _argv(second["cmd"])
    assert first["input"] == second["input"] == "问题"
    assert first["cmd"][first["cmd"].index("--tools") + 1] == ""
    assert "--permission-prompts" not in first["cmd"]


@pytest.mark.parametrize("name", ["../p1.png", "sub/p1.png", "", ".hidden.png", "p1.txt"])
def test_unsafe_image_names_are_rejected(monkeypatch, fake_bin, tmp_path, name):
    _install(monkeypatch, FakeRun(_payload("ok")))
    img = tmp_path / "x.png"
    img.write_bytes(b"x")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q"},
                {"type": "image", "path": str(img), "name": name},
            ],
        }
    ]
    with pytest.raises(ValueError):
        ClaudeCliProvider().chat(messages)


def test_duplicate_image_names_are_rejected(monkeypatch, fake_bin, tmp_path):
    _install(monkeypatch, FakeRun(_payload("ok")))
    img = tmp_path / "x.png"
    img.write_bytes(b"x")
    part = {"type": "image", "path": str(img), "name": "p1.png"}
    with pytest.raises(ValueError):
        ClaudeCliProvider().chat(
            [{"role": "user", "content": [{"type": "text", "text": "q"}, part, part]}]
        )


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("RAGSPINE_CLAUDE_CLI_SMOKE") != "1" or shutil.which("claude") is None,
    reason="真实 claude -p 读图 smoke：需本机已登录 claude 且 RAGSPINE_CLAUDE_CLI_SMOKE=1",
)
def test_live_claude_cli_reads_page_image(tmp_path):
    from ragspine.ingestion.page_images.render import render_pdf_pages
    from tests.ingestion.page_images.fixtures import make_pdf

    pdf = make_pdf(tmp_path / "a.pdf", ["Codeword: ZEBRA-4417"])
    png = tmp_path / "img.png"
    png.write_bytes(render_pdf_pages(pdf, dpi=144, max_side=1568)[0].png)
    resp = ClaudeCliProvider(timeout=180.0).chat(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "What codeword is printed in p1.png? Reply with the codeword only.",
                    },
                    {"type": "image", "path": str(png), "name": "p1.png"},
                ],
            }
        ]
    )
    assert "ZEBRA-4417" in (resp.choices[0].message.content or "")
