"""人审写回闭环 ResolvedReviewApplier 的红色测试（TDD）。

闭环此前半开：ReviewQueue 记下 approve / reject / 更正，但没有任何东西把决议
写回 FactStore，于是 SME 的决定永远改变不了 query() 的结果。本测试冻结
applier 的外部契约：决议生效（approve→可见 / reject→不可见 /
reject+更正→更正后的可见事实，盖 corrected_by / corrected_audit_seq），
且按审计 seq 幂等。

只断言外部行为：enqueue → query 不可见；apply 后 query 的可见性与值；
更正事实的血缘戳；重复 apply 的幂等；以及隐私不泄漏（applier 不把事实数值
打进任何 trace / 日志）。
"""

import logging
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.review.apply import (
    ResolvedReviewApplier,
    enqueue_fact_for_review,
)
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.storage.fact_store import (
    REVIEW_APPROVED,
    REVIEW_REJECTED,
    Fact,
    FactStore,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_db_path):
    """临时 sqlite FactStore（建表 init_schema）。"""
    fs = FactStore(tmp_db_path)
    fs.init_schema()
    yield fs
    fs.close()


@pytest.fixture
def queue(tmp_sqlite_factory):
    """临时 sqlite ReviewQueue（与 FactStore 分库，建表 init_schema）。"""
    q = ReviewQueue(tmp_sqlite_factory("review"))
    q.init_schema()
    yield q
    q.close()


@pytest.fixture
def applier(store, queue):
    """绑定 store + queue 的写回应用器。"""
    return ResolvedReviewApplier(store, queue)


def _base_fact(**overrides) -> Fact:
    """构造一条满足必填维度的 Fact，overrides 覆盖任意字段。"""
    data = dict(
        metric_code="REVENUE",
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period="2024",
        value=2680.0,
        unit="US$m",
        source_doc_id="HK_Performance.xlsx",
        source_locator="HK_Performance!B2",
    )
    data.update(overrides)
    return Fact(**data)


def _query_one(store: FactStore, fact: Fact) -> Fact | None:
    """按身份查可见事实，命中返回唯一一条，否则 None。"""
    rows = store.query(
        fact.metric_code, fact.entity, fact.period_type, fact.period, fact.channel
    )
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# 1. 入队待审的事实不可见（pending → 查不到）
# --------------------------------------------------------------------------- #
def test_enqueued_fact_is_invisible_until_resolved(store, queue):
    """enqueue_fact_for_review 写入 pending 事实，query 默认查不到（防编造路径）。"""
    fact = _base_fact()
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")
    assert isinstance(item_id, int)
    # 队列里有一条 pending、locator 回链原文
    item = queue.get(item_id)
    assert item is not None
    assert item.locator == fact.source_locator
    assert item.payload.get("fact") is not None
    # query 默认只看可见状态 → pending 查不到
    assert _query_one(store, fact) is None


# --------------------------------------------------------------------------- #
# 2. approve → apply → 可见
# --------------------------------------------------------------------------- #
def test_approve_then_apply_makes_fact_visible(store, queue, applier):
    """approve 后 apply_item → 该事实对 query 可见。"""
    fact = _base_fact()
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")
    queue.approve(item_id, actor="alice_fin", note="核对原件无误")

    code = applier.apply_item(item_id)
    assert code == "approved"

    got = _query_one(store, fact)
    assert got is not None
    assert got.value == fact.value
    assert got.review_status == REVIEW_APPROVED


# --------------------------------------------------------------------------- #
# 3. reject（无更正）→ apply → 仍不可见
# --------------------------------------------------------------------------- #
def test_reject_without_correction_then_apply_stays_invisible(store, queue, applier):
    """reject 不带更正值 → apply 后该事实仍不可见（query 查不到）。"""
    fact = _base_fact()
    item_id = enqueue_fact_for_review(store, queue, fact, reason="cross_channel_mismatch")
    queue.reject(item_id, actor="carol_rc", note="错列，撤下")

    code = applier.apply_item(item_id)
    assert code == "rejected"

    assert _query_one(store, fact) is None
    # 审计视角（放开状态）能看到这条仍在、状态为 rejected。
    audit = store.query(
        fact.metric_code, fact.entity, fact.period_type, fact.period,
        fact.channel, review_statuses=None,
    )
    assert len(audit) == 1
    assert audit[0].review_status == REVIEW_REJECTED


# --------------------------------------------------------------------------- #
# 4. reject + 更正值 → apply → 返回更正后的值，盖血缘戳
# --------------------------------------------------------------------------- #
def test_reject_with_correction_applies_corrected_visible_fact(store, queue, applier):
    """reject 附更正值 → apply 后 query 返回更正值；Fact 盖 corrected_by /
    corrected_audit_seq；源血缘保留。"""
    fact = _base_fact(value=2350.0)
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")
    queue.reject(item_id, actor="dave_fin", note="OCR 读错", corrected_value=2530.0)

    code = applier.apply_item(item_id)
    assert code == "corrected"

    got = _query_one(store, fact)
    assert got is not None
    assert got.value == 2530.0
    assert got.review_status == REVIEW_APPROVED
    assert got.corrected_by == "dave_fin"
    # 解析决议的 reject 审计 seq 应被盖到事实上
    trail = queue.audit_trail(item_id)
    reject_seq = trail[-1].seq
    assert reject_seq is not None
    assert got.corrected_audit_seq == reject_seq
    # 源血缘原样保留
    assert got.source_doc_id == fact.source_doc_id
    assert got.source_locator == fact.source_locator


# --------------------------------------------------------------------------- #
# 5. pending 项 apply 是 no-op（skipped_pending）
# --------------------------------------------------------------------------- #
def test_apply_pending_item_is_skipped(store, queue, applier):
    """尚未决议（pending）的项 apply_item → skipped_pending，不改可见性。"""
    fact = _base_fact()
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")

    code = applier.apply_item(item_id)
    assert code == "skipped_pending"
    assert _query_one(store, fact) is None


# --------------------------------------------------------------------------- #
# 6. 幂等：重复 apply_item / apply_all 不重复生效
# --------------------------------------------------------------------------- #
def test_apply_item_idempotent_on_correction(store, queue, applier):
    """更正事实只生效一次：二次 apply_item 返回 noop，值不变。"""
    fact = _base_fact(value=2350.0)
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")
    queue.reject(item_id, actor="dave_fin", corrected_value=2530.0)

    assert applier.apply_item(item_id) == "corrected"
    assert applier.apply_item(item_id) == "noop"

    got = _query_one(store, fact)
    assert got is not None
    assert got.value == 2530.0


def test_apply_item_idempotent_on_approve(store, queue, applier):
    """approve 二次 apply_item → noop（状态已是 approved）。"""
    fact = _base_fact()
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")
    queue.approve(item_id, actor="alice_fin")

    assert applier.apply_item(item_id) == "approved"
    assert applier.apply_item(item_id) == "noop"


def test_apply_all_tally_and_idempotent(store, queue, applier):
    """apply_all 走遍所有已决议项并计数；二次运行全 noop（幂等）。"""
    f_app = _base_fact(metric_code="REVENUE", period="2024")
    f_rej = _base_fact(metric_code="OPEX", period="2024", value=900.0)
    f_cor = _base_fact(metric_code="NBV", period="2024", value=100.0)
    f_pending = _base_fact(metric_code="VONB", period="2024", value=50.0)

    id_app = enqueue_fact_for_review(store, queue, f_app, reason="r")
    id_rej = enqueue_fact_for_review(store, queue, f_rej, reason="r")
    id_cor = enqueue_fact_for_review(store, queue, f_cor, reason="r")
    enqueue_fact_for_review(store, queue, f_pending, reason="r")  # 留 pending

    queue.approve(id_app, actor="a")
    queue.reject(id_rej, actor="b", note="撤下")
    queue.reject(id_cor, actor="c", corrected_value=123.0)

    tally = applier.apply_all()
    assert tally.get("approved", 0) == 1
    assert tally.get("rejected", 0) == 1
    assert tally.get("corrected", 0) == 1
    # pending 项不被 apply_all 触碰（它只走已决议项）

    # 第二次：全部已生效 → 计数应全是 noop（更正/通过/驳回都自然 no-op）
    tally2 = applier.apply_all()
    assert tally2.get("approved", 0) == 0
    assert tally2.get("corrected", 0) == 0
    assert tally2.get("noop", 0) >= 1

    # 终态可见性正确
    assert _query_one(store, f_app) is not None
    assert _query_one(store, f_rej) is None
    got_cor = _query_one(store, f_cor)
    assert got_cor is not None and got_cor.value == 123.0


# --------------------------------------------------------------------------- #
# 7. 隐私：applier 不把事实数值泄漏进 trace / 日志
# --------------------------------------------------------------------------- #
def test_apply_does_not_leak_fact_value(store, queue, applier, caplog):
    """applier 若打日志，只许码 / 计数 / 计时，绝不许出现事实数值。
    （镜像 test_trace_does_not_leak_sensitive_values 的断言模式。）"""
    sensitive = 987654.0
    fact = _base_fact(value=2350.0)
    item_id = enqueue_fact_for_review(store, queue, fact, reason="low_confidence")
    queue.reject(item_id, actor="dave_fin", corrected_value=sensitive)

    with caplog.at_level(logging.DEBUG):
        applier.apply_item(item_id)

    # 写回确实生效（值已落库）
    got = _query_one(store, fact)
    assert got is not None and got.value == sensitive

    blob = "\n".join(
        rec.getMessage() + " " + str(rec.__dict__) for rec in caplog.records
    )
    assert "987654" not in blob
