"""/studio 静态站点挂载测试：显式目录优先，否则使用 wheel 内置静态产物。"""

from pathlib import Path

from fastapi.testclient import TestClient

from ragspine.agent.llm_provider import MockProvider
from ragspine.service import config as service_config
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import FakeQueue


def make_client(tmp_path, *, studio_dir=None):
    kwargs = {} if studio_dir is None else {"studio_dir": studio_dir}
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"), **kwargs)
    app = create_app(
        config, provider=MockProvider(), queue=FakeQueue(), faq_cache=FAQCache.empty()
    )
    return TestClient(app)


def test_studio_serves_index_when_configured(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<html><body>studio-ok</body></html>", encoding="utf-8"
    )
    client = make_client(tmp_path, studio_dir=str(dist))
    resp = client.get("/studio/")
    assert resp.status_code == 200
    assert "studio-ok" in resp.text


def test_studio_default_targets_packaged_assets(tmp_path):
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))
    expected = Path(service_config.__file__).resolve().with_name("studio_dist")

    assert Path(config.studio_dir) == expected


def test_studio_not_mounted_when_packaged_assets_missing(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/studio/").status_code == 404


def test_studio_not_mounted_when_explicit_dir_missing(tmp_path):
    client = make_client(tmp_path, studio_dir=str(tmp_path / "no_such_dir"))
    assert client.get("/studio/").status_code == 404


def test_studio_can_be_explicitly_disabled(tmp_path):
    client = make_client(tmp_path, studio_dir="")
    assert client.get("/studio/").status_code == 404
