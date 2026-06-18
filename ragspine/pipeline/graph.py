"""管线拓扑的图值类型（Node / Edge / PipelineGraph）与三种导出器。

设计为叶子级、零依赖的纯值层：本模块【不】import 任何 agent / retrieval / service
编排器，所有 Node.symbol 都只是 dotted-path 字符串（仅由漂移守护测试用 importlib 解析），
从而保证零循环 import、图类型保持单一关注点（screaming-architecture）。

不变量：
- Node / Edge / PipelineGraph 均为 frozen dataclass（值语义、可哈希、可比较）。
- 导出确定：节点/边一律按【声明顺序】发射，故 to_mermaid / to_dot / to_dict 的输出
  在同一张图上逐字节稳定，重生成的 diff 干净可审。
"""

from dataclasses import dataclass

# 节点形状仅区分少数 kind；其余统一走矩形。映射保持小而确定。
_MERMAID_OPEN = "["
_MERMAID_CLOSE = "]"
_MERMAID_SHAPE_BY_KIND: dict[str, tuple[str, str]] = {
    "gate": ("{", "}"),  # 判定/门 —— 菱形
    "store": ("[(", ")]"),  # 存储 —— 柱体
    "channel": ("([", "])"),  # 通道 —— 体育场形
}


@dataclass(frozen=True)
class Node:
    """拓扑中的一个节点。

    kind 取值约定："stage" | "store" | "external" | "gate" | "channel"。
    domain 用于分组/子图（如 "retrieval" / "agent"）。
    symbol 为其所代表代码的 dotted path（漂移守护用；概念节点为 None）。
    """

    id: str
    label: str
    kind: str
    domain: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class Edge:
    """拓扑中的一条有向边。

    label 形如 "route=structured" / "hit" / "miss"（无标签为 None）。
    kind 取值约定："flow" | "conditional" | "data"。
    """

    src: str
    dst: str
    label: str | None = None
    kind: str = "flow"


def _mermaid_escape(text: str) -> str:
    """转义 Mermaid 节点 label 中会破坏语法的字符。

    Mermaid 不支持反斜杠转义引号；约定用 HTML 实体表达双引号，并把会被解析为
    形状/连线的字符也实体化，确保 label 文本不泄漏破坏 flowchart 语法。
    """
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
        .replace("|", "&#124;")
    )


def _dot_escape(text: str) -> str:
    """转义 DOT 双引号字符串中的反斜杠与双引号。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class PipelineGraph:
    """一张静态管线拓扑：标题 + 有序节点 + 有序边。"""

    title: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    def to_mermaid(self, *, direction: str = "TD") -> str:
        """发射 Mermaid `flowchart <DIR>`（节点按 kind 取形状，边按声明顺序）。"""
        lines: list[str] = [f"flowchart {direction}"]
        for node in self.nodes:
            open_br, close_br = _MERMAID_SHAPE_BY_KIND.get(
                node.kind, (_MERMAID_OPEN, _MERMAID_CLOSE)
            )
            lines.append(f'    {node.id}{open_br}"{_mermaid_escape(node.label)}"{close_br}')
        for edge in self.edges:
            if edge.label is None:
                lines.append(f"    {edge.src} --> {edge.dst}")
            else:
                lines.append(f"    {edge.src} -->|{_mermaid_escape(edge.label)}| {edge.dst}")
        return "\n".join(lines) + "\n"

    def to_dot(self) -> str:
        """发射 Graphviz `digraph { ... }`（节点/边按声明顺序）。"""
        lines: list[str] = [f'digraph "{_dot_escape(self.title)}" {{', "    rankdir=TB;"]
        for node in self.nodes:
            lines.append(f'    "{_dot_escape(node.id)}" [label="{_dot_escape(node.label)}"];')
        for edge in self.edges:
            src = _dot_escape(edge.src)
            dst = _dot_escape(edge.dst)
            if edge.label is None:
                lines.append(f'    "{src}" -> "{dst}";')
            else:
                lines.append(f'    "{src}" -> "{dst}" [label="{_dot_escape(edge.label)}"];')
        lines.append("}")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, object]:
        """返回 JSON 可序列化的 {title, nodes:[...], edges:[...]}（可经 json 往返）。"""
        nodes: list[dict[str, object]] = [
            {
                "id": n.id,
                "label": n.label,
                "kind": n.kind,
                "domain": n.domain,
                "symbol": n.symbol,
            }
            for n in self.nodes
        ]
        edges: list[dict[str, object]] = [
            {"src": e.src, "dst": e.dst, "label": e.label, "kind": e.kind} for e in self.edges
        ]
        return {"title": self.title, "nodes": nodes, "edges": edges}

    def merge(self, other: "PipelineGraph", *, group: str | None = None) -> "PipelineGraph":
        """并入 other：节点按 id 去重（first wins），边全保留；标题取 self。

        发射顺序：self 的节点/边在前，other 新增的在后（确定）。给定 group 时，
        仅给 other 真正【新加入】的节点打上 domain=group 分组标签。
        """
        seen: set[str] = {n.id for n in self.nodes}
        merged_nodes: list[Node] = list(self.nodes)
        for node in other.nodes:
            if node.id in seen:
                continue
            seen.add(node.id)
            if group is not None:
                node = Node(
                    id=node.id,
                    label=node.label,
                    kind=node.kind,
                    domain=group,
                    symbol=node.symbol,
                )
            merged_nodes.append(node)
        merged_edges: tuple[Edge, ...] = self.edges + other.edges
        return PipelineGraph(
            title=self.title,
            nodes=tuple(merged_nodes),
            edges=merged_edges,
        )
