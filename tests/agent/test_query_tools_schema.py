"""冻结金标：query_metric 工具 schema（ADR 0004 pin-before-touch）。

ADR 0004 会把工具 schema 改为从 DomainProfile.dimensions 动态生成。本测试钉住当前
（金融 ACME 实例）的生成结果【字节级不变】：属性插入顺序 metric→entity→period→channel、
required==[metric,entity,period]、以及含实体/指标同义词的逐字 description。
金标存 data/golden/query_metric_tool_schema.json（由当前值生成、force-tracked），
是后续"动态生成字节级等价"的回归门。
"""

import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.query_tools import QUERY_METRIC_TOOL_ANTHROPIC, QUERY_METRIC_TOOL_OPENAI

_GOLDEN = json.loads(
    (ROOT_DIR / "data" / "golden" / "query_metric_tool_schema.json").read_text(encoding="utf-8")
)


def test_anthropic_tool_schema_byte_identical():
    assert QUERY_METRIC_TOOL_ANTHROPIC == _GOLDEN["anthropic"]


def test_openai_tool_schema_byte_identical():
    assert QUERY_METRIC_TOOL_OPENAI == _GOLDEN["openai"]


def test_tool_schema_structural_invariants():
    """属性顺序 + required 是动态生成必须复现的硬结构。"""
    sch = QUERY_METRIC_TOOL_ANTHROPIC["input_schema"]
    assert QUERY_METRIC_TOOL_ANTHROPIC["name"] == "query_metric"
    assert list(sch["properties"].keys()) == ["metric", "entity", "period", "channel"]
    assert sch["required"] == ["metric", "entity", "period"]

    fn = QUERY_METRIC_TOOL_OPENAI["function"]
    assert fn["name"] == "query_metric"
    assert list(fn["parameters"]["properties"].keys()) == ["metric", "entity", "period", "channel"]
    assert fn["parameters"]["required"] == ["metric", "entity", "period"]
