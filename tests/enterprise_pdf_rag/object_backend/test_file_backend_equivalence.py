"""FileBackend 的字节级等价:对同一组操作,它产生的目录与现有 store 代码逐字节一致。

基准由**现有写路径**生成(``LocalDocumentStore.put`` / ``ProcessingStore.cache`` 与指针 /
``save_document_tree`` / ``json_completion`` 的 ``_immutable_write`` 与 claim 家族),
FileBackend 在另一个根重放同样的操作,然后比对两棵目录树的文件集合与每个文件的字节。
含 EPERM(无硬链接)回退路径。
"""

import json
from collections.abc import Callable
from pathlib import Path
from time import time

import pytest

from enterprise_pdf_rag.adapters.document_store import (
    INLINE_ARTIFACT_LIMIT,
    LocalDocumentStore,
)
from enterprise_pdf_rag.adapters.http.processing_schemas import DocumentTreeRecord, StageEnvelope
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.files import (
    FileBackend,
    FileModelCacheBackend,
)
from ragspine.common.evidence.object_backend.protocol import (
    ClaimOwner,
    DamagedEntry,
    StageEntry,
    StoreConflict,
)
from ragspine.common.evidence.providers import json_completion
from ragspine.extraction.evidence.page.models import StageOutcome, StageState
from tests.enterprise_pdf_rag.adapters.legacy_pointer_helpers import write_pointer
from tests.enterprise_pdf_rag.adapters.no_hard_link_helpers import forbid_hard_links
from tests.enterprise_pdf_rag.object_backend.conftest import live_owner, sha, tree

FP = "b" * 64

# 本文件钉的是 FileBackend 与三处现有**文件**写路径的逐字节等价:store 侧必须跑在
# 文件布局上(PR-2 之后 store 默认经 registry,auto 在本机会选 sqlite)。
pytestmark = pytest.mark.usefixtures("files_object_backend")


@pytest.fixture(params=["hard-links", "no-hard-links"])
def placement(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """每个等价测试双跑:支持硬链接的文件系统,与 EPERM 回退路径(ADR 0020)。"""
    if request.param == "no-hard-links":
        forbid_hard_links(monkeypatch)
    return str(request.param)


def test_put_object_matches_local_document_store(placement: str, tmp_path: Path) -> None:
    baseline_root, replay_root = tmp_path / "baseline", tmp_path / "replay"
    store = LocalDocumentStore(baseline_root, verify_every_load=False)
    backend = FileBackend(replay_root)

    payloads = [b'{"a": 1}' * 50, b"plain text", b'{"a": 1}' * 50, b"\x89binary" * 300]
    for payload in payloads:
        ref = store.put(payload, media_type="application/octet-stream")
        assert backend.put_object(ref.sha256, payload, "application/octet-stream") in {
            "placed",
            "existing",
        }
    assert tree(baseline_root) == tree(replay_root)

    # 损坏同一个对象,双方都以同样的字节修复(ADR 0029 的写路径自愈)。
    damaged = sha(payloads[1])
    for root in (baseline_root, replay_root):
        (root / "objects" / "sha256-sharded" / damaged[:2] / damaged).write_bytes(b"\x00torn")
    fresh_store = LocalDocumentStore(baseline_root, verify_every_load=False)
    fresh_store.put(payloads[1], media_type="application/octet-stream")
    backend.put_object(damaged, payloads[1], "application/octet-stream")
    assert tree(baseline_root) == tree(replay_root)


def _outcome(store: ProcessingStore, data: bytes, fingerprint: str = FP) -> StageOutcome:
    ref = store.assets.put(data, media_type="application/json")
    return StageOutcome("description", fingerprint, StageState.SUCCEEDED, "producer-v1", ref)


def _entry(outcome: StageOutcome) -> StageEntry:
    envelope = StageEnvelope(outcome=outcome).model_dump_json().encode()
    return StageEntry(sha(envelope), envelope)


def test_stage_cache_matches_processing_store(placement: str, tmp_path: Path) -> None:
    baseline_root, replay_root = tmp_path / "baseline", tmp_path / "replay"
    store = ProcessingStore(baseline_root, verify_every_load=False)
    backend = FileBackend(replay_root)

    artifact = b'{"result": 1}' * 30
    outcome = _outcome(store, artifact)
    entry = _entry(outcome)
    store.cache(outcome)
    store.cache(outcome)  # 幂等
    backend.put_object(outcome.artifact.sha256, artifact, "application/json")  # type: ignore[union-attr]
    assert backend.put_stage_entry(FP, entry) == "placed"
    assert backend.put_stage_entry(FP, entry) == "existing"
    assert tree(baseline_root) == tree(replay_root)

    # 同指纹另一份产出:两边都拒绝、都不写(冲突语义等价;文案是各层自己的)。
    other = _outcome(store, b'{"result": 2}' * 30)
    backend.put_object(other.artifact.sha256, b'{"result": 2}' * 30, "application/json")  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already names another actual output"):
        store.cache(other)
    with pytest.raises(StoreConflict, match="Conflicting immutable stage cache entry"):
        backend.put_stage_entry(FP, _entry(other))
    assert tree(baseline_root) == tree(replay_root)

    # 损坏的指针在两边都被同样的字节替换(cache() 的 damaged 分支 ↔ replace=True)。
    pointer = Path("stage-cache-sharded") / FP[:2] / FP
    for root in (baseline_root, replay_root):
        (root / pointer).write_bytes(b"torn pointer")
    fresh = ProcessingStore(baseline_root, verify_every_load=False)
    fresh.cache(outcome)
    with pytest.raises(DamagedEntry):
        backend.stage_entry(FP)
    backend.put_stage_entry(FP, entry, replace=True)
    assert tree(baseline_root) == tree(replay_root)

    # current-processing 指针与 document-tree 记录。
    digest = outcome.artifact.sha256  # type: ignore[union-attr]
    write_pointer(baseline_root / "current-processing", digest)
    backend.set_pointer("current-processing", digest)
    record = DocumentTreeRecord(
        processing_id="c" * 64,
        producer="tree-v1",
        state=StageState.DEFERRED,
        diagnostic="document_tree_budget_deferred",
        artifact=None,
        summary_calls=0,
    )
    store.save_document_tree("c" * 64, record)
    backend.put_record("document-tree/" + "c" * 64 + ".json", record.model_dump_json().encode())
    assert tree(baseline_root) == tree(replay_root)
    assert backend.pointer("current-processing") == digest


def _claim_keys(path: Path) -> set[str]:
    return set(json.loads(path.read_bytes()))


def test_model_cache_matches_json_completion_write_path(placement: str, tmp_path: Path) -> None:
    baseline, replay = tmp_path / "baseline", tmp_path / "replay"
    backend = FileModelCacheBackend(replay)
    fingerprint = "d" * 64
    body = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
    digest = sha(body)
    record = json.dumps(
        {"request_fingerprint": fingerprint, "response_digest": digest, "failure_code": None}
    ).encode()
    context = b'{"payload": {"model": "m"}}'

    # claim → response → context → record → release:json_completion 的一次成功调用留下的字节。
    record_path = baseline / "requests" / f"{fingerprint}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    generation = json_completion._claim_request(record_path, fingerprint, 300)
    owner = live_owner(300)
    assert backend.claim(fingerprint, owner) == generation == 0
    claim_a = record_path.with_suffix(".json.claim")
    claim_b = replay / "requests" / f"{fingerprint}.json.claim"
    assert _claim_keys(claim_a) == _claim_keys(claim_b)  # 同一组持有者字段,从不含正文
    json_completion._immutable_write(
        baseline / "responses" / f"{digest}.json", body, replace_damaged=True
    )
    backend.put_response(digest, body)
    json_completion._immutable_write(baseline / "contexts" / f"{fingerprint}.json", context)
    backend.put_context(fingerprint, context)
    json_completion._immutable_write(record_path, record)
    backend.put_record(fingerprint, record)
    json_completion._release_claims(record_path)
    backend.release(fingerprint, owner)
    assert tree(baseline) == tree(replay)

    # 重复 / 冲突:同字节 no-op,异字节两边都拒绝且不改写。
    json_completion._immutable_write(record_path, record)
    backend.put_record(fingerprint, record)
    with pytest.raises(json_completion.JsonCompletionError, match="cache_conflict"):
        json_completion._immutable_write(record_path, b'{"other": 1}')
    with pytest.raises(StoreConflict, match="cache_conflict"):
        backend.put_record(fingerprint, b'{"other": 1}')
    assert tree(baseline) == tree(replay)

    # .retry-1 记录(ADR 0021)与损坏响应的替换(ADR 0029)。
    retry = json.dumps({"request_fingerprint": fingerprint, "response_digest": None}).encode()
    json_completion._immutable_write(baseline / "requests" / f"{fingerprint}.retry-1.json", retry)
    backend.put_record(f"{fingerprint}.retry-1", retry)
    for root in (baseline, replay):
        (root / "responses" / f"{digest}.json").write_bytes(b"torn response")
    json_completion._immutable_write(
        baseline / "responses" / f"{digest}.json", body, replace_damaged=True
    )
    backend.put_response(digest, body)
    assert tree(baseline) == tree(replay)


def test_claim_takeover_files_match_json_completion(
    placement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """过期持有者:两边都以 .takeover-1 的排他创建接管,文件名与字段集合一致。"""
    baseline, replay = tmp_path / "baseline", tmp_path / "replay"
    backend = FileModelCacheBackend(replay)
    fingerprint = "e" * 64
    record_path = baseline / "requests" / f"{fingerprint}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    json_completion._claim_request(record_path, fingerprint, 10)
    assert backend.claim(fingerprint, live_owner(10)) == 0

    clock: Callable[[], float] = lambda: time() + 100_000  # noqa: E731
    monkeypatch.setattr(json_completion, "_wall_clock", clock)
    monkeypatch.setattr(lease, "wall_clock", clock)
    assert json_completion._claim_request(record_path, fingerprint, 10) == 1
    late = ClaimOwner(
        host=live_owner().host,
        pid=live_owner().pid,
        process=live_owner().process,
        created_at=round(clock(), 3),
        lease_seconds=10,
    )
    assert backend.claim(fingerprint, late) == 1
    claims_a = {path.name for path in (baseline / "requests").iterdir()}
    claims_b = {path.name for path in (replay / "requests").iterdir()}
    assert (
        claims_a
        == claims_b
        == {f"{fingerprint}.json.claim", f"{fingerprint}.json.claim.takeover-1"}
    )


def test_inline_stage_output_matches_cache_output(placement: str, tmp_path: Path) -> None:
    """Amendment 2:小产物内联进指针;与 ProcessingStore.cache_output 的字节逐位一致。"""
    baseline_root, replay_root = tmp_path / "baseline", tmp_path / "replay"
    store = ProcessingStore(baseline_root, verify_every_load=False)
    backend = FileBackend(replay_root)

    payload = b'{"inline": "output"}' * 20  # 远小于 INLINE_ARTIFACT_LIMIT:内联,不落对象
    outcome = store.cache_output("description", FP, "producer-v1", payload)
    assert outcome.artifact is not None
    envelope = StageEnvelope(outcome=outcome).model_dump_json().encode()
    backend.put_stage_entry(FP, StageEntry(sha(envelope), envelope, payload))
    assert tree(baseline_root) == tree(replay_root)  # 没有对象文件,只有一个三段指针

    read = backend.stage_entry(FP)
    assert read is not None and read.product == payload

    # 超过内联上限:两边都退回"对象 + 两段指针"。
    big = b"x" * (INLINE_ARTIFACT_LIMIT + 1)
    fp_big = "f" * 64
    outcome_big = store.cache_output("description", fp_big, "producer-v1", big)
    assert outcome_big.artifact is not None
    envelope_big = StageEnvelope(outcome=outcome_big).model_dump_json().encode()
    backend.put_object(outcome_big.artifact.sha256, big, "application/json")
    backend.put_stage_entry(fp_big, StageEntry(sha(envelope_big), envelope_big))
    assert tree(baseline_root) == tree(replay_root)
