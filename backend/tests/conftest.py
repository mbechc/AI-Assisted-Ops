import importlib
import os
import sys

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL_OVERRIDE", "sqlite://")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MOCK_CC_APS", "900")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RELEASES = ["2.3.5.6", "3.1.3.0"]


def load_mock(release: str):
    os.environ["MOCK_CC_RELEASE"] = release
    os.environ["MOCK_CC_SEED"] = release.replace(".", "")[:3]
    import mockcc.app
    return importlib.reload(mockcc.app)


@pytest.fixture(params=RELEASES)
def mock_cc(request):
    return load_mock(request.param)


def setup_task(mock_module, key: str, **params):
    """Fresh DB, a mock CC source and a RunContext for the given task (LLM off)."""
    from app.db import Base, SessionLocal, engine
    from app.framework import registry
    from app.framework.base import RunContext
    from app.framework.runner import ensure_task_configs
    from app.models import DataSource, TaskConfig
    from app.security import encrypt_secret

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    ensure_task_configs(db)
    src = DataSource(name="mock", base_url="http://mockcc", username="admin", secret_enc=encrypt_secret("mock"))
    db.add(src)
    db.flush()
    defn = registry.get(key)
    cfg = db.get(TaskConfig, defn.key)
    cfg.params = {**cfg.params, "llm_enabled": False, **params}
    ctx = RunContext(db, defn.key, src, dict(cfg.params))
    ctx._client = cc_client(mock_module)
    ctx._client.connect()
    return db, defn, cfg, ctx


def truth(mock_module) -> dict:
    from fastapi.testclient import TestClient
    return TestClient(mock_module.app).get("/mock/truth").json()


def cc_client(mock_module):
    from fastapi.testclient import TestClient
    from app.connectors.catalyst_center import CatalystCenter
    cc = CatalystCenter("http://mockcc", "admin", "mock", rate_limit_rps=0)
    cc.http._client.close()
    cc.http._client = TestClient(mock_module.app, base_url="http://mockcc")
    return cc
