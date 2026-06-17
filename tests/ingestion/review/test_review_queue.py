"""人工复核队列 ReviewQueue 的红色测试（TDD）。

只验证外部行为：入队 / 优先级排序 / approve-reject 状态机 /
reject 附更正值 / 追加式审计（actor + 时间 + 依据）/ 幂等性。
对应 PRD user stories 22 / 23 / 25 / 28。

契约阶段所有方法体 raise NotImplementedError —— 因此本文件的每个用例
都断言"方法真正工作后才成立的外部行为"，红色阶段应全部 FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.ingestion.review.review_queue import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    AuditRecord,
    ReviewItem,
    ReviewQueue,
)


@pytest.fixture
def queue(tmp_db_path):
    """每个测试一个独立的复核队列连接（建表 init_schema 由测试体内首调，

    以便契约未实现时 NotImplementedError 作为用例 FAILURE 而非 fixture ERROR 暴露）。
    """
    q = ReviewQueue(tmp_db_path)
    yield q
    q.close()


# --------------------------------------------------------------------------- #
# enqueue + get：入队基本契约（story #22）
# --------------------------------------------------------------------------- #
def test_enqueue_returns_id_and_persists_item(queue):
    """story #22 入队返回新 id，且能按 id 取回该项（pending、字段原样）。"""
    queue.init_schema()
    item_id = queue.enqueue(
        reason="low_confidence",
        payload={"metric": "REVENUE", "value": 2350.0},
        locator="sheet=HK!C4",
        priority=10,
    )
    assert isinstance(item_id, int)

    item = queue.get(item_id)
    assert isinstance(item, ReviewItem)
    assert item.id == item_id
    assert item.reason == "low_confidence"
    assert item.payload == {"metric": "REVENUE", "value": 2350.0}
    assert item.locator == "sheet=HK!C4"
    assert item.priority == 10
    assert item.status == STATUS_PENDING
    assert item.actor is None
    assert item.corrected_value is None


def test_enqueue_default_priority(queue):
    """story #22 不指定 priority 时落默认值 100。"""
    queue.init_schema()
    item_id = queue.enqueue("unconfirmed_mapping", {"a": 1}, "sheet=HK!A1")
    item = queue.get(item_id)
    assert item.priority == 100


def test_enqueue_payload_roundtrip_nested(queue):
    """story #22 payload 为嵌套结构（双通道两侧结果）须 JSON round-trip 完整。"""
    queue.init_schema()
    payload = {
        "channel_a": {"value": 100.0, "period": "FY2024"},
        "channel_b": {"value": 101.0, "period": "FY2024"},
        "delta": 1.0,
        "labels": ["新产品线", "成熟"],
    }
    item_id = queue.enqueue("cross_channel_mismatch", payload, "sheet=HK!C4", priority=5)
    item = queue.get(item_id)
    assert item.payload == payload


def test_get_missing_returns_none(queue):
    """story #22 取不存在的 id 返回 None（不抛异常）。"""
    queue.init_schema()
    assert queue.get(999) is None


def test_enqueue_ids_are_unique(queue):
    """story #22 连续入队应分配互不相同的 id。"""
    queue.init_schema()
    ids = [
        queue.enqueue("low_confidence", {"i": i}, f"sheet=HK!C{i}")
        for i in range(5)
    ]
    assert len(set(ids)) == 5


# --------------------------------------------------------------------------- #
# list_pending：优先级排序 + 只列 pending（story #22）
# --------------------------------------------------------------------------- #
def test_list_pending_sorted_by_priority_ascending(queue):
    """story #22 list_pending 按 priority 升序（数值越小越先审）。"""
    queue.init_schema()
    queue.enqueue("r", {"n": "mid"}, "loc=mid", priority=50)
    queue.enqueue("r", {"n": "high"}, "loc=high", priority=1)
    queue.enqueue("r", {"n": "low"}, "loc=low", priority=100)

    pending = queue.list_pending()
    priorities = [it.priority for it in pending]
    assert priorities == sorted(priorities)
    assert priorities[0] == 1
    assert priorities[-1] == 100


def test_list_pending_carries_locator(queue):
    """story #22 每个待审项都带原文精确定位回链 locator。"""
    queue.init_schema()
    queue.enqueue("low_confidence", {"v": 1}, "sheet=HK_Performance!C4", priority=3)
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].locator == "sheet=HK_Performance!C4"


def test_list_pending_excludes_approved_and_rejected(queue):
    """story #22/#23 已 approve / reject 的项不再出现在 pending 列表。"""
    queue.init_schema()
    keep = queue.enqueue("r", {"n": "keep"}, "loc=keep", priority=10)
    to_approve = queue.enqueue("r", {"n": "ok"}, "loc=ok", priority=20)
    to_reject = queue.enqueue("r", {"n": "bad"}, "loc=bad", priority=30)

    queue.approve(to_approve, actor="sme_fin")
    queue.reject(to_reject, actor="sme_fin", note="错列")

    pending = queue.list_pending()
    ids = {it.id for it in pending}
    assert ids == {keep}


def test_list_pending_empty_initially(queue):
    """story #22 空队列 list_pending 返回空列表。"""
    queue.init_schema()
    assert queue.list_pending() == []


# --------------------------------------------------------------------------- #
# approve：状态流转 + 留痕（story #23）
# --------------------------------------------------------------------------- #
def test_approve_sets_status_and_actor(queue):
    """story #23 approve 后状态变 approved 并记录处理人与备注。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="alice_fin", note="核对原件无误")

    item = queue.get(item_id)
    assert item.status == STATUS_APPROVED
    assert item.actor == "alice_fin"
    assert item.note == "核对原件无误"


def test_approve_without_note(queue):
    """story #23 approve 可不附 note，状态仍正确流转。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="bob_hr")
    item = queue.get(item_id)
    assert item.status == STATUS_APPROVED
    assert item.actor == "bob_hr"


# --------------------------------------------------------------------------- #
# reject + corrected_value：状态流转 + 更正值直接生效（story #23/#25）
# --------------------------------------------------------------------------- #
def test_reject_sets_status_actor_note(queue):
    """story #23 reject 后状态变 rejected 并留痕处理人与依据。"""
    queue.init_schema()
    item_id = queue.enqueue("cross_channel_mismatch", {"v": 1}, "sheet=HK!C4")
    queue.reject(item_id, actor="carol_rc", note="两通道不一致，以右表为准")

    item = queue.get(item_id)
    assert item.status == STATUS_REJECTED
    assert item.actor == "carol_rc"
    assert item.note == "两通道不一致，以右表为准"


def test_reject_with_corrected_value(queue):
    """story #25 reject 附更正值，更正值随项持久化可取回。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"value": 2350.0}, "sheet=HK!C4")
    queue.reject(item_id, actor="dave_fin", note="OCR 读错", corrected_value=2530.0)

    item = queue.get(item_id)
    assert item.status == STATUS_REJECTED
    assert item.corrected_value == 2530.0


def test_reject_corrected_value_can_be_structured(queue):
    """story #25 更正值可为结构化对象（如更正后的整条 fact 候选）。"""
    queue.init_schema()
    corrected = {"metric": "REVENUE", "value": 2530.0, "unit": "US$m"}
    item_id = queue.enqueue("low_confidence", {"value": 2350.0}, "sheet=HK!C4")
    queue.reject(item_id, actor="dave_fin", corrected_value=corrected)

    item = queue.get(item_id)
    assert item.corrected_value == corrected


def test_reject_without_corrected_value_keeps_none(queue):
    """story #25 不附更正值时 corrected_value 仍为 None（纯驳回）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"value": 1}, "sheet=HK!C4")
    queue.reject(item_id, actor="dave_fin", note="整条作废")

    item = queue.get(item_id)
    assert item.status == STATUS_REJECTED
    assert item.corrected_value is None


# --------------------------------------------------------------------------- #
# 非法状态流转：终态不可再变（story #23 状态机）
# --------------------------------------------------------------------------- #
def test_approve_already_approved_is_rejected(queue):
    """story #23 已 approved 项不可再次 approve（终态，非法流转拒绝）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="alice")
    with pytest.raises(Exception):
        queue.approve(item_id, actor="alice")


def test_reject_already_approved_is_rejected(queue):
    """story #23 已 approved 项不可再 reject（终态，非法流转拒绝）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="alice")
    with pytest.raises(Exception):
        queue.reject(item_id, actor="alice", note="反悔")


def test_approve_already_rejected_is_rejected(queue):
    """story #23 已 rejected 项不可再 approve（终态，非法流转拒绝）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.reject(item_id, actor="bob", note="作废")
    with pytest.raises(Exception):
        queue.approve(item_id, actor="bob")


def test_approve_missing_item_raises(queue):
    """story #23 对不存在的项 approve 应报错而非静默成功。"""
    queue.init_schema()
    with pytest.raises(Exception):
        queue.approve(123456, actor="ghost")


def test_reject_missing_item_raises(queue):
    """story #23 对不存在的项 reject 应报错而非静默成功。"""
    queue.init_schema()
    with pytest.raises(Exception):
        queue.reject(123456, actor="ghost", note="x")


# --------------------------------------------------------------------------- #
# 追加式审计：actor + 时间 + 依据，时间序，不可篡改（story #28）
# --------------------------------------------------------------------------- #
def test_audit_trail_records_enqueue(queue):
    """story #28 入队即产生一条 enqueue 审计记录（item_id 关联、有时间戳）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    trail = queue.audit_trail(item_id)

    assert len(trail) == 1
    rec = trail[0]
    assert isinstance(rec, AuditRecord)
    assert rec.item_id == item_id
    assert rec.action == "enqueue"
    assert rec.at  # 时间戳非空


def test_audit_trail_appends_on_approve(queue):
    """story #28 approve 在原审计后追加一条记录（含 actor / 时间 / note）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="alice_fin", note="依据原件 C4")

    trail = queue.audit_trail(item_id)
    actions = [r.action for r in trail]
    assert actions == ["enqueue", "approve"]

    approve_rec = trail[-1]
    assert approve_rec.actor == "alice_fin"
    assert approve_rec.note == "依据原件 C4"
    assert approve_rec.at


def test_audit_trail_appends_on_reject_with_corrected_value(queue):
    """story #28/#25 reject 审计记录留更正值依据（detail 携带 corrected_value）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"value": 2350.0}, "sheet=HK!C4")
    queue.reject(item_id, actor="dave_fin", note="OCR 读错", corrected_value=2530.0)

    trail = queue.audit_trail(item_id)
    actions = [r.action for r in trail]
    assert actions == ["enqueue", "reject"]

    reject_rec = trail[-1]
    assert reject_rec.actor == "dave_fin"
    assert reject_rec.note == "OCR 读错"
    assert reject_rec.detail.get("corrected_value") == 2530.0


def test_audit_trail_is_time_ordered(queue):
    """story #28 审计记录按时间序返回（追加式，先入队后处理）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="alice")

    trail = queue.audit_trail(item_id)
    timestamps = [r.at for r in trail]
    assert timestamps == sorted(timestamps)


def test_audit_trail_append_only_failed_transition_preserved(queue):
    """story #28 非法流转被拒后，已有审计记录不被篡改或回滚（追加式不可篡改）。"""
    queue.init_schema()
    item_id = queue.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    queue.approve(item_id, actor="alice")

    trail_before = queue.audit_trail(item_id)
    with pytest.raises(Exception):
        queue.approve(item_id, actor="mallory")

    trail_after = queue.audit_trail(item_id)
    # 非法操作不得新增成功审计，也不得删改既有记录。
    assert [r.action for r in trail_after] == [r.action for r in trail_before]
    assert trail_after[-1].actor == "alice"


def test_audit_trail_isolated_per_item(queue):
    """story #28 每条项的审计互相隔离，互不串台。"""
    queue.init_schema()
    id1 = queue.enqueue("r", {"n": 1}, "loc=1")
    id2 = queue.enqueue("r", {"n": 2}, "loc=2")
    queue.approve(id1, actor="alice")
    queue.reject(id2, actor="bob", note="x")

    trail1 = queue.audit_trail(id1)
    trail2 = queue.audit_trail(id2)
    assert all(r.item_id == id1 for r in trail1)
    assert all(r.item_id == id2 for r in trail2)
    assert [r.action for r in trail1] == ["enqueue", "approve"]
    assert [r.action for r in trail2] == ["enqueue", "reject"]


# --------------------------------------------------------------------------- #
# 持久化 / 隔离：跨连接可见（story #28 不可篡改时间序的持久化前提）
# --------------------------------------------------------------------------- #
def test_state_persists_across_reopen(tmp_db_path):
    """story #28 审批结果与审计写盘后，重开连接仍可完整读回。"""
    q1 = ReviewQueue(tmp_db_path)
    q1.init_schema()
    item_id = q1.enqueue("low_confidence", {"v": 1}, "sheet=HK!C4")
    q1.approve(item_id, actor="alice", note="ok")
    q1.close()

    q2 = ReviewQueue(tmp_db_path)
    try:
        item = q2.get(item_id)
        assert item.status == STATUS_APPROVED
        assert item.actor == "alice"
        trail = q2.audit_trail(item_id)
        assert [r.action for r in trail] == ["enqueue", "approve"]
    finally:
        q2.close()
