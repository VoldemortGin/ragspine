"""元数据过滤（批次 2.2 ①，manual 先行）：检索管线里【打分之前】的确定性条件过滤。

设计取向（家族公共宪章：离线优先、确定性、刻意地薄）：
    - 零三方依赖、纯函数、确定性；只用最小算子集（=、!=、in、not in、范围）。
    - 只【收窄】候选——apply 恒返回输入的【子序列】（保序），绝不新增、绝不改动任何块。因此
      RESTRICTED 语义绝不被过滤器绕过：过滤发生在打分之前，link/rerank 双出口照常剔除 RESTRICTED，
      即便某过滤器刻意选中 RESTRICTED，也只是把它留在候选里、随后被出口剔除，绝无泄漏。
    - 比较口径统一为【字符串】：字段值与条件值都归一为 str（None -> ''），范围/大小按字典序
      （lexicographic）——对 ISO 式期间/日期（'2024' < '2025'、'2025H1' < '2025H2'）语义正确且跨平台
      确定；调用方若需数值序，传零填充字符串即可。缺失字段的块一律【不命中】（安全收窄）。

算子（最小集）：
    eq      字段 == 值
    ne      字段 != 值
    in      字段 ∈ 值集合（值为可迭代）
    nin     字段 ∉ 值集合
    gt/gte  字段 > / >= 值（字典序）
    lt/lte  字段 < / <= 值（字典序）
    between 值 = (low, high)，low <= 字段 <= high（闭区间，字典序）
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# 缺失字段哨兵：与 None（字段存在但值为 None）区分——缺字段一律不命中。
_MISSING = object()

# 需要标量字符串值的算子。
_SCALAR_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
# 需要集合值的算子。
_COLLECTION_OPS = frozenset({"in", "nin"})
# 需要 (low, high) 值的算子。
_RANGE_OPS = frozenset({"between"})

VALID_OPS = _SCALAR_OPS | _COLLECTION_OPS | _RANGE_OPS


def _to_str(value: Any) -> str:
    """字段/条件值归一为 str（None -> ''），统一比较口径。"""
    return "" if value is None else str(value)


@dataclass(frozen=True)
class FilterCondition:
    """一条元数据过滤条件：字段名 + 算子 + 值。

    值的形态随算子而定：标量算子取标量、集合算子取可迭代、between 取 (low, high)。
    构造期校验算子合法且值形态匹配（畸形条件立刻 ValueError，绝不静默放行）。
    """

    field: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.op not in VALID_OPS:
            raise ValueError(f"未知过滤算子 {self.op!r}；可用：{' / '.join(sorted(VALID_OPS))}")
        if self.op in _COLLECTION_OPS:
            if isinstance(self.value, (str, bytes)) or not isinstance(self.value, Iterable):
                raise ValueError(f"算子 {self.op!r} 的值须为可迭代集合，得到 {self.value!r}")
        if self.op in _RANGE_OPS:
            if not (
                isinstance(self.value, Sequence)
                and not isinstance(self.value, (str, bytes))
                and len(self.value) == 2
            ):
                raise ValueError(
                    f"算子 {self.op!r} 的值须为 (low, high) 二元组，得到 {self.value!r}"
                )

    def matches(self, obj: Any) -> bool:
        """obj 是否满足本条件（缺字段 -> 不命中；比较统一走字符串口径）。"""
        raw = getattr(obj, self.field, _MISSING)
        if raw is _MISSING:
            return False
        fv = _to_str(raw)
        if self.op == "eq":
            return fv == _to_str(self.value)
        if self.op == "ne":
            return fv != _to_str(self.value)
        if self.op == "in":
            return fv in {_to_str(v) for v in self.value}
        if self.op == "nin":
            return fv not in {_to_str(v) for v in self.value}
        if self.op == "gt":
            return fv > _to_str(self.value)
        if self.op == "gte":
            return fv >= _to_str(self.value)
        if self.op == "lt":
            return fv < _to_str(self.value)
        if self.op == "lte":
            return fv <= _to_str(self.value)
        # between（闭区间）
        low, high = self.value
        return _to_str(low) <= fv <= _to_str(high)


@dataclass(frozen=True)
class MetadataFilter:
    """一组元数据过滤条件的合取 / 析取（默认合取 AND）。

    matches(obj)：combine=='and' 时所有条件命中才算命中；'or' 时任一命中即算。空条件集恒命中
    （等价于不过滤）。apply(objs)：返回命中的子序列（保序、只收窄，绝不新增/改动）。
    """

    conditions: tuple[FilterCondition, ...] = ()
    combine: str = "and"

    def __post_init__(self) -> None:
        if self.combine not in ("and", "or"):
            raise ValueError(f"combine 须为 'and' 或 'or'，得到 {self.combine!r}")
        # 允许传 list：归一为 tuple（frozen dataclass 用 object.__setattr__）。
        if not isinstance(self.conditions, tuple):
            object.__setattr__(self, "conditions", tuple(self.conditions))

    def matches(self, obj: Any) -> bool:
        if not self.conditions:
            return True
        if self.combine == "and":
            return all(c.matches(obj) for c in self.conditions)
        return any(c.matches(obj) for c in self.conditions)

    def apply(self, objs: Iterable[Any]) -> list[Any]:
        """确定性收窄：返回命中的子序列（保序）。只收窄，绝不新增/改动任何元素。"""
        return [o for o in objs if self.matches(o)]


def make_filter(
    conditions: Iterable[tuple[str, str, Any]] | None = None,
    *,
    combine: str = "and",
) -> MetadataFilter:
    """便捷构造：从 (field, op, value) 三元组序列建 MetadataFilter。conditions 为空 -> 空过滤（恒命中）。"""
    conds = tuple(FilterCondition(f, op, v) for f, op, v in (conditions or ()))
    return MetadataFilter(conditions=conds, combine=combine)
