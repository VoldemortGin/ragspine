"""P5 静态优化器验收：8 条规则各自命中/不命中、analyzer 排序去重、env 注入。

纯静态：不跑任何 provider、不读真实环境变量（上限经 OptimizeEnv 注入）。每条规则用最小输入
（已有 fixture 或内联 DSL）触发，断言对应 rule_id 出现；并断言不该触发时不触发。
"""

from __future__ import annotations

from collections.abc import Callable

from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.optimize.analyzer import analyze_ir
from ragspine.dify.optimize.rules import OptimizeEnv
from ragspine.dify.optimize.suggestion import Severity, Suggestion
from ragspine.dify.parse.loader import parse_dify_yaml


def _analyze(dsl: str, *, env: OptimizeEnv | None = None) -> list[Suggestion]:
    return analyze_ir(lower_to_ir(parse_dify_yaml(dsl)), env=env)


def _ids(suggestions: list[Suggestion]) -> set[str]:
    return {s.rule_id for s in suggestions}


def _by_rule(suggestions: list[Suggestion], rule_id: str) -> list[Suggestion]:
    return [s for s in suggestions if s.rule_id == rule_id]


# ---------------------------------------------------------------------------
# PARALLEL_001：parallel fixture 的 {llm_a, llm_b} 层。
# ---------------------------------------------------------------------------


def test_parallel_001_fires_on_parallel_fixture(
    fixture_text: Callable[[str], str],
) -> None:
    out = _analyze(fixture_text("parallel"))
    hits = _by_rule(out, "PARALLEL_001")
    assert hits, "PARALLEL_001 应在 parallel fixture 触发"
    node_ids = set(hits[0].node_ids)
    assert {"llm_a", "llm_b"} <= node_ids
    assert hits[0].severity is Severity.HIGH


def test_parallel_001_quiet_on_seq(fixture_text: Callable[[str], str]) -> None:
    """顺序型只有单 llm，无并行层 ≥2 重节点 → 不触发。"""
    assert "PARALLEL_001" not in _ids(_analyze(fixture_text("seq")))


def test_parallel_001_excludes_mutually_exclusive_branches(
    fixture_text: Callable[[str], str],
) -> None:
    """branch fixture 的 llm_yes/llm_no 处同一 if-else 的不同分支，运行期只走其一 →
    虽同处一个 parallel_layer，也【不】算并发机会，PARALLEL_001 不触发。"""
    assert "PARALLEL_001" not in _ids(_analyze(fixture_text("branch")))


# ---------------------------------------------------------------------------
# PARALLEL_002：串行迭代 + 内层 llm 触发；iteration fixture（已并行）不触发。
# ---------------------------------------------------------------------------

_SERIAL_ITER_LLM = """
app:
  mode: workflow
  name: iter-serial-llm
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: items, type: array}]}
      - id: iter_1
        data:
          type: iteration
          title: 串行迭代
          iterator_selector: [start_1, items]
          output_selector: [iter_inner, text]
          is_parallel: false
          start_node_id: iter_inner
      - id: iter_inner
        data:
          type: llm
          title: 内层
          iteration_id: iter_1
          model: {provider: anthropic, name: claude-opus-4-8, completion_params: {max_tokens: 256}}
          prompt_template: [{role: user, text: "处理 {{#iter_1.item#}}"}]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [iter_1, output]}]}
    edges:
      - {source: start_1, target: iter_1, sourceHandle: source}
      - {source: iter_1, target: end_1, sourceHandle: source}
"""


def test_parallel_002_fires_on_serial_iteration_with_llm() -> None:
    out = _analyze(_SERIAL_ITER_LLM)
    hits = _by_rule(out, "PARALLEL_002")
    assert hits, "PARALLEL_002 应在串行迭代+内层 llm 触发"
    assert hits[0].node_ids == ("iter_1",)
    assert hits[0].severity is Severity.MEDIUM


def test_parallel_002_quiet_on_parallel_iteration(
    fixture_text: Callable[[str], str],
) -> None:
    """iteration fixture 已 is_parallel=true → 不触发。"""
    assert "PARALLEL_002" not in _ids(_analyze(fixture_text("iteration")))


# ---------------------------------------------------------------------------
# BOTTLE_001：3 个 LLM 串行链。
# ---------------------------------------------------------------------------

_THREE_LLM_CHAIN = """
app:
  mode: workflow
  name: llm-chain
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: llm_1
        data:
          type: llm
          title: 第一跳
          model: {provider: anthropic, name: claude-opus-4-8, completion_params: {max_tokens: 256}}
          prompt_template: [{role: user, text: "{{#start_1.q#}}"}]
      - id: llm_2
        data:
          type: llm
          title: 第二跳
          model: {provider: anthropic, name: claude-opus-4-8, completion_params: {max_tokens: 256}}
          prompt_template: [{role: user, text: "{{#llm_1.text#}}"}]
      - id: llm_3
        data:
          type: llm
          title: 第三跳
          model: {provider: anthropic, name: claude-opus-4-8, completion_params: {max_tokens: 256}}
          prompt_template: [{role: user, text: "{{#llm_2.text#}}"}]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [llm_3, text]}]}
    edges:
      - {source: start_1, target: llm_1, sourceHandle: source}
      - {source: llm_1, target: llm_2, sourceHandle: source}
      - {source: llm_2, target: llm_3, sourceHandle: source}
      - {source: llm_3, target: end_1, sourceHandle: source}
"""


def test_bottle_001_fires_on_three_llm_chain() -> None:
    out = _analyze(_THREE_LLM_CHAIN)
    hits = _by_rule(out, "BOTTLE_001")
    assert hits, "BOTTLE_001 应在 3-LLM 串行链触发"
    assert hits[0].node_ids == ("llm_1", "llm_2", "llm_3")


def test_bottle_001_quiet_on_seq(fixture_text: Callable[[str], str]) -> None:
    """seq fixture 仅 1 个 llm → 不触发。"""
    assert "BOTTLE_001" not in _ids(_analyze(fixture_text("seq")))


# ---------------------------------------------------------------------------
# BOTTLE_002：迭代体内含 http-request。
# ---------------------------------------------------------------------------

_ITER_HTTP_BODY = """
app:
  mode: workflow
  name: iter-http
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: items, type: array}]}
      - id: iter_1
        data:
          type: iteration
          title: 逐项请求
          iterator_selector: [start_1, items]
          output_selector: [iter_http, body]
          is_parallel: false
          start_node_id: iter_http
      - id: iter_http
        data: {type: http-request, title: 拉取, iteration_id: iter_1}
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [iter_1, output]}]}
    edges:
      - {source: start_1, target: iter_1, sourceHandle: source}
      - {source: iter_1, target: end_1, sourceHandle: source}
"""


def test_bottle_002_fires_on_iteration_http_body() -> None:
    out = _analyze(_ITER_HTTP_BODY)
    hits = _by_rule(out, "BOTTLE_002")
    assert hits, "BOTTLE_002 应在迭代体内 http-request 触发"
    assert hits[0].node_ids == ("iter_1",)


def test_bottle_002_quiet_on_iteration_fixture(
    fixture_text: Callable[[str], str],
) -> None:
    """iteration fixture 体内是 llm 非 http-request → 不触发。"""
    assert "BOTTLE_002" not in _ids(_analyze(fixture_text("iteration")))


# ---------------------------------------------------------------------------
# CACHE_001：两个 knowledge-retrieval 共享 dataset_ids。
# ---------------------------------------------------------------------------

_DUP_RETRIEVAL = """
app:
  mode: workflow
  name: dup-retrieval
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: kr_1
        data: {type: knowledge-retrieval, title: 检索A, dataset_ids: [ds_1]}
      - id: kr_2
        data: {type: knowledge-retrieval, title: 检索B, dataset_ids: [ds_1]}
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [kr_2, result]}]}
    edges:
      - {source: start_1, target: kr_1, sourceHandle: source}
      - {source: kr_1, target: kr_2, sourceHandle: source}
      - {source: kr_2, target: end_1, sourceHandle: source}
"""


def test_cache_001_fires_on_shared_dataset() -> None:
    out = _analyze(_DUP_RETRIEVAL)
    hits = _by_rule(out, "CACHE_001")
    assert hits, "CACHE_001 应在两个检索节点共享 dataset 时触发"
    assert set(hits[0].node_ids) == {"kr_1", "kr_2"}


_DISTINCT_RETRIEVAL = """
app:
  mode: workflow
  name: distinct-retrieval
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: kr_1
        data: {type: knowledge-retrieval, title: 检索A, dataset_ids: [ds_1]}
      - id: kr_2
        data: {type: knowledge-retrieval, title: 检索B, dataset_ids: [ds_2]}
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [kr_2, result]}]}
    edges:
      - {source: start_1, target: kr_1, sourceHandle: source}
      - {source: kr_1, target: kr_2, sourceHandle: source}
      - {source: kr_2, target: end_1, sourceHandle: source}
"""


def test_cache_001_quiet_on_distinct_datasets() -> None:
    """不同 dataset → 不触发。"""
    assert "CACHE_001" not in _ids(_analyze(_DISTINCT_RETRIEVAL))


# ---------------------------------------------------------------------------
# RESOURCE_001：parallel_nums > env.max_parallel_workers。
# ---------------------------------------------------------------------------


def test_resource_001_quiet_on_default_env(
    fixture_text: Callable[[str], str],
) -> None:
    """iteration fixture parallel_nums=5，默认上限 10 → 不触发。"""
    assert "RESOURCE_001" not in _ids(_analyze(fixture_text("iteration")))


def test_resource_001_fires_with_injected_small_cap(
    fixture_text: Callable[[str], str],
) -> None:
    """注入 max_parallel_workers=3，5>3 → 触发（证明读注入 env 非真实环境）。"""
    out = _analyze(fixture_text("iteration"), env=OptimizeEnv(max_parallel_workers=3))
    hits = _by_rule(out, "RESOURCE_001")
    assert hits, "RESOURCE_001 应在注入小上限时触发"
    assert hits[0].node_ids == ("iter_1",)
    assert "3" in hits[0].detail and "5" in hits[0].detail


# ---------------------------------------------------------------------------
# RESOURCE_002：is_parallel=True 但 parallel_nums<=1。
# ---------------------------------------------------------------------------

_PARALLEL_BUT_ONE = """
app:
  mode: workflow
  name: misconfigured-parallel
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: items, type: array}]}
      - id: iter_1
        data:
          type: iteration
          title: 名并实串
          iterator_selector: [start_1, items]
          output_selector: [iter_inner, text]
          is_parallel: true
          parallel_nums: 1
          start_node_id: iter_inner
      - id: iter_inner
        data:
          type: llm
          title: 内层
          iteration_id: iter_1
          model: {provider: anthropic, name: claude-opus-4-8, completion_params: {max_tokens: 256}}
          prompt_template: [{role: user, text: "处理 {{#iter_1.item#}}"}]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [iter_1, output]}]}
    edges:
      - {source: start_1, target: iter_1, sourceHandle: source}
      - {source: iter_1, target: end_1, sourceHandle: source}
"""


def test_resource_002_fires_on_parallel_one() -> None:
    out = _analyze(_PARALLEL_BUT_ONE)
    hits = _by_rule(out, "RESOURCE_002")
    assert hits, "RESOURCE_002 应在 is_parallel=true 且 parallel_nums<=1 触发"
    assert hits[0].node_ids == ("iter_1",)


# ---------------------------------------------------------------------------
# LLM_001：缺失 max_tokens / 过大 max_tokens（含迭代体内层）。
# ---------------------------------------------------------------------------

_MISSING_MAX_TOKENS = """
app:
  mode: workflow
  name: missing-max-tokens
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: llm_1
        data:
          type: llm
          title: 无上限
          model: {provider: anthropic, name: claude-opus-4-8}
          prompt_template: [{role: user, text: "{{#start_1.q#}}"}]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [llm_1, text]}]}
    edges:
      - {source: start_1, target: llm_1, sourceHandle: source}
      - {source: llm_1, target: end_1, sourceHandle: source}
"""


def test_llm_001_fires_on_missing_max_tokens() -> None:
    out = _analyze(_MISSING_MAX_TOKENS)
    hits = _by_rule(out, "LLM_001")
    assert hits, "LLM_001 应在缺失 max_tokens 时触发"
    assert hits[0].node_ids == ("llm_1",)
    assert "max_tokens" in hits[0].title


def test_llm_001_fires_on_oversized_max_tokens(
    fixture_text: Callable[[str], str],
) -> None:
    """seq fixture llm_1 max_tokens=1024；注入 max_llm_tokens=512 → 偏大触发。"""
    out = _analyze(fixture_text("seq"), env=OptimizeEnv(max_llm_tokens=512))
    hits = _by_rule(out, "LLM_001")
    assert hits, "LLM_001 应在注入小 token 上限时对 1024 触发"
    assert hits[0].node_ids == ("llm_1",)
    assert "偏大" in hits[0].title


def test_llm_001_quiet_when_max_tokens_reasonable(
    fixture_text: Callable[[str], str],
) -> None:
    """seq fixture 默认上限 8192，1024 既不缺也不大 → 不触发。"""
    assert "LLM_001" not in _ids(_analyze(fixture_text("seq")))


def test_llm_001_inspects_iteration_body_llm() -> None:
    """迭代体内层 llm 也参与体检：注入小上限使内层 256 之上的值触发。"""
    out = _analyze(_SERIAL_ITER_LLM, env=OptimizeEnv(max_llm_tokens=100))
    hits = _by_rule(out, "LLM_001")
    assert hits, "LLM_001 应递归命中迭代体内层 llm"
    assert hits[0].node_ids == ("iter_inner",)


# ---------------------------------------------------------------------------
# analyzer：排序（HIGH 在前）、去重、env 注入语义。
# ---------------------------------------------------------------------------


def test_analyze_sorts_by_severity(fixture_text: Callable[[str], str]) -> None:
    """parallel fixture 同时有 HIGH(PARALLEL_001) 等：列表按严重度升序（HIGH 在前）。"""
    out = _analyze(fixture_text("parallel"))
    keys = [s.sort_key()[0] for s in out]
    assert keys == sorted(keys), "建议应按严重度确定性升序"
    assert out[0].severity is Severity.HIGH


def test_analyze_dedups_identical() -> None:
    """同一 (rule_id, node_ids) 仅出现一次（去重）。"""
    out = _analyze(_DUP_RETRIEVAL)
    keys = [(s.rule_id, s.node_ids) for s in out]
    assert len(keys) == len(set(keys))


def test_env_injection_changes_behavior(
    fixture_text: Callable[[str], str],
) -> None:
    """同一 IR + 不同注入 env → 结果不同：证明上限来自参数而非真实环境。"""
    ir = lower_to_ir(parse_dify_yaml(fixture_text("iteration")))
    default = {s.rule_id for s in analyze_ir(ir)}
    tightened = {
        s.rule_id
        for s in analyze_ir(ir, env=OptimizeEnv(max_parallel_workers=3))
    }
    assert "RESOURCE_001" not in default
    assert "RESOURCE_001" in tightened
