import json

import httpx
import pytest

from app.db import Base, SessionLocal, engine
from app.llm import azure_openai
from app.llm.azure_openai import AzureOpenAIClient, strict_schema
from app.llm.base import REVIEW_SCHEMA, LLMUnavailable
from app.llm.factory import effective_mode, get_provider
from app.store import get_setting, set_setting


@pytest.fixture
def update_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UPDATE_DIR", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    (tmp_path / "requests").mkdir()
    yield tmp_path
    get_settings.cache_clear()


def _db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return SessionLocal()


def _llm_json(d, state):
    (d / "llm.json").write_text(json.dumps({"state": state, "model": "qwen2.5:7b-instruct"}))


def test_default_is_no_ai(update_dir):
    db = _db()
    assert get_setting(db, "ai_mode") == "none"
    assert effective_mode(db) == "none" and get_provider(db) is None
    db.close()


def test_local_only_effective_when_model_ready(update_dir):
    db = _db()
    set_setting(db, "ai_mode", "local")
    assert effective_mode(db) == "none", "nothing installed yet"
    _llm_json(update_dir, "pulling")
    assert effective_mode(db) == "none"
    _llm_json(update_dir, "ready")
    assert effective_mode(db) == "local"
    assert get_provider(db).name == "local"
    db.close()


def test_cloud_provider_from_settings(update_dir):
    from app.security import encrypt_secret
    db = _db()
    set_setting(db, "ai_mode", "cloud")
    set_setting(db, "ai_cloud", {"endpoint": "https://x.openai.azure.com", "deployment": "gpt-4o-mini",
                                 "auth": "api_key", "api_key_enc": encrypt_secret("k")})
    p = get_provider(db)
    assert p.name == "cloud" and p.configured and p.api_key == "k"
    db.close()


def test_strict_schema_for_azure():
    s = strict_schema(REVIEW_SCHEMA)
    assert s["additionalProperties"] is False
    assert "minimum" not in s["properties"]["confidence"]
    assert s["required"] == ["confidence", "rationale", "concerns"]
    assert "minimum" in REVIEW_SCHEMA["properties"]["confidence"], "original schema untouched"


def _azure(monkeypatch, status=200, content=None):
    seen = {}

    def post(url, params=None, json=None, headers=None, timeout=None, data=None):
        seen.update(url=url, params=params, json=json, headers=headers)
        body = {"choices": [{"message": {"content": content or '{"confidence": 140, "rationale": " r ", '
                                                              '"concerns": ["c"]}'}}]}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(azure_openai.httpx, "post", post)
    return seen


def test_azure_review_request_and_normalisation(monkeypatch):
    seen = _azure(monkeypatch)
    c = AzureOpenAIClient("https://x.openai.azure.com/", "dep", api_key="secret")
    out = c.review("sys", "user")
    assert out == {"confidence": 100, "rationale": "r", "concerns": ["c"]}
    assert seen["url"] == "https://x.openai.azure.com/openai/deployments/dep/chat/completions"
    assert seen["headers"] == {"api-key": "secret"}
    assert seen["json"]["response_format"]["type"] == "json_schema"
    assert seen["params"]["api-version"]


@pytest.mark.parametrize("status", [401, 404, 429])
def test_azure_errors_become_unavailable(monkeypatch, status):
    _azure(monkeypatch, status=status)
    with pytest.raises(LLMUnavailable):
        AzureOpenAIClient("https://x.openai.azure.com", "dep", api_key="k").chat_json("s", "u", REVIEW_SCHEMA)


def test_azure_not_configured():
    c = AzureOpenAIClient("http://insecure", "dep", api_key="k")
    assert not c.configured
    with pytest.raises(LLMUnavailable):
        c.chat_json("s", "u", REVIEW_SCHEMA)


def _client(role="admin"):
    from fastapi.testclient import TestClient
    from app.deps import current_user
    from app.framework import registry
    from app.main import app
    from app.models import User
    db = _db()
    u = User(username="t", password_hash="x", role=role)
    db.add(u)
    db.commit()
    registry.load_all()
    app.dependency_overrides[current_user] = lambda: db.get(User, u.id)
    return TestClient(app, headers={"X-Steward": "1"}), db


def test_ai_settings_api_local_lifecycle(update_dir):
    c, db = _client()
    st = c.get("/api/settings/ai").json()
    assert st["mode"] == "none" and st["local"]["state"] == "absent"
    st = c.put("/api/settings/ai", json={"mode": "local"}).json()
    assert st["mode"] == "local" and st["effective_mode"] == "none"
    assert st["local"]["state"] == "queued"
    reqs = sorted(p.name.split("-", 1)[1] for p in (update_dir / "requests").iterdir())
    assert reqs == ["llm-enable"]
    for p in (update_dir / "requests").iterdir():
        p.unlink()
    _llm_json(update_dir, "ready")
    assert c.get("/api/settings/ai").json()["effective_mode"] == "local"
    c.put("/api/settings/ai", json={"mode": "none"})
    assert [p.name.split("-", 1)[1] for p in (update_dir / "requests").iterdir()] == ["llm-disable"]
    db.close()


def test_ai_settings_api_cloud_secrets_never_returned(update_dir):
    c, db = _client()
    assert c.put("/api/settings/ai", json={"mode": "cloud"}).status_code == 400
    r = c.put("/api/settings/ai", json={"mode": "cloud", "cloud": {
        "endpoint": "https://x.openai.azure.com/", "deployment": "gpt-4o-mini", "api_key": "topsecret"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effective_mode"] == "cloud" and body["cloud"]["api_key_set"] is True
    assert "topsecret" not in r.text and "api_key_enc" not in r.text
    assert body["cloud"]["endpoint"] == "https://x.openai.azure.com"
    from app.models import AuditLog
    entry = db.query(AuditLog).filter(AuditLog.action == "settings.ai_updated").all()[-1]
    assert "topsecret" not in json.dumps(entry.details) and "api_key" in entry.details["cloud_fields_changed"]
    db.close()


def test_ai_settings_admin_only(update_dir):
    c, db = _client(role="operator")
    assert c.get("/api/settings/ai").status_code == 200
    assert c.put("/api/settings/ai", json={"mode": "local"}).status_code == 403
    assert c.post("/api/settings/ai/test", json={}).status_code == 403
    db.close()


def test_ai_test_endpoint_reports_errors(update_dir, monkeypatch):
    _azure(monkeypatch, status=401)
    c, db = _client()
    r = c.post("/api/settings/ai/test", json={"mode": "cloud", "cloud": {
        "endpoint": "https://x.openai.azure.com", "deployment": "d", "api_key": "k"}}).json()
    assert r["ok"] is False and "credentials" in r["error"]
    db.close()


def test_updates_queue_accepts_llm_actions(update_dir):
    from app import updates
    for a in updates.LLM_ACTIONS:
        updates.queue(a)
    with pytest.raises(ValueError):
        updates.queue("llm-enable", "x")
    assert updates.llm_state()["state"] == "queued"
