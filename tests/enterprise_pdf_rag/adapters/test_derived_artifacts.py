"""ADR 0048:派生产物(对象 SVG 裁剪 / 图 PNG 渲染)不落盘,只存 digest,用时重算并核对。

- 默认(``APP_PERSIST_DERIVED_ARTIFACTS`` 未设,非 staged)逐字节不变,不写任何标记;
- 设为 false:对象表 / 内联产物里没有任何派生 SVG / PNG 字节,但 stage 记录、发布 id、
  模型请求指纹都与落盘模式相同;重跑零调用;挂载、requalify / publish 的证明核对全过;
- full 模式摄入时写的审阅页与 ``export_document_review`` 的产出与落盘模式逐字节相同;
- 重算结果对不上记录的 digest(版本漂移 / 篡改)→ ``DerivedArtifactDrift`` + trace,绝不静默使用;
- staged 下未显式设置时默认不落盘,显式 true 仍落盘。
"""

import logging
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pytest

from enterprise_pdf_rag.adapters import derived_artifacts
from enterprise_pdf_rag.adapters.derived_artifacts import (
    DERIVED_STAGES,
    DerivedArtifactDrift,
    object_stage_bytes,
    recomputable,
    recompute_object_stage,
)
from enterprise_pdf_rag.adapters.document_catalog import mount_document, scan_catalog
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult
from enterprise_pdf_rag.adapters.pdf_ingestion import export_document_review
from enterprise_pdf_rag.adapters.pdfspine_svg import crop_native_svg
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore, persist_derived_default
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit
from ragspine.common.evidence.configs import Settings, get_settings
from ragspine.common.evidence.object_backend import staged
from ragspine.common.evidence.object_backend.probe import clear_probe_cache
from ragspine.extraction.evidence.page.models import ObjectKind, ProcessingManifest
from tests.enterprise_pdf_rag.adapters.lite_ingest_helpers import (
    FULL_PUBLISHED_ID,
    FULL_REQUESTS_DIGEST,
    FULL_STORE_DIGEST,
    FULL_TASKS,
    lite_env,
    mixed_folder,
    run_mode,
    store_digest,
)

pytestmark = pytest.mark.usefixtures("_fresh_settings")


@pytest.fixture
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _run(tmp_path: Path, mode: str = "full") -> FolderPipelineResult:
    mixed_folder(tmp_path)
    return run_mode(tmp_path, mode)


def _document_root(result: FolderPipelineResult) -> Path:
    (document,) = result.documents
    assert document.ingestion is not None
    return Path(document.ingestion.processing_store).parent


def _published(root: Path) -> tuple[LocalDocumentStore, ProcessingStore, ProcessingManifest]:
    sources = LocalDocumentStore(root / "source", activate_on_publish=False)
    outputs = ProcessingStore(root / "processing")
    return sources, outputs, outputs.load_current()[1]


def _derived_stages(manifest: ProcessingManifest) -> list[tuple[int, str, str, int]]:
    """(page, object id, stage, index in the page) of every succeeded derived stage."""
    found = []
    for position, page in enumerate(manifest.pages):
        for record in page.objects:
            for stage in record.stages:
                if stage.stage in DERIVED_STAGES and stage.artifact is not None:
                    found.append((page.page_index, record.object_id, stage.stage, position))
    return found


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---- 开关的默认值 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, True),
        ({"APP_OBJECT_STORE_BACKEND": "sqlite"}, True),
        ({"APP_OBJECT_STORE_BACKEND": "staged"}, False),
        ({"APP_OBJECT_STORE_BACKEND": "staged", "APP_PERSIST_DERIVED_ARTIFACTS": "true"}, True),
        ({"APP_PERSIST_DERIVED_ARTIFACTS": "false"}, False),
    ],
)
def test_the_switch_defaults_by_backend_and_an_explicit_value_wins(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: bool
) -> None:
    monkeypatch.delenv("APP_OBJECT_STORE_BACKEND", raising=False)
    monkeypatch.delenv("APP_PERSIST_DERIVED_ARTIFACTS", raising=False)
    _env(monkeypatch, **env)
    assert persist_derived_default(Settings()) is expected


# ---- 默认:逐字节不变 -------------------------------------------------------------------


def test_default_writes_the_derived_bytes_and_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    _env(monkeypatch, APP_OBJECT_STORE_BACKEND="sqlite")
    result = _run(tmp_path)

    digest, _, requests = store_digest(tmp_path / "ingestion")
    assert (digest, requests) == (FULL_STORE_DIGEST, FULL_REQUESTS_DIGEST)
    _, outputs, manifest = _published(_document_root(result))
    for page in manifest.pages:
        for record in page.objects:
            for stage in record.stages:
                if stage.stage in DERIVED_STAGES and stage.artifact is not None:
                    assert not recomputable(outputs.assets, stage.artifact)
                    assert outputs.assets.backend.get_content(stage.artifact.sha256) is not None


def test_every_derived_stage_recomputes_to_its_stored_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消费者盘点的实测:每个落盘的派生产物都能从页 SVG + 对象几何逐字节重算(两次)。"""
    lite_env(monkeypatch)
    _env(monkeypatch, APP_OBJECT_STORE_BACKEND="sqlite")
    sources, outputs, manifest = _published(_document_root(_run(tmp_path)))
    stages = _derived_stages(manifest)
    assert {stage for _, _, stage, _ in stages} == DERIVED_STAGES
    for _, object_id, name, position in stages:
        page = manifest.pages[position]
        (stage,) = (
            stage
            for record in page.objects
            if record.object_id == object_id
            for stage in record.stages
            if stage.stage == name
        )
        assert stage.artifact is not None
        stored = outputs.assets.get(stage.artifact)
        for _ in range(2):
            recomputed = recompute_object_stage(
                sources, outputs.assets, manifest.scope, page, object_id, name, stage.artifact
            )
            assert recomputed == stored, (object_id, name)


# ---- 不落盘 -----------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "files"])
def test_not_persisted_writes_no_derived_bytes_and_publishes_the_same_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    tasks = lite_env(monkeypatch)
    _env(monkeypatch, APP_OBJECT_STORE_BACKEND=backend, APP_PERSIST_DERIVED_ARTIFACTS="false")
    result = _run(tmp_path)
    (document,) = result.documents

    assert document.status == "published" and document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    assert tasks == FULL_TASKS
    digest, _, requests = store_digest(tmp_path / "ingestion")
    assert requests == FULL_REQUESTS_DIGEST and digest != FULL_STORE_DIGEST
    _, outputs, manifest = _published(_document_root(result))
    stages = _derived_stages(manifest)
    assert stages
    for page in manifest.pages:
        for record in page.objects:
            for stage in record.stages:
                if stage.stage in DERIVED_STAGES and stage.artifact is not None:
                    assert recomputable(outputs.assets, stage.artifact)
                    # 既不是对象,也不是 stage 条目里的内联产物。
                    assert outputs.assets.backend.get_content(stage.artifact.sha256) is None

    # 重跑:零模型调用、同一发布 id、无修复。
    tasks.clear()
    (again,) = run_mode(tmp_path, "full").documents
    assert again.publication is not None
    assert again.publication.published_processing_id == FULL_PUBLISHED_ID
    assert (sum(tasks.values()), again.live_calls, again.storage_repairs) == (0, 0, {})

    # 挂载核对每个成员的证据(含对象 SVG 的重算);图表成员按需重新证明。
    (entry,) = scan_catalog(tmp_path / "ingestion").documents
    mounted = mount_document(entry, embedder=None, verify_every_request=True)
    plan_id = mounted.manifest().retrieval
    assert plan_id is not None
    charts = [member for member in mounted.member_texts() if member.kind is ObjectKind.CHART]
    assert charts
    for member in charts:
        context = mounted.chart_context(
            PinnedRetrievalHit(plan_id.snapshot_id, member.member_id, 1.0)
        )
        assert context.chart.points


def test_full_review_pages_are_byte_identical_without_the_derived_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lite_env(monkeypatch)
    _env(monkeypatch, APP_OBJECT_STORE_BACKEND="sqlite")
    kept = _document_root(_run(tmp_path / "kept"))
    _env(monkeypatch, APP_PERSIST_DERIVED_ARTIFACTS="false")
    dropped = _document_root(_run(tmp_path / "dropped"))
    assert kept.name == dropped.name

    # full 摄入时写的审阅页(runs/<id>/)逐字节相同。
    runs = sorted(path.name for path in (kept / "processing" / "runs").iterdir())
    assert runs == sorted(path.name for path in (dropped / "processing" / "runs").iterdir())
    for run in runs:
        assert _tree(kept / "processing" / "runs" / run) == _tree(
            dropped / "processing" / "runs" / run
        )
    # 按需导出也一样。
    review_kept = export_document_review(kept)
    review_dropped = export_document_review(dropped)
    assert review_kept.read_bytes() == review_dropped.read_bytes()
    assert _tree(review_kept.parent) == _tree(review_dropped.parent)
    assert any(name.endswith("model_render.png") for name in _tree(review_dropped.parent))


# ---- 漂移 / 篡改 ------------------------------------------------------------------------


def test_a_recomputation_off_the_recorded_digest_is_refused_with_a_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lite_env(monkeypatch)
    _env(monkeypatch, APP_OBJECT_STORE_BACKEND="sqlite", APP_PERSIST_DERIVED_ARTIFACTS="false")
    root = _document_root(_run(tmp_path))
    sources, outputs, manifest = _published(root)
    (page_index, object_id, name, position) = next(
        item for item in _derived_stages(manifest) if item[2] == "native_crop"
    )
    page = manifest.pages[position]
    (stage,) = (
        stage
        for record in page.objects
        if record.object_id == object_id
        for stage in record.stages
        if stage.stage == name
    )
    assert object_stage_bytes(sources, outputs.assets, manifest.scope, page, stage, object_id)

    # 一个"新版本"的裁剪器:多一个空格也不行。
    monkeypatch.setattr(
        derived_artifacts, "crop_native_svg", lambda *a, **k: crop_native_svg(*a, **k) + " "
    )
    with caplog.at_level(logging.INFO), pytest.raises(DerivedArtifactDrift):
        object_stage_bytes(sources, outputs.assets, manifest.scope, page, stage, object_id)
    assert any(
        getattr(record, "event", None) == "derived_artifact_drift" for record in caplog.records
    )
    with pytest.raises(DerivedArtifactDrift):
        export_document_review(root)
    assert page_index == page.page_index


# ---- staged ----------------------------------------------------------------------------


@pytest.fixture
def staged_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    staging = tmp_path / "staging"
    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", "staged")
    monkeypatch.setenv("APP_OBJECT_STORE_STAGING_DIR", str(staging))
    get_settings.cache_clear()
    clear_probe_cache()
    yield staging
    staged.release_staged(tmp_path)
    get_settings.cache_clear()


def test_staged_drops_the_derived_bytes_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staged_backend: Path
) -> None:
    lite_env(monkeypatch)
    monkeypatch.delenv("APP_PERSIST_DERIVED_ARTIFACTS", raising=False)
    result = _run(tmp_path)
    (document,) = result.documents
    assert document.publication is not None
    assert document.publication.published_processing_id == FULL_PUBLISHED_ID
    digest, _, requests = store_digest(tmp_path / "ingestion")
    assert requests == FULL_REQUESTS_DIGEST and digest != FULL_STORE_DIGEST

    staged.release_staged(tmp_path)
    _env(monkeypatch, APP_OBJECT_STORE_BACKEND="staged")
    _, outputs, manifest = _published(_document_root(result))
    assert all(
        outputs.assets.backend.get_content(stage.artifact.sha256) is None
        for page in manifest.pages
        for record in page.objects
        for stage in record.stages
        if stage.stage in DERIVED_STAGES and stage.artifact is not None
    )
