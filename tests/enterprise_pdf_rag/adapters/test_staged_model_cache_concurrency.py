"""ADR 0046:文档自己的模型缓存 staged 之后,claim / 单飞 / 预算 / 429 共享冷却的语义不变。

ADR 0042(一个 client 的并发调用)与 ADR 0045(文档内页级并发)的回归用例原样再跑一遍,
只是模型缓存换成随文档 store 发布的 ``StagedModelCacheBackend``。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from ragspine.common.evidence.configs import Settings, get_settings
from ragspine.common.evidence.object_backend import staged
from ragspine.common.evidence.object_backend.probe import clear_probe_cache
from ragspine.common.evidence.object_backend.registry import open_backend
from tests.enterprise_pdf_rag.adapters.test_ingest_concurrency import (
    test_a_rate_limit_cooldown_is_shared_by_concurrent_calls as _cooldown_shared,
)
from tests.enterprise_pdf_rag.adapters.test_ingest_concurrency import (
    test_concurrent_calls_never_overspend_the_budget as _budget_never_overspent,
)
from tests.enterprise_pdf_rag.adapters.test_ingest_concurrency import (
    test_concurrent_calls_of_one_client_overlap_on_the_network as _calls_overlap,
)
from tests.enterprise_pdf_rag.adapters.test_ingest_concurrency import (
    test_concurrent_calls_of_one_fingerprint_send_it_once as _single_flight,
)
from tests.enterprise_pdf_rag.adapters.test_page_concurrency import (
    test_a_rate_limit_cooldown_holds_back_every_page_of_every_document as _pages_cooldown,
)
from tests.enterprise_pdf_rag.adapters.test_page_concurrency import (
    test_a_tight_budget_is_never_overspent_by_pages_at_once as _pages_budget,
)
from tests.enterprise_pdf_rag.adapters.test_page_concurrency import (
    test_an_interrupt_with_pages_at_once_stops_at_page_boundaries_and_leaves_no_claim as _pages_interrupt,
)
from tests.enterprise_pdf_rag.adapters.test_page_concurrency import (
    test_documents_and_pages_at_once_write_exactly_what_the_serial_run_writes as _pages_bytes,
)


@pytest.fixture
def staged_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    staging = tmp_path / "staging"
    monkeypatch.setenv("APP_OBJECT_STORE_BACKEND", "staged")
    monkeypatch.setenv("APP_OBJECT_STORE_STAGING_DIR", str(staging))
    get_settings.cache_clear()
    clear_probe_cache()
    yield staging
    staged.release_staged(tmp_path)
    get_settings.cache_clear()


@pytest.fixture
def document_cache(tmp_path: Path, staged_env: Path) -> Iterator[Path]:
    """一份文档的 processing store 已在 staged(注册表里),它的 ``model-cache`` 目录。"""
    processing = tmp_path / "doc" / "processing"
    store = open_backend(processing, settings=Settings())
    yield processing / "model-cache"
    store.close()


def _assert_staged_and_published(cache_dir: Path, tmp_path: Path) -> None:
    assert any(
        isinstance(backend, staged.StagedModelCacheBackend) for backend in staged._REGISTRY.values()
    )
    staged.release_staged(tmp_path / "doc")
    assert (cache_dir / "model-cache.sqlite").is_file()
    assert not (cache_dir / "requests").exists()  # 没有走文件布局


@pytest.mark.parametrize(
    "case", [_calls_overlap, _single_flight, _budget_never_overspent], ids=lambda c: c.__name__
)
def test_one_client_on_a_staged_document_cache(
    case: object, document_cache: Path, tmp_path: Path
) -> None:
    case(document_cache)  # type: ignore[operator]
    _assert_staged_and_published(document_cache, tmp_path)


def test_a_rate_limit_cooldown_is_shared_on_a_staged_document_cache(
    document_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cooldown_shared(document_cache, monkeypatch)
    _assert_staged_and_published(document_cache, tmp_path)


@pytest.mark.usefixtures("staged_env")
def test_pages_at_once_on_staged_stores_write_what_the_serial_run_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pages_bytes(tmp_path, monkeypatch)
    assert any((tmp_path / "both").rglob("model-cache.sqlite"))


@pytest.mark.usefixtures("staged_env")
def test_a_tight_budget_on_staged_stores_is_never_overspent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pages_budget(tmp_path, monkeypatch)


@pytest.mark.usefixtures("staged_env")
def test_a_rate_limit_cooldown_holds_back_every_page_on_staged_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pages_cooldown(tmp_path, monkeypatch)


@pytest.mark.usefixtures("staged_env")
def test_an_interrupt_on_staged_stores_leaves_no_claim_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pages_interrupt(tmp_path, monkeypatch)
