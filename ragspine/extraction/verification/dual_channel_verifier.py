"""双通道交叉校验（二期 PDF 线）—— 纯逻辑契约，不依赖 Docling。

两种独立方法抽同一张表（如 Docling 表格解析 vs 文本层重建），对结果做维度键对齐的
交叉校验：维度键相同且值在容差内 -> 自动放行（agreed）；值冲突 -> 进复核队列；
单通道独有 -> 也进队列（优先级更低）。让人工只处理真正可疑的少数
（PRD user story 15）。

队列约定（与 ragspine.ingestion.review.review_queue.ReviewQueue.enqueue 对齐）：
    - 值冲突      -> reason='dual_channel_conflict'，payload 含双方值与 locator。
    - 单通道独有  -> reason='single_channel_only'，优先级低于 conflict。

实现已完成，dataclass 字段契约保持冻结，行为契约见 tests/extraction/verification/test_dual_channel_verifier.py。
"""

from dataclasses import dataclass

# 入队原因（与 review_queue 约定一致）。
REASON_CONFLICT = "dual_channel_conflict"
REASON_SINGLE_ONLY = "single_channel_only"

# 入队优先级：数值越小越先审。冲突项优先于单通道独有项。
PRIORITY_CONFLICT = 10
PRIORITY_SINGLE_ONLY = 50


@dataclass
class ChannelFact:
    """单通道抽出的一条候选事实（维度键与 fact_store.Fact 对齐）。

    字段语义约定：
        metric_code / entity / period_type / period / channel:
                        构成维度键，跨两通道做对齐的依据（同键才比对值）。
        value:          数值。
        source_locator: 原文精确定位（如 'page1_table1!R2C3'），随入队载荷回链。
        channel_name:   产出该事实的通道名（如 'docling' / 'text_layer'），用于
                        区分双方来源，写入冲突 / 独有项的载荷。
    """

    metric_code: str
    entity: str
    period_type: str
    period: str
    channel: str
    value: float
    source_locator: str
    channel_name: str

    def dim_key(self) -> tuple:
        """返回用于跨通道对齐的维度键（不含 value / locator / channel_name）。"""
        return (
            self.metric_code,
            self.entity,
            self.period_type,
            self.period,
            self.channel,
        )


@dataclass
class VerificationResult:
    """一次双通道校验的汇总结果。

    字段语义约定：
        agreed:       双方维度键相同且值在容差内的事实（自动放行），list[ChannelFact]
                      （取通道 A 一侧代表即可）。
        conflicts:    值冲突明细 list[dict]，每项含维度键、双方值与各自 locator /
                      channel_name，以及（若入队）队列项 id。
        only_in_a:    仅出现在通道 A 的事实 list[ChannelFact]。
        only_in_b:    仅出现在通道 B 的事实 list[ChannelFact]。
        n_auto_passed: 自动放行条数（== len(agreed)）。
        n_enqueued:    实际入队的条数（冲突 + 单通道独有，仅在给了 queue 时 > 0）。
    """

    agreed: list = None
    conflicts: list = None
    only_in_a: list = None
    only_in_b: list = None
    n_auto_passed: int = 0
    n_enqueued: int = 0

    def __post_init__(self) -> None:
        if self.agreed is None:
            self.agreed = []
        if self.conflicts is None:
            self.conflicts = []
        if self.only_in_a is None:
            self.only_in_a = []
        if self.only_in_b is None:
            self.only_in_b = []


def verify(
    facts_a: list[ChannelFact],
    facts_b: list[ChannelFact],
    queue=None,
    tolerance: float = 0.0,
) -> VerificationResult:
    """对两通道事实做维度键对齐的交叉校验。

    规则：
        - 维度键相同且 |value_a - value_b| <= tolerance -> agreed（自动放行）。
        - 维度键相同但值超出容差 -> conflicts；若给了 queue，按
          reason='dual_channel_conflict'、priority=PRIORITY_CONFLICT 入队，
          payload 含双方值与各自 locator / channel_name。
        - 维度键仅在一侧 -> only_in_a / only_in_b；若给了 queue，按
          reason='single_channel_only'、priority=PRIORITY_SINGLE_ONLY 入队
          （优先级低于 conflict）。
        - queue 为 None 时只分类不入队（n_enqueued=0）。

    返回 VerificationResult（n_auto_passed=len(agreed)、n_enqueued=实际入队数）。
    """
    by_key_a: dict[tuple, ChannelFact] = {f.dim_key(): f for f in facts_a}
    by_key_b: dict[tuple, ChannelFact] = {f.dim_key(): f for f in facts_b}

    result = VerificationResult()
    n_enqueued = 0

    # 两侧共有的维度键：在容差内放行，否则记冲突（按需入队）。
    for key in by_key_a:
        if key not in by_key_b:
            continue
        fa = by_key_a[key]
        fb = by_key_b[key]
        if abs(fa.value - fb.value) <= tolerance:
            result.agreed.append(fa)
            continue

        detail = {
            "dim_key": list(key),
            "channel_a": fa.channel_name,
            "value_a": fa.value,
            "locator_a": fa.source_locator,
            "channel_b": fb.channel_name,
            "value_b": fb.value,
            "locator_b": fb.source_locator,
        }
        if queue is not None:
            item_id = queue.enqueue(
                reason=REASON_CONFLICT,
                payload=dict(detail),
                locator=fa.source_locator,
                priority=PRIORITY_CONFLICT,
            )
            detail["queue_item_id"] = item_id
            n_enqueued += 1
        result.conflicts.append(detail)

    # 仅 A 侧独有。
    for key, fa in by_key_a.items():
        if key in by_key_b:
            continue
        result.only_in_a.append(fa)
        if queue is not None:
            queue.enqueue(
                reason=REASON_SINGLE_ONLY,
                payload=_single_payload(fa),
                locator=fa.source_locator,
                priority=PRIORITY_SINGLE_ONLY,
            )
            n_enqueued += 1

    # 仅 B 侧独有。
    for key, fb in by_key_b.items():
        if key in by_key_a:
            continue
        result.only_in_b.append(fb)
        if queue is not None:
            queue.enqueue(
                reason=REASON_SINGLE_ONLY,
                payload=_single_payload(fb),
                locator=fb.source_locator,
                priority=PRIORITY_SINGLE_ONLY,
            )
            n_enqueued += 1

    result.n_auto_passed = len(result.agreed)
    result.n_enqueued = n_enqueued
    return result


def _single_payload(fact: ChannelFact) -> dict:
    """单通道独有项的入队载荷：含该侧维度键、值、locator、channel_name。"""
    return {
        "dim_key": list(fact.dim_key()),
        "channel_name": fact.channel_name,
        "value": fact.value,
        "locator": fact.source_locator,
    }
