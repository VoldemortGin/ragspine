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
from ragspine.dify.codegen.naming import NameTable
from ragspine.dify.ir.model import (
    IfElseNode,
    IRNode,
    StartNode,
    UnsupportedNode,
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
    ir: WorkflowIR, *, provider_expr: str = "MockProvider()"
) -> GeneratedCode:
    """WorkflowIR → GeneratedCode。provider_expr 为 run_workflow 内 provider 默认值表达式。"""
    builder = _Emitter(ir, provider_expr=provider_expr)
    return builder.build()


class _Emitter:
    """一次编译的可变状态（名表 / 警告 / 钩子函数 / import）。"""

    def __init__(self, ir: WorkflowIR, *, provider_expr: str) -> None:
        self.ir = ir
        self.provider_expr = provider_expr
        self.names = NameTable(n.id for n in ir.graph.nodes)
        self.warnings: list[str] = []
        self.imports: set[str] = set()

    # -- 组装 ---------------------------------------------------------------

    def build(self) -> GeneratedCode:
        body = self._emit_body()
        hooks = self._emit_hooks()
        inputs_cls = self._emit_inputs_class()
        source = "\n".join(
            [*_PRELUDE_LINES, "", *inputs_cls, "", *hooks, *self._emit_run_workflow(body)]
        )
        source = source.rstrip("\n") + "\n"
        return GeneratedCode(
            source=source,
            entrypoint="run_workflow",
            imports=tuple(sorted(self.imports)),
            warnings=tuple(self.warnings),
        )

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
            "",
            "    def _var(node: str, field: str) -> Any:",
            '        """取某节点输出字段（闭合 _ctx）；缺失返回空串，不因取值缺失整体崩。"""',
            "        return _ctx.get((node, field), '')",
            "",
        ]
        indented = [f"{INDENT}{ln}" if ln else "" for ln in body]
        tail = [f"{INDENT}return _result"]
        return [*head, *indented, *tail]

    # -- 控制流展平 ---------------------------------------------------------

    def _emit_body(self) -> list[str]:
        """按拓扑序展平节点；if-else 节点把独占下游包进分支块。返回（基础缩进 0 的）相对行。"""
        gated = self._branch_gating()  # node_id -> (ifelse_id, handle)
        emitted: set[str] = set()
        lines: list[str] = []
        for node_id in self.ir.topo_order:
            if node_id in emitted:
                continue
            if node_id in gated:
                # 该节点属于某 if-else 分支，由 if-else 的展开统一产出，跳过这里的独立产出。
                continue
            node = self.ir.node(node_id)
            if isinstance(node, IfElseNode):
                lines.extend(self._emit_ifelse_region(node, gated, emitted))
            else:
                lines.extend(self._emit_one(node))
            emitted.add(node_id)
        return lines

    def _emit_one(self, node: IRNode) -> list[str]:
        """单节点产出（含不支持节点 warning 记录）。"""
        if isinstance(node, UnsupportedNode):
            self.warnings.append(
                f"节点 {node.id}（类型 {node.node_type}）未支持：已生成带 "
                f"NotImplementedError 的骨架钩子 _hook_{self.names.var(node.id)}，请补全。"
            )
        return node_emit.emit_node(node, self.names)

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
            block.extend(f"{INDENT}{ln}" if ln else "" for ln in self._emit_one(self.ir.node(nid)))
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
        lines: list[str] = []
        for node in self.ir.graph.nodes:
            if isinstance(node, UnsupportedNode):
                lines.extend(node_emit.emit_hook_function(node, self.names))
                lines.append("")
        return lines


# 固定 prelude：import + 运行期 helper（_var 取上下文值、_as_num 数值容错）。
_PRELUDE_LINES: list[str] = [
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
    "",
    "def _as_num(value: Any) -> float:",
    '    """把任意值容错转 float（条件比较用；非数返回 0.0）。"""',
    "    try:",
    "        return float(value)",
    "    except (TypeError, ValueError):",
    "        return 0.0",
]
