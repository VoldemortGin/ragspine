"""P9 扩展节点验收：variable-aggregator / assigner / document-extractor / loop / http-request。

每类节点覆盖编译层（lower → IR 形状、codegen 源码）+ 执行层（生成代码真跑、断言输出）。
全部离线：LLM 走 MockProvider，http-request 用假客户端注入 _HTTP_CLIENT 槽位，零真实网络。
"""

from __future__ import annotations

from typing import Any

import pytest

from ragspine.dify.codegen.emitter import GeneratedCode, generate_code
from ragspine.dify.ir.lower import lower_to_ir
from ragspine.dify.ir.model import (
    HttpRequestNode,
    Literal,
    LoopNode,
    UnsupportedNode,
    VariableAggregatorNode,
    VariableAssignerNode,
    VarRef,
    WorkflowIR,
)
from ragspine.dify.parse.loader import parse_dify_yaml


def _ir(dsl: str) -> WorkflowIR:
    return lower_to_ir(parse_dify_yaml(dsl))


def _gen(dsl: str) -> GeneratedCode:
    return generate_code(_ir(dsl))


def _exec(code: GeneratedCode) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(compile(code.source, "<dify-p9>", "exec"), ns)  # noqa: S102
    return ns


def _run(dsl: str, **inputs: Any) -> dict[str, Any]:
    ns = _exec(_gen(dsl))
    result = ns["run_workflow"](ns["Inputs"](**inputs))
    assert isinstance(result, dict)
    return result


# ---------------------------------------------------------------------------
# variable-aggregator：first-non-null（未产出跳过、None 跳过、空串有效）
# ---------------------------------------------------------------------------

_AGG_BASIC = """
app:
  mode: workflow
  name: agg-demo
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: x, type: text-input}]}
      - id: code_none
        data:
          type: code
          title: 产出None
          code: "def main():\\n    return {'out': None}\\n"
          code_language: python3
          variables: []
          outputs: {out: {type: string}}
      - id: tt_empty
        data: {type: template-transform, title: 空串, template: '', variables: []}
      - id: agg_1
        data:
          type: variable-aggregator
          title: 聚合
          variables:
            - [ghost_branch, output]
            - [code_none, out]
            - [tt_empty, output]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [agg_1, output]}]}
    edges:
      - {source: start_1, target: code_none, sourceHandle: source}
      - {source: code_none, target: tt_empty, sourceHandle: source}
      - {source: tt_empty, target: agg_1, sourceHandle: source}
      - {source: agg_1, target: end_1, sourceHandle: source}
"""


def test_aggregator_lowered_shape() -> None:
    node = _ir(_AGG_BASIC).node("agg_1")
    assert isinstance(node, VariableAggregatorNode)
    assert node.kind == "variable-aggregator"
    # 候选按声明序保留（first-non-null 的判定顺序）。
    assert node.items == (
        VarRef("ghost_branch", "output"),
        VarRef("code_none", "out"),
        VarRef("tt_empty", "output"),
    )
    assert node.groups == ()


def test_aggregator_skips_missing_and_none_but_keeps_empty_string() -> None:
    # ghost_branch 未产出（_ctx 无键）跳过；code_none 产出 None 跳过；
    # tt_empty 的空串是有效产出（非真值判定）→ 聚合结果为 ''。
    out = _run(_AGG_BASIC, x="ignored")
    assert out["out"] == ""


_AGG_BRANCH = """
app:
  mode: workflow
  name: agg-branch
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: score, type: number}]}
      - id: ifelse_1
        data:
          type: if-else
          title: 阈值
          cases:
            - case_id: "true"
              logical_operator: and
              conditions:
                - {variable_selector: [start_1, score], comparison_operator: ">", value: "60"}
      - id: tt_yes
        data: {type: template-transform, title: 高分, template: high, variables: []}
      - id: tt_no
        data: {type: template-transform, title: 低分, template: low, variables: []}
      - id: agg_1
        data:
          type: variable-aggregator
          title: 汇合
          variables:
            - [tt_yes, output]
            - [tt_no, output]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [agg_1, output]}]}
    edges:
      - {source: start_1, target: ifelse_1, sourceHandle: source}
      - {source: ifelse_1, target: tt_yes, sourceHandle: "true"}
      - {source: ifelse_1, target: tt_no, sourceHandle: "false"}
      - {source: tt_yes, target: agg_1, sourceHandle: source}
      - {source: tt_no, target: agg_1, sourceHandle: source}
      - {source: agg_1, target: end_1, sourceHandle: source}
"""


@pytest.mark.parametrize(("score", "expected"), [(90, "high"), (30, "low")])
def test_aggregator_picks_the_executed_branch(score: int, expected: str) -> None:
    # 真实 if-else 分流：未走到的分支不写 _ctx，聚合自然取已执行分支的产出。
    assert _run(_AGG_BRANCH, score=score)["out"] == expected


_AGG_GROUPS = """
app:
  mode: workflow
  name: agg-groups
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: x, type: text-input}]}
      - id: tt_a
        data: {type: template-transform, title: A, template: 'A:{{ x }}', variables: [{variable: x, value_selector: [start_1, x]}]}
      - id: tt_b
        data: {type: template-transform, title: B, template: 'B', variables: []}
      - id: agg_1
        data:
          type: variable-aggregator
          title: 分组聚合
          variables: []
          advanced_settings:
            group_enabled: true
            groups:
              - group_name: g1
                variables:
                  - [ghost, output]
                  - [tt_a, output]
              - group_name: g2
                variables:
                  - [tt_b, output]
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - {variable: a, value_selector: [agg_1, g1, output]}
            - {variable: b, value_selector: [agg_1, g2, output]}
    edges:
      - {source: start_1, target: tt_a, sourceHandle: source}
      - {source: tt_a, target: tt_b, sourceHandle: source}
      - {source: tt_b, target: agg_1, sourceHandle: source}
      - {source: agg_1, target: end_1, sourceHandle: source}
"""


def test_aggregator_group_mode_outputs_per_group() -> None:
    node = _ir(_AGG_GROUPS).node("agg_1")
    assert isinstance(node, VariableAggregatorNode)
    assert [g.name for g in node.groups] == ["g1", "g2"]
    # 每组独立聚合，输出字段 '<组名>.output'。
    out = _run(_AGG_GROUPS, x="hi")
    assert out["a"] == "A:hi"
    assert out["b"] == "B"


# ---------------------------------------------------------------------------
# variable-assigner（assigner v2 items）：conversation 变量池
# ---------------------------------------------------------------------------

_ASSIGN_CONV = """
app:
  mode: workflow
  name: assign-demo
workflow:
  conversation_variables:
    - {name: counter, value: 0, value_type: number}
    - {name: log, value: [], value_type: 'array[string]'}
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: x, type: text-input}]}
      - id: assign_1
        data:
          type: assigner
          title: 写变量
          version: "2"
          items:
            - {variable_selector: [conversation, counter], operation: "+=", input_type: constant, value: 5}
            - {variable_selector: [conversation, log], operation: append, input_type: variable, value: [start_1, x]}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - {variable: counter, value_selector: [conversation, counter]}
            - {variable: log, value_selector: [conversation, log]}
    edges:
      - {source: start_1, target: assign_1, sourceHandle: source}
      - {source: assign_1, target: end_1, sourceHandle: source}
"""


def test_assigner_lowered_shape_and_conversation_defaults() -> None:
    ir = _ir(_ASSIGN_CONV)
    assert ir.conversation_defaults == (("counter", 0), ("log", []))
    node = ir.node("assign_1")
    assert isinstance(node, VariableAssignerNode)
    assert node.kind == "assigner"
    first, second = node.items
    assert first.target == VarRef("conversation", "counter")
    assert first.operation == "+="
    assert first.value == Literal(5)
    assert second.target == VarRef("conversation", "log")
    assert second.operation == "append"
    assert second.value == VarRef("start_1", "x")


def test_assigner_seeds_pool_and_writes_visible_downstream() -> None:
    code = _gen(_ASSIGN_CONV)
    # run_workflow 开头按声明序种 conversation 默认值。
    assert "_ctx[('conversation', 'counter')] = 0" in code.source
    assert "_ctx[('conversation', 'log')] = []" in code.source
    out = _run(_ASSIGN_CONV, x="hello")
    assert out["counter"] == 5.0  # 0 += 5（算术运算统一数值化）
    assert out["log"] == ["hello"]


def test_assigner_v1_shape_is_normalized() -> None:
    dsl = """
app:
  mode: workflow
  name: assign-v1
workflow:
  conversation_variables:
    - {name: memo, value: '', value_type: string}
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: x, type: text-input}]}
      - id: assign_1
        data:
          type: assigner
          title: 旧形状
          assigned_variable_selector: [conversation, memo]
          write_mode: over-write
          input_variable_selector: [start_1, x]
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: memo, value_selector: [conversation, memo]}]}
    edges:
      - {source: start_1, target: assign_1, sourceHandle: source}
      - {source: assign_1, target: end_1, sourceHandle: source}
"""
    node = _ir(dsl).node("assign_1")
    assert isinstance(node, VariableAssignerNode)
    assert len(node.items) == 1  # v1 旧形状归一成等价的单条 v2 item
    item = node.items[0]
    assert item.target == VarRef("conversation", "memo")
    assert item.operation == "over-write"
    assert item.value == VarRef("start_1", "x")
    assert _run(dsl, x="备忘")["memo"] == "备忘"


def test_assigner_unknown_operation_falls_to_unsupported() -> None:
    dsl = """
app:
  mode: workflow
  name: assign-bad
workflow:
  graph:
    nodes:
      - id: assign_1
        data:
          type: assigner
          title: 未知操作
          items:
            - {variable_selector: [conversation, x], operation: shuffle, input_type: constant, value: 1}
    edges: []
"""
    node = _ir(dsl).node("assign_1")
    assert isinstance(node, UnsupportedNode)
    assert node.node_type == "assigner"


# ---------------------------------------------------------------------------
# document-extractor：str/list → text 纯计算（沙箱零文件 I/O）
# ---------------------------------------------------------------------------

_DOC_DSL = """
app:
  mode: workflow
  name: doc-demo
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: x, type: file}]}
      - id: doc_1
        data: {type: document-extractor, title: 抽文本, variable_selector: [start_1, x]}
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: text, value_selector: [doc_1, text]}]}
    edges:
      - {source: start_1, target: doc_1, sourceHandle: source}
      - {source: doc_1, target: end_1, sourceHandle: source}
"""


def test_document_extractor_str_to_text() -> None:
    node = _ir(_DOC_DSL).node("doc_1")
    assert node.kind == "document-extractor"
    assert _run(_DOC_DSL, x="纯文本内容")["text"] == "纯文本内容"


def test_document_extractor_list_to_array_of_text() -> None:
    out = _run(_DOC_DSL, x=["a", {"text": "b"}, 3])
    assert out["text"] == ["a", "b", "3"]


def test_document_extractor_never_touches_files() -> None:
    # 纯计算 codegen：源码无 open / pathlib / os（沙箱受限 builtins 下也可跑）。
    src = _gen(_DOC_DSL).source
    assert "open(" not in src
    assert "import os" not in src
    assert "_doc_text" in src


# ---------------------------------------------------------------------------
# loop：有界 for + break 条件 + loop_vars 跨轮累积
# ---------------------------------------------------------------------------


def _loop_dsl(loop_count: int, *, with_break: bool) -> str:
    break_block = (
        """
          break_conditions:
            - {variable_selector: [loop_1, n], comparison_operator: ">=", value: "3"}
          logical_operator: and
"""
        if with_break
        else ""
    )
    return f"""
app:
  mode: workflow
  name: loop-demo
workflow:
  graph:
    nodes:
      - id: start_1
        data: {{type: start, title: 开始, variables: [{{variable: x, type: text-input}}]}}
      - id: loop_1
        data:
          type: loop
          title: 循环
          loop_count: {loop_count}
          start_node_id: loop_start_1
          loop_variables:
            - {{label: n, value_type: constant, value: 0, var_type: number}}
            - {{label: trace, value_type: constant, value: [], var_type: 'array[string]'}}
{break_block}
      - id: loop_start_1
        data: {{type: loop-start, title: '', loop_id: loop_1}}
      - id: assign_in_loop
        data:
          type: assigner
          title: 累加
          loop_id: loop_1
          items:
            - {{variable_selector: [loop_1, n], operation: "+=", input_type: constant, value: 1}}
            - {{variable_selector: [loop_1, trace], operation: append, input_type: variable, value: [start_1, x]}}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - {{variable: n, value_selector: [loop_1, n]}}
            - {{variable: trace, value_selector: [loop_1, trace]}}
    edges:
      - {{source: start_1, target: loop_1, sourceHandle: source}}
      - {{source: loop_start_1, target: assign_in_loop, sourceHandle: source}}
      - {{source: loop_1, target: end_1, sourceHandle: source}}
"""


def test_loop_lowered_shape() -> None:
    ir = _ir(_loop_dsl(5, with_break=True))
    node = ir.node("loop_1")
    assert isinstance(node, LoopNode)
    assert node.loop_count == 5
    assert node.break_expr is not None and "loop_1" in node.break_expr
    assert node.break_refs == (VarRef("loop_1", "n"),)
    assert [name for name, _ in node.loop_vars] == ["n", "trace"]
    # loop-start 是画布结构节点：不进主图也不进子图。
    assert node.body is not None
    assert {n.id for n in node.body.graph.nodes} == {"assign_in_loop"}
    assert "loop_start_1" not in {n.id for n in ir.graph.nodes}


def test_loop_codegen_is_bounded_for_without_while() -> None:
    src = _gen(_loop_dsl(5, with_break=True)).source
    assert "for _loop_round_" in src
    assert "range(5)" in src
    assert "while" not in src  # 无 while 死循环面
    assert "break" in src


def test_loop_runs_full_count_and_accumulates_across_rounds() -> None:
    out = _run(_loop_dsl(5, with_break=False), x="r")
    assert out["n"] == 5.0  # 跑满 5 轮，写入跨轮累积
    assert out["trace"] == ["r"] * 5
    # 循环终值保留在 (loop_1, ·) 键，供下游（end）直接引用——上两行已隐式验证。


def test_loop_break_condition_exits_early() -> None:
    # 每轮体执行【完后】判定 n>=3：第 3 轮结束后 break（不是先判后跑）。
    out = _run(_loop_dsl(100, with_break=True), x="r")
    assert out["n"] == 3.0
    assert out["trace"] == ["r"] * 3


def test_loop_count_clamped_to_100() -> None:
    node = _ir(_loop_dsl(10000, with_break=False)).node("loop_1")
    assert isinstance(node, LoopNode)
    assert node.loop_count == 100  # 编译期护栏：钳制 [0, 100]


# ---------------------------------------------------------------------------
# http-request：受控客户端槽位（生成代码零网络 import）
# ---------------------------------------------------------------------------

_HTTP_DSL = """
app:
  mode: workflow
  name: http-demo
workflow:
  graph:
    nodes:
      - id: start_1
        data: {type: start, title: 开始, variables: [{variable: q, type: text-input}]}
      - id: http_1
        data:
          type: http-request
          title: 调接口
          method: POST
          url: "http://api.example.com/search?lang=zh"
          headers: "X-App: demo"
          params: "q: {{#start_1.q#}}"
          authorization:
            type: api-key
            config: {type: bearer, api_key: sk-test}
          body:
            type: json
            data: '{"query": "{{#start_1.q#}}"}'
          timeout: {max_read_timeout: 60}
      - id: end_1
        data:
          type: end
          title: 结束
          outputs:
            - {variable: status, value_selector: [http_1, status_code]}
            - {variable: body, value_selector: [http_1, body]}
    edges:
      - {source: start_1, target: http_1, sourceHandle: source}
      - {source: http_1, target: end_1, sourceHandle: source}
"""


def test_http_request_lowered_shape() -> None:
    node = _ir(_HTTP_DSL).node("http_1")
    assert isinstance(node, HttpRequestNode)
    assert node.method == "post"
    assert node.body_type == "json"
    assert node.timeout_s == 60.0
    headers_text = "".join(p for p in node.headers.parts if isinstance(p, str))
    assert "X-App: demo" in headers_text
    assert "Authorization: Bearer sk-test" in headers_text  # auth 并入 headers
    assert VarRef("start_1", "q") in node.params.refs()


def test_http_request_sets_requires_http_flag() -> None:
    assert _gen(_HTTP_DSL).requires_http is True
    assert _gen(_DOC_DSL).requires_http is False


def test_http_request_inside_iteration_sets_requires_http() -> None:
    dsl = """
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
          title: 逐项
          iterator_selector: [start_1, items]
          output_selector: [http_in, body]
      - id: http_in
        data: {type: http-request, title: 拉取, method: get, url: 'http://e.com', iteration_id: iter_1}
      - id: end_1
        data: {type: end, title: 结束, outputs: [{variable: out, value_selector: [iter_1, output]}]}
    edges:
      - {source: start_1, target: iter_1, sourceHandle: source}
      - {source: iter_1, target: end_1, sourceHandle: source}
"""
    assert _gen(dsl).requires_http is True  # 子图内层节点同样计入


def test_http_request_codegen_never_imports_network_modules() -> None:
    code = _gen(_HTTP_DSL)
    assert code.warnings == ()  # 真实建模，不再是骨架钩子
    src = code.source
    for banned in ("urllib", "socket", "http.client", "requests", "httpx"):
        assert f"import {banned}" not in src
        assert f"from {banned}" not in src
    # 唯一出网面：模块级槽位 + _dify_http 调用（受信 runner 注入）。
    assert "_HTTP_CLIENT: Any = None" in src
    assert "_dify_http(" in src


def test_http_request_executes_via_injected_client() -> None:
    ns = _exec(_gen(_HTTP_DSL))
    seen: list[dict[str, Any]] = []

    def fake_client(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(request)
        return {"status_code": 200, "body": "pong", "headers": {}}

    ns["_HTTP_CLIENT"] = fake_client
    out = ns["run_workflow"](ns["Inputs"](q="北京"))
    assert out == {"status": 200, "body": "pong"}
    req = seen[0]
    assert req["method"] == "post"
    assert req["url"] == "http://api.example.com/search?lang=zh"
    assert req["params"] == "q: 北京"  # 模板已内插
    assert req["body_type"] == "json"
    assert "北京" in req["body"]
    assert req["timeout_s"] == 60.0


def test_http_request_without_injection_raises_clear_error() -> None:
    ns = _exec(_gen(_HTTP_DSL))  # 不注入 _HTTP_CLIENT
    with pytest.raises(RuntimeError) as exc:
        ns["run_workflow"](ns["Inputs"](q="x"))
    assert "RAGSPINE_DIFY_HTTP_ENABLED" in str(exc.value)


def test_http_request_form_data_body_falls_to_unsupported() -> None:
    dsl = """
app:
  mode: workflow
  name: http-form
workflow:
  graph:
    nodes:
      - id: http_1
        data:
          type: http-request
          title: 传文件
          method: post
          url: "http://e.com/upload"
          body:
            type: form-data
            data: [{type: file, key: f, value: ''}]
    edges: []
"""
    node = _ir(dsl).node("http_1")
    assert isinstance(node, UnsupportedNode)  # 多部分/文件上传不猜语义，留钩子
    assert node.node_type == "http-request"
