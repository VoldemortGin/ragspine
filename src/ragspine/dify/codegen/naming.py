"""node_id → 稳定确定性的 Python 变量名。纯 stdlib。

Dify node id 形如 'llm_1' / '1710000000000'（毫秒时间戳）/ '语言模型'，未必是合法 Python
标识符。本模块把它归一成合法、可读、确定性、去重后的变量名，供 codegen 命名中间结果。

确定性：同一 id 集合恒定产出同一映射（不依赖插入顺序之外的随机性），保证离线可复现快照。
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Iterable

_NON_IDENT = re.compile(r"\W")
_LEADING_DIGIT = re.compile(r"^\d")


def _sanitize(node_id: str) -> str:
    """把单个 node_id 归一成合法 Python 标识符片段（非法字符→_，数字开头加前缀）。"""
    name = _NON_IDENT.sub("_", node_id).strip("_")
    if not name:
        name = "node"
    if _LEADING_DIGIT.match(name):
        name = f"n_{name}"
    if keyword.iskeyword(name) or keyword.issoftkeyword(name):
        name = f"{name}_"
    return name.lower()


class NameTable:
    """node_id → 唯一合法变量名的确定性映射（冲突时加数字后缀去重）。"""

    def __init__(self, node_ids: Iterable[str]) -> None:
        self._map: dict[str, str] = {}
        used: set[str] = set()
        # 按 id 排序保证确定性（去重后缀只取决于 id 集合，不取决于遍历顺序）。
        for node_id in sorted(set(node_ids)):
            base = _sanitize(node_id)
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            self._map[node_id] = candidate

    def var(self, node_id: str) -> str:
        """取 node_id 对应的变量名（未登记的临时 id 即时归一，不入表）。"""
        if node_id in self._map:
            return self._map[node_id]
        return _sanitize(node_id)
