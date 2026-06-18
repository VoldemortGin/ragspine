"""人审写回闭环：把已决议的复核项应用回 FactStore（决议生效）。

ReviewQueue 只记 approve / reject / 更正，不动 FactStore；本模块补上那一步——
让 SME 的决定真正改变 query() 的结果，且全程确定性、盖 provenance、按审计 seq 幂等：

- approve            → set_review_status(dim_key, APPROVED)，事实变可见。
- reject（无更正）    → set_review_status(dim_key, REJECTED)，事实仍不可见。
- reject（带更正值）  → 按身份重建一条 Fact、value=更正值、APPROVED，盖
                       corrected_by / corrected_audit_seq，源血缘保留；按 seq 幂等。

隐私：applier 不打任何带数值 / 答案 / 文本的 trace —— 决议生效不产生可观测副作用，
故此处不写日志（镜像 privacy-aware-traces 不变量）。
"""

from dataclasses import fields

from ragspine.ingestion.review.review_queue import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ReviewItem,
    ReviewQueue,
)
from ragspine.storage.fact_store import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    Fact,
    FactStore,
)

# Fact 中需要随 payload 往返的持久化字段（身份 + 数值 + 血缘 / v2 / 时效）。
# 排除 dimensions（内存袋）与 corrected_* 血缘戳（由 applier 在写回时盖，非入口数据）。
_EXCLUDED_FACT_FIELDS = frozenset(
    {"dimensions", "corrected_by", "corrected_audit_seq"}
)
_PAYLOAD_FACT_FIELDS = tuple(
    f.name for f in fields(Fact) if f.name not in _EXCLUDED_FACT_FIELDS
)


def fact_to_dict(fact: Fact) -> dict[str, object]:
    """把 Fact 序列化成可入 payload 的纯字典（仅持久化身份+值+血缘，不含袋/戳）。"""
    return {name: getattr(fact, name) for name in _PAYLOAD_FACT_FIELDS}


def fact_from_dict(data: dict[str, object]) -> Fact:
    """从 payload 字典重建 Fact（只取识别得到的持久化字段，余字段走默认）。"""
    kwargs = {k: data[k] for k in _PAYLOAD_FACT_FIELDS if k in data}
    return Fact(**kwargs)  # type: ignore[arg-type]


def enqueue_fact_for_review(
    store: FactStore,
    queue: ReviewQueue,
    fact: Fact,
    reason: str,
    priority: int = 100,
) -> int:
    """把一条事实送审：以 PENDING 写入 store（决议前不可见）并入队复核项。

    入队项 payload={"fact": <fact-as-dict>}、locator=fact.source_locator，
    返回复核项 id。这是人审写回的"上车口"，不改默认入库路径
    （确定性抽取仍 REVIEW_AUTO_APPROVED）。
    """
    pending_fact = fact_from_dict(fact_to_dict(fact))
    pending_fact.review_status = REVIEW_PENDING
    store.upsert_facts([pending_fact])
    return queue.enqueue(
        reason=reason,
        payload={"fact": fact_to_dict(fact)},
        locator=fact.source_locator,
        priority=priority,
    )


class ResolvedReviewApplier:
    """把已决议的复核项写回 FactStore，使决议对 query() 生效（确定性、幂等）。"""

    def __init__(self, store: FactStore, queue: ReviewQueue):
        self._store = store
        self._queue = queue

    def apply_item(self, item_id: int) -> str:
        """应用单个已决议项，返回短状态码。

        码：'skipped_pending'（仍待审，不动）/ 'approved' / 'rejected' /
        'corrected'（驳回带更正值，写回更正事实）/ 'noop'（已生效，幂等无改动）。
        """
        item = self._queue.get(item_id)
        if item is None:
            return "noop"
        if item.status == STATUS_PENDING:
            return "skipped_pending"

        fact = fact_from_dict(item.payload["fact"])
        dim_key = self._store.dim_key_for(fact)

        if item.status == STATUS_APPROVED:
            return self._apply_approve(dim_key)
        if item.status == STATUS_REJECTED:
            if item.corrected_value is None:
                return self._apply_reject(dim_key)
            return self._apply_correction(item_id, fact, dim_key, item)
        return "noop"

    def apply_all(self) -> dict[str, int]:
        """走遍所有已决议（非 pending）项并逐个 apply，返回状态码计数。可重复跑（幂等）。"""
        tally: dict[str, int] = {}
        for item in self._queue.list_resolved():
            assert item.id is not None
            code = self.apply_item(item.id)
            tally[code] = tally.get(code, 0) + 1
        return tally

    # --- 内部实现 --------------------------------------------------------

    def _apply_approve(self, dim_key: str) -> str:
        current = self._store.get_by_dim_key(dim_key)
        if current is not None and current.review_status == REVIEW_APPROVED:
            return "noop"
        self._store.set_review_status(dim_key, REVIEW_APPROVED)
        return "approved"

    def _apply_reject(self, dim_key: str) -> str:
        current = self._store.get_by_dim_key(dim_key)
        if current is not None and current.review_status == REVIEW_REJECTED:
            return "noop"
        self._store.set_review_status(dim_key, REVIEW_REJECTED)
        return "rejected"

    def _apply_correction(
        self, item_id: int, fact: Fact, dim_key: str, item: ReviewItem
    ) -> str:
        seq = self._resolving_seq(item_id)
        current = self._store.get_by_dim_key(dim_key)
        if current is not None and current.corrected_audit_seq == seq:
            return "noop"
        corrected = fact_from_dict(fact_to_dict(fact))
        corrected.value = float(item.corrected_value)  # type: ignore[arg-type]
        corrected.review_status = REVIEW_APPROVED
        corrected.corrected_by = item.actor
        corrected.corrected_audit_seq = seq
        self._store.upsert_facts([corrected])
        return "corrected"

    def _resolving_seq(self, item_id: int) -> int | None:
        """取解析决议的那条 approve/reject 审计记录 seq（审计序最后一条决议）。"""
        for record in reversed(self._queue.audit_trail(item_id)):
            if record.action in ("approve", "reject"):
                return record.seq
        return None
