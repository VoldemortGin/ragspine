"""emitter：WorkflowIR → GeneratedCode（拓扑展平 + 控制流 + import 收集 + 组装脚本）。纯 stdlib。

产出一段无框架命令式纯 Python：顶层 `run_workflow(inputs, *, provider=None) -> dict`，LLM 节点走
`provider.chat(messages)`（默认 `ragspine.MockProvider()`），并行用 ThreadPoolExecutor（P4），
不支持节点用带 NotImplementedError 的骨架钩子。生成模块自带一小段固定 prelude（_var/_as_num）。

控制流（P3）：if-else 节点把其各分支【独占可达】的下游节点包进 if/elif/else 块；分支汇合处的
节点（多分支可达，如 answer）在 if/else 之后无条件执行。线性/分支/并行（P4）皆由拓扑序驱动。
"""

from __future__ import annotations

from dataclasses import dataclass

from ragspine.dify.codegen import nodes as node_emit
from ragspine.dify.codegen.fold import FoldPlan, detect_answer_question_folds
from ragspine.dify.codegen.naming import NameTable
from ragspine.dify.ir.model import (
    IfElseNode,
    IRNode,
    IterationNode,
    KnowledgeRetrievalNode,
    ParameterExtractorNode,
    StartNode,
    ToolNode,
    UnsupportedNode,
    VarRef,
    WorkflowIR,
)

INDENT = "    "


@dataclass(frozen=True)
class GeneratedCode:
    """生成结果（frozen）：可 exec 的源码 + 入口名 + 收集到的 import + 警告列表。"""

    source: str
    entrypoint: str = "run_workflow"
    imports: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def generate_code(
    ir: WorkflowIR,
    *,
    provider_expr: str = "MockProvider()",
    fold_answer_question: bool = True,
    emit_node_traces: bool = False,
) -> GeneratedCode:
    """WorkflowIR → GeneratedCode。provider_expr 为 run_workflow 内 provider 默认值表达式。

    fold_answer_question（默认 True）：识别「问答骨架」（start→knowledge-retrieval→llm(context
    指向该检索)→answer/end）并整体折叠成一次 ragspine.answer_question（自带反幻觉/provenance，
    比手拼 retrieve+chat 更短更对）。置 False 关闭折叠，逐节点生成。

    emit_node_traces（默认 False）：在生成代码里注入节点级执行 trace——模块级 _NODE_TRACES
    列表逐节点记 {node_id/title/node_type/status/elapsed_ms/inputs/outputs/error}，返回前把
    未执行节点 sweep 成 skipped。计时钟 _TRACE_CLOCK 由 runner 注入（独立运行时计时为 0.0）。
    默认关时生成源码与不带本参数完全相同（字节级）。
    """
    builder = _Emitter(
        ir, provider_expr=provider_expr, fold_answer_question=fold_answer_question,
        emit_node_traces=emit_node_traces,
    )
    return builder.build()


class _Emitter:
    """一次编译的可变状态（名表 / 警告 / 钩子函数 / import / 折叠计划）。"""

    def __init__(
        self,
        ir: WorkflowIR,
        *,
        provider_expr: str,
        fold_answer_question: bool = True,
        emit_node_traces: bool = False,
    ) -> None:
        self.ir = ir
        self.provider_expr = provider_expr
        self.emit_node_traces = emit_node_traces
        self.names = NameTable(n.id for n in ir.graph.nodes)
        self.warnings: list[str] = []
        self.imports: set[str] = set()
        plans = detect_answer_question_folds(ir) if fold_answer_question else ()
        # llm_id -> 折叠计划（在该 llm 节点处发折叠调用）；kr_id 集合在折叠中被吞并、跳过逐节点生成。
        self.folds_by_llm: dict[str, FoldPlan] = {p.llm_id: p for p in plans}
        self.folded_kr: set[str] = {p.kr_id for p in plans}

    # -- 组装 ---------------------------------------------------------------

    def build(self) -> GeneratedCode:
        # 先展平 body（这一步会把按需 import 如 ThreadPoolExecutor 收进 self.imports）。
        body = self._emit_body()
        hooks = self._emit_hooks()
        constants = self._emit_module_constants()
        trace_block = self._emit_trace_module_block()
        inputs_cls = self._emit_inputs_class()
        prelude = self._emit_prelude()
        source = "\n".join(
            [*prelude, "", *constants, *trace_block, *inputs_cls, "", *hooks,
             *self._emit_run_workflow(body)]
        )
        source = source.rstrip("\n") + "\n"
        return GeneratedCode(
            source=source,
            entrypoint="run_workflow",
            imports=tuple(sorted(self.imports)),
            warnings=tuple(self.warnings),
        )

    def _emit_module_constants(self) -> list[str]:
        """按需的可改模块常量：knowledge-retrieval / 问答折叠用到的库路径（默认离线空库）。"""
        has_kr = any(
            isinstance(n, KnowledgeRetrievalNode) for n in self.ir.graph.nodes
        )
        lines: list[str] = []
        if has_kr or self.folds_by_llm:
            lines += [
                "# 检索 chunk 库路径：默认 ':memory:'（离线空库可跑，无文件副作用）；",
                "# 指向你用 ragspine 灌好 chunk 的 sqlite 文件即接通真实检索。",
                'KNOWLEDGE_CHUNK_DB = ":memory:"',
                "",
            ]
        if self.folds_by_llm:
            lines += [
                "# answer_question 结构化通路的 FactStore 库路径：默认 ':memory:'（空库，反幻觉照常生效）；",
                "# 指向你用 ragspine 灌好数值事实的 sqlite 即接通结构化问答。",
                'ANSWER_QUESTION_FACT_DB = ":memory:"',
                "",
            ]
        return lines

    def _emit_prelude(self) -> list[str]:
        """模块头：docstring + import（含按需收集的 import）+ 运行期 helper。"""
        extra = sorted(self.imports)
        extra_block = [*extra, ""] if extra else []
        return [*_PRELUDE_HEAD, *extra_block, *_PRELUDE_HELPERS]

    # -- 节点级 execution trace（emit_node_traces，默认关） -------------------

    def _emit_trace_module_block(self) -> list[str]:
        """trace 的模块级设施：记录列表 / 可注入计时钟 / 全节点表 / _trace_now。flag 关 → []。

        _TRACE_NODES 收外层 topo_order 全部节点（含被折叠的 kr/llm 与被 if-else gate 的节点；
        iteration 子图内部节点本就不在外层 topo_order，不含），供 sweep 判定 skipped。
        """
        if not self.emit_node_traces:
            return []
        lines = [
            "# 节点级执行 trace：runner 注入 _TRACE_CLOCK（time.perf_counter）并读取 _NODE_TRACES；",
            "# 独立运行（不经 runner）时 _TRACE_CLOCK 为 None → 计时一律 0.0。",
            "_NODE_TRACES: list = []",
            "_TRACE_CLOCK: Any = None",
            "_TRACE_NODES = (",
        ]
        for nid in self.ir.topo_order:
            node = self.ir.node(nid)
            lines.append(f"    ({nid!r}, {node.title!r}, {node.kind!r}),")
        lines += [
            ")",
            "",
            "def _trace_now() -> float:",
            "    return _TRACE_CLOCK() if _TRACE_CLOCK is not None else 0.0",
            "",
        ]
        return lines

    def _trace_inputs_expr(self, node: IRNode) -> str:
        """节点输入快照表达式：dep_refs 去重 → {'src.field': _var(...)} dict；无依赖 → None。"""
        refs: list[VarRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in node.dep_refs():
            key = (ref.node_id, ref.field)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
        if not refs:
            return "None"
        items = ", ".join(
            f"{r.node_id + '.' + r.field!r}: _var({r.node_id!r}, {r.field!r})"
            for r in refs
        )
        return "{" + items + "}"

    def _emit_traced(self, node: IRNode) -> list[str]:
        """把单节点 body 包成 trace 单元（try/except + _trace_done）。flag 关 / body 空则原样。

        折叠单元（folds_by_llm 命中）发两条记录：先 kr 后 llm，共用同一 _trace_t0。
        body 无可执行语句（纯注释）时不 try 包裹（空 try 块非法），直接顺发成功记录。
        """
        body = self._emit_one(node)
        if not self.emit_node_traces or not body:
            return body
        plan = self.folds_by_llm.get(node.id)
        # (node_id, title, kind, inputs 变量名) —— 折叠单元两条，其余一条。
        records: list[tuple[str, str, str, str]]
        lines = ["_trace_t0 = _trace_now()"]
        if plan is not None:
            kr = self.ir.node(plan.kr_id)
            lines.append(f"_trace_in_kr = {self._trace_inputs_expr(kr)}")
            lines.append(f"_trace_in = {self._trace_inputs_expr(node)}")
            records = [
                (kr.id, kr.title, kr.kind, "_trace_in_kr"),
                (node.id, node.title, node.kind, "_trace_in"),
            ]
        else:
            lines.append(f"_trace_in = {self._trace_inputs_expr(node)}")
            records = [(node.id, node.title, node.kind, "_trace_in")]
        has_stmt = any(
            ln.strip() and not ln.strip().startswith("#") for ln in body
        )
        if not has_stmt:
            lines.extend(body)
            for nid, title, kind, in_var in records:
                lines.append(
                    f"_trace_done({nid!r}, {title!r}, {kind!r}, _trace_t0, {in_var})"
                )
            return lines
        lines.append("try:")
        lines.extend(f"{INDENT}{ln}" if ln else "" for ln in body)
        lines.append("except BaseException as _trace_exc:")
        for nid, title, kind, in_var in records:
            lines.append(
                f"{INDENT}_trace_done({nid!r}, {title!r}, {kind!r}, "
                f"_trace_t0, {in_var}, _trace_exc)"
            )
        lines.append(f"{INDENT}raise")
        for nid, title, kind, in_var in records:
            lines.append(
                f"_trace_done({nid!r}, {title!r}, {kind!r}, _trace_t0, {in_var})"
            )
        return lines

    def _emit_ifelse_trace(self, node: IfElseNode) -> list[str]:
        """if-else 节点自身的独立 trace 记录（分支判定本身近零耗时，elapsed≈0）。"""
        return [
            f"_trace_in = {self._trace_inputs_expr(node)}",
            f"_trace_done({node.id!r}, {node.title!r}, {node.kind!r}, "
            "_trace_now(), _trace_in)",
        ]

    def _emit_inputs_class(self) -> list[str]:
        """从 start 节点变量生成 Inputs dataclass（全可选、默认 None）。"""
        start = next(
            (n for n in self.ir.graph.nodes if isinstance(n, StartNode)), None
        )
        variables = start.variables if start else ()
        lines = ["@dataclass", "class Inputs:", '    """工作流输入（对应 Dify start 节点变量）。"""']
        if not variables:
            lines.append("    pass")
        for var in variables:
            lines.append(f"    {var}: Any = None")
        return lines

    def _emit_run_workflow(self, body: list[str]) -> list[str]:
        head = [
            "def run_workflow(",
            "    inputs: Inputs,",
            "    *,",
            "    provider: LLMProvider | None = None,",
            ") -> dict[str, Any]:",
            '    """编译自 Dify 工作流：拓扑展平、命令式执行、provider.chat 走 LLM 缝。"""',
            f"    provider = provider if provider is not None else {self.provider_expr}",
            "    _ctx: dict[tuple[str, str], Any] = {}",
            "    _result: dict[str, Any] = {}",
        ]
        if self.emit_node_traces:
            head.append("    _NODE_TRACES.clear()")
        head += [
            "",
            "    def _var(node: str, field: str) -> Any:",
            '        """取某节点输出字段（闭合 _ctx）；缺失返回空串，不因取值缺失整体崩。"""',
            "        return _ctx.get((node, field), '')",
            "",
        ]
        if self.emit_node_traces:
            head += [
                "    def _trace_done(node_id: str, title: str, node_type: str,",
                "                    t0: float, inputs: Any, exc: Any = None) -> None:",
                '        """记一条节点执行 trace：输出快照扫 _ctx；exc 非 None 即 failed。"""',
                "        _outs = {f: v for (n, f), v in _ctx.items() if n == node_id} or None",
                "        _NODE_TRACES.append({",
                "            'node_id': node_id, 'title': title, 'node_type': node_type,",
                "            'status': 'failed' if exc is not None else 'succeeded',",
                "            'elapsed_ms': (_trace_now() - t0) * 1000.0,",
                "            'inputs': inputs, 'outputs': None if exc is not None else _outs,",
                "            'error': f'{type(exc).__name__}: {exc}' if exc is not None else None,",
                "        })",
                "",
            ]
        indented = [f"{INDENT}{ln}" if ln else "" for ln in body]
        tail: list[str] = []
        if self.emit_node_traces:
            tail += [
                f"{INDENT}# sweep：外层节点若无记录（未走到的分支等）→ skipped（排在最后）。",
                f"{INDENT}_traced = {{t['node_id'] for t in _NODE_TRACES}}",
                f"{INDENT}for _t_nid, _t_title, _t_kind in _TRACE_NODES:",
                f"{INDENT}    if _t_nid not in _traced:",
                f"{INDENT}        _NODE_TRACES.append({{",
                f"{INDENT}            'node_id': _t_nid, 'title': _t_title, 'node_type': _t_kind,",
                f"{INDENT}            'status': 'skipped', 'elapsed_ms': 0.0,",
                f"{INDENT}            'inputs': None, 'outputs': None, 'error': None,",
                f"{INDENT}        }})",
            ]
        tail.append(f"{INDENT}return _result")
        return [*head, *indented, *tail]

    # -- 控制流展平 ---------------------------------------------------------

    def _emit_body(self) -> list[str]:
        """按拓扑序展平节点；同层≥2 独立重节点→ThreadPoolExecutor；if-else 独占下游入分支块。"""
        gated = self._branch_gating()  # node_id -> (ifelse_id, handle)
        # node_id -> 它所在 parallel_layer 内、同样未被 gate 的兄弟（用于识别并行组）。
        parallel_groups = self._parallel_groups(gated)
        emitted: set[str] = set()
        lines: list[str] = []
        for node_id in self.ir.topo_order:
            if node_id in emitted:
                continue
            if node_id in self.folded_kr:
                # 已折叠进某 llm 的 knowledge-retrieval：折叠调用在 llm 处统一产出，这里整体跳过。
                continue
            if node_id in gated:
                # 该节点属于某 if-else 分支，由 if-else 的展开统一产出，跳过这里的独立产出。
                continue
            group = parallel_groups.get(node_id)
            if group is not None and len(group) >= 2:
                lines.extend(self._emit_parallel_group(group))
                emitted.update(group)
                continue
            node = self.ir.node(node_id)
            if isinstance(node, IfElseNode):
                if self.emit_node_traces:
                    lines.extend(self._emit_ifelse_trace(node))
                lines.extend(self._emit_ifelse_region(node, gated, emitted))
            else:
                lines.extend(self._emit_traced(node))
            emitted.add(node_id)
        return lines

    def _parallel_groups(
        self, gated: dict[str, tuple[str, str]]
    ) -> dict[str, list[str]]:
        """每个可并行节点 → 它所属的并行组（同层、未 gate、≥2 重节点）。键覆盖组内每个成员。

        资格：同一 parallel_layer 内、均未被 if-else gate、且组中【重节点】（llm/code/iteration/
        unsupported）数 ≥2。轻节点（template-transform 等）若与重节点同层同样并发执行，搭便车入组。
        """
        out: dict[str, list[str]] = {}
        for layer in self.ir.parallel_layers:
            # 折叠掉的 knowledge-retrieval 不参与并行分组（其产出由 llm 处的折叠调用统一负责）。
            members = [
                nid for nid in layer
                if nid not in gated and nid not in self.folded_kr
            ]
            if len(members) < 2:
                continue
            heavy = [m for m in members if self._is_heavy(m)]
            if len(heavy) < 2:
                continue
            group = sorted(members)  # 确定性顺序
            for m in group:
                out[m] = group
        return out

    def _is_heavy(self, node_id: str) -> bool:
        """重节点：有外部调用/可观测副作用、并行能省墙钟时间（llm/code/iteration/unsupported）。"""
        return self.ir.node(node_id).kind in {"llm", "code", "iteration", "unsupported"}

    def _emit_parallel_group(self, group: list[str]) -> list[str]:
        """把一组独立节点包成各自的 _task_* 闭包，经 ThreadPoolExecutor 并发执行后汇合 _ctx。

        每个 task 直接写 _ctx（各节点写不相交的键，GIL 下安全）；executor 仅用来并发墙钟时间。
        家族全同步：用线程池，绝不生成 async。
        """
        self.imports.add("from concurrent.futures import ThreadPoolExecutor")
        lines = [f"# parallel layer: {', '.join(group)}（同层独立，线程并发）"]
        task_names: list[str] = []
        for nid in group:
            var = self.names.var(nid)
            task = f"_task_{var}"
            task_names.append(task)
            lines.append(f"def {task}() -> None:")
            body = self._emit_traced(self.ir.node(nid))
            lines.extend(f"{INDENT}{ln}" if ln else "" for ln in body)
        lines.append(f"with ThreadPoolExecutor(max_workers={len(group)}) as _ex:")
        lines.append(f"    _futs = [_ex.submit(_t) for _t in ({', '.join(task_names)},)]")
        lines.append("    for _f in _futs:")
        lines.append("        _f.result()")
        return lines

    def _emit_one(self, node: IRNode) -> list[str]:
        """单节点产出（含不支持节点 warning 记录；iteration 走子图递归展开；问答骨架折叠）。"""
        if node.id in self.folded_kr:
            # 该 knowledge-retrieval 已被折叠进某 llm 节点，逐节点产出在此跳过。
            return []
        plan = self.folds_by_llm.get(node.id)
        if plan is not None:
            self.imports.add(
                "from ragspine.retrieval.link.narrative_link import build_narrative_retriever"
            )
            self.imports.add("from ragspine import SqliteFactStore, answer_question")
            return node_emit.emit_answer_question_fold(plan, self.names)
        if isinstance(node, IterationNode):
            return self._emit_iteration(node)
        if isinstance(node, KnowledgeRetrievalNode):
            self.imports.add(
                "from ragspine.retrieval.link.narrative_link import build_narrative_retriever"
            )
        if isinstance(node, ParameterExtractorNode):
            self.imports.add("import json")
        if isinstance(node, ToolNode):
            self.imports.add("from spineagent import function_tool")
            self.warnings.append(
                f"节点 {node.id}（tool {node.tool_name}）：已生成 spineagent @function_tool "
                f"占位 _tool_{self.names.var(node.id)}（带 NotImplementedError），请补全工具逻辑。"
            )
        if isinstance(node, UnsupportedNode):
            self.warnings.append(
                f"节点 {node.id}（类型 {node.node_type}）未支持：已生成带 "
                f"NotImplementedError 的骨架钩子 _hook_{self.names.var(node.id)}，请补全。"
            )
        return node_emit.emit_node(node, self.names)

    def _emit_iteration(self, node: IterationNode) -> list[str]:
        """iteration → 内层子图函数 _iter_body_*(item) + 逐项执行（串行 for / 并行线程池）。

        子图体在一个嵌套函数里跑：种子 _ctx=dict(外层 _ctx) 再注入 (iter_id, 'item')=item，
        故子图既能读外层节点输出、又能读当前 item。output 取 node.output 指向的子图字段。
        is_parallel → ThreadPoolExecutor(max_workers=parallel_nums)；否则串行列表推导。
        """
        var = self.names.var(node.id)
        body_fn = f"_iter_body_{var}"
        lines = [f"# iteration: {node.id}"]
        lines.append(f"def {body_fn}(_item: Any) -> Any:")
        # 子图局部上下文：拷贝外层 _ctx，再注入当前 item（键 (iter_id,'item')）。
        lines.append("    _ctx = dict(_ctx_outer)")
        lines.append(f"    _ctx[({node.id!r}, 'item')] = _item")
        lines.append("")
        lines.append("    def _var(node: str, field: str) -> Any:")
        lines.append("        return _ctx.get((node, field), '')")
        lines.append("")
        # 子图节点按拓扑序展平（子图通常简单线性；复用同一节点 emitter）。
        if node.body is not None:
            sub_lines = self._emit_subgraph(node.body)
            lines.extend(f"    {ln}" if ln else "" for ln in sub_lines)
        # 返回 output 取值（output ref 指向子图某节点字段）。
        if node.output is not None:
            ret = node_emit.value_expr(node.output)
        else:
            ret = "_item"
        lines.append(f"    return {ret}")
        # 逐项执行：先把外层 _ctx 暴露给子图函数（重命名避免 def 内 _ctx=dict(_ctx) 自指）。
        lines.append("_ctx_outer = _ctx")
        lines.append(
            f"_iter_items_{var} = list({node_emit.value_expr(node.iterator)} or [])"
        )
        if node.is_parallel and node.parallel_nums > 1:
            self.imports.add("from concurrent.futures import ThreadPoolExecutor")
            workers = max(1, node.parallel_nums)
            lines.append(
                f"with ThreadPoolExecutor(max_workers={workers}) as _ex_{var}:"
            )
            lines.append(
                f"    _ctx[({node.id!r}, 'output')] = "
                f"list(_ex_{var}.map({body_fn}, _iter_items_{var}))"
            )
        else:
            lines.append(
                f"_ctx[({node.id!r}, 'output')] = "
                f"[{body_fn}(_it) for _it in _iter_items_{var}]"
            )
        return lines

    def _emit_subgraph(self, sub: WorkflowIR) -> list[str]:
        """展平一个 iteration 子图（按拓扑序逐节点；子图内不再递归并行/分支，保持简单）。"""
        out: list[str] = []
        for nid in sub.topo_order:
            out.extend(node_emit.emit_node(sub.node(nid), self.names))
        return out

    def _emit_ifelse_region(
        self,
        node: IfElseNode,
        gated: dict[str, tuple[str, str]],
        emitted: set[str],
    ) -> list[str]:
        """把一个 if-else 节点展开成 if/elif/else 块，内含各分支独占下游节点。"""
        lines = [f"# if-else: {node.id}"]
        # handle -> 该分支独占可达的节点（按拓扑序）。
        members: dict[str, list[str]] = {}
        for nid, (owner, handle) in gated.items():
            if owner == node.id:
                members.setdefault(handle, []).append(nid)
        for handle in members:
            members[handle] = [n for n in self.ir.topo_order if n in members[handle]]

        first = True
        for branch in node.branches:
            cond = branch.condition_expr if branch.condition_expr else "True"
            keyword = "if" if first else "elif"
            lines.append(f"{keyword} {cond}:")
            block = self._emit_branch_block(members.get(branch.handle, []), emitted)
            lines.extend(block or [f"{INDENT}pass"])
            first = False
        # else 分支（handle 'false' 或未在 branches 中声明的兜底）。
        else_members = [
            nid for h, ms in members.items()
            for nid in ms
            if h not in {b.handle for b in node.branches}
        ]
        else_members = [n for n in self.ir.topo_order if n in else_members]
        if else_members:
            lines.append("else:")
            lines.extend(self._emit_branch_block(else_members, emitted))
        elif first:
            # 没有任何分支条件（异常情况）：占位，避免空 if。
            lines.append("if True:")
            lines.append(f"{INDENT}pass")
        return lines

    def _emit_branch_block(self, member_ids: list[str], emitted: set[str]) -> list[str]:
        """产出一个分支块内的节点（缩进一级）。"""
        block: list[str] = []
        for nid in member_ids:
            if nid in emitted:
                continue
            block.extend(
                f"{INDENT}{ln}" if ln else ""
                for ln in self._emit_traced(self.ir.node(nid))
            )
            emitted.add(nid)
        return block

    def _branch_gating(self) -> dict[str, tuple[str, str]]:
        """计算每个被 if-else 分支独占的节点 → (ifelse_id, handle)。

        独占判据：从该 if-else 的某 handle 出边出发、沿控制流可达，且【不】能从该 if-else 的其它
        handle、也不绕过该 if-else 到达——即所有入路径都经由这一个 handle。汇合节点（多 handle 可达）
        不计入，留到 if/else 之后无条件执行。
        """
        gating: dict[str, tuple[str, str]] = {}
        node_map = self.ir.graph.node_map()
        for node in self.ir.graph.nodes:
            if not isinstance(node, IfElseNode):
                continue
            # handle -> 直接后继集合。
            handle_succ: dict[str, set[str]] = {}
            for edge in self.ir.graph.successors(node.id):
                handle = edge.source_handle or "true"
                handle_succ.setdefault(handle, set()).add(edge.target)
            # 各 handle 的可达闭包（不跨越其它 if-else 边界过度扩张：遇汇合即由下方多归属剔除）。
            handle_reach: dict[str, set[str]] = {
                h: self._reach(seeds, node_map) for h, seeds in handle_succ.items()
            }
            # 某节点若只被一个 handle 的闭包覆盖 → 独占该分支；被多个覆盖 → 汇合点，不 gate。
            counts: dict[str, list[str]] = {}
            for handle, reach in handle_reach.items():
                for nid in reach:
                    counts.setdefault(nid, []).append(handle)
            for nid, handles in counts.items():
                if len(handles) == 1 and nid != node.id:
                    gating[nid] = (node.id, handles[0])
        return gating

    def _reach(self, seeds: set[str], node_map: dict[str, IRNode]) -> set[str]:
        """从 seeds 沿控制流出边的可达集合（含 seeds 自身），遇到下一个 if-else 即停在其上。"""
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            # 不穿越下一个 if-else（其下游归它自己管），避免分支闭包相互污染。
            if isinstance(node_map.get(cur), IfElseNode):
                continue
            for edge in self.ir.graph.successors(cur):
                stack.append(edge.target)
        return seen

    # -- 钩子函数 -----------------------------------------------------------

    def _emit_hooks(self) -> list[str]:
        """模块级钩子/工具函数：UnsupportedNode 的 _hook_*，与 ToolNode 的 spineagent @function_tool。"""
        lines: list[str] = []
        for node in self.ir.graph.nodes:
            if isinstance(node, UnsupportedNode):
                lines.extend(node_emit.emit_hook_function(node, self.names))
                lines.append("")
            elif isinstance(node, ToolNode):
                lines.extend(node_emit.emit_tool_function(node, self.names))
                lines.append("")
        return lines


# 固定 prelude 头：模块 docstring + 固定 import（按需 import 由 _emit_prelude 紧随其后插入）。
_PRELUDE_HEAD: list[str] = [
    '"""由 ragspine.dify 从 Dify 工作流 YAML 自动生成的纯 Python 脚本。',
    "",
    "无框架、命令式、可离线运行：LLM 节点走 corespine.LLMProvider.chat（默认",
    "ragspine.MockProvider()），并行用 concurrent.futures.ThreadPoolExecutor（全同步，无 async）。",
    '编辑提示：本文件可直接 exec / 入库；不支持的节点处留有 _hook_* 骨架待补全。"""',
    "from __future__ import annotations",
    "",
    "from dataclasses import dataclass",
    "from typing import Any",
    "",
    "from corespine import LLMProvider",
    "from ragspine import MockProvider",
    "",
]

# 运行期 helper（_var 闭包在 run_workflow 内单独注入；_as_num 数值容错放模块级）。
_PRELUDE_HELPERS: list[str] = [
    "",
    "def _as_num(value: Any) -> float:",
    '    """把任意值容错转 float（条件比较用；非数返回 0.0）。"""',
    "    try:",
    "        return float(value)",
    "    except (TypeError, ValueError):",
    "        return 0.0",
]
