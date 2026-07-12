"""/studio 静态站点挂载测试：studio_dir 非空且目录存在才挂载，否则静默不挂（404）。"""

import os

import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import FakeQueue


def make_client(tmp_path, *, studio_dir=""):
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"), studio_dir=studio_dir)
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


def test_studio_not_mounted_by_default(tmp_path):
    client = make_client(tmp_path)  # studio_dir=""（默认不启用）
    assert client.get("/studio/").status_code == 404


def test_studio_not_mounted_when_dir_missing(tmp_path):
    client = make_client(tmp_path, studio_dir=str(tmp_path / "no_such_dir"))
    assert client.get("/studio/").status_code == 404
