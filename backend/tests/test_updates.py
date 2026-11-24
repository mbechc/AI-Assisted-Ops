import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

RELEASES = [
    {"tag_name": "v0.3.0-dev.2", "prerelease": True, "draft": False, "body": "dev 2"},
    {"tag_name": "v0.2.1", "prerelease": False, "draft": False, "body": "fixes"},
    {"tag_name": "v0.3.0-dev.10", "prerelease": True, "draft": False, "body": "dev 10"},
    {"tag_name": "v9.9.9", "prerelease": False, "draft": True},
    {"tag_name": "nightly", "prerelease": True, "draft": False},
]


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def upd(tmp_path, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "update_dir", str(tmp_path))
    monkeypatch.setattr(s, "steward_version", "0.2.0")
    monkeypatch.setattr(s, "steward_connectivity_interval_min", 15)
    (tmp_path / "requests").mkdir()
    return tmp_path


def write(d, name, data):
    (d / name).write_text(json.dumps(data))


def connected(d, minutes_ago=1, ok=True):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    write(d, "connectivity.json", {"checked_at": iso(t), "status": "connected" if ok else "airgapped",
                                   "endpoints": [{"host": "api.github.com", "ok": True},
                                                 {"host": "ghcr.io", "ok": ok, "error": None if ok else "timeout"}]})


def test_semantic_order():
    from app.updates import version_gt
    assert version_gt("0.2.1", "0.2.1-dev.3")
    assert version_gt("0.2.1-dev.10", "0.2.1-dev.9")
    assert version_gt("0.2.1-dev.1", "0.2.0")
    assert not version_gt("0.2.0", "0.2.0")
    assert version_gt("2026.11.0-dev.1", "0.2.5-dev.8"), "calendar versions follow 0.x.y"
    assert version_gt("2026.11.0", "2026.11.0-dev.12")
    assert version_gt("2026.12.0-dev.1", "2026.11.3")
    assert version_gt("2027.1.0", "2026.12.4")
    assert version_gt("0.10.0", "0.9.9")


def test_tracks():
    from app.updates import available_update, releases_for
    assert [r["version"] for r in releases_for("production", RELEASES)] == ["0.2.1"]
    assert [r["version"] for r in releases_for("development", RELEASES)] == ["0.3.0-dev.10", "0.3.0-dev.2", "0.2.1"]
    assert available_update("0.2.0", "production", RELEASES)["version"] == "0.2.1"
    assert available_update("0.2.0", "development", RELEASES)["version"] == "0.3.0-dev.10"
    # Back on Production after a pre-release: nothing older is offered, the final release is.
    assert available_update("0.3.0-dev.10", "production", RELEASES) is None
    assert available_update("0.2.1-dev.1", "production", RELEASES)["version"] == "0.2.1"


def test_connectivity_states(upd):
    from app.updates import connectivity
    assert connectivity()["status"] == "unknown"
    connected(upd)
    assert connectivity()["status"] == "connected"
    connected(upd, ok=False)
    c = connectivity()
    assert c["status"] == "airgapped" and c["failed"] == ["ghcr.io"]
    connected(upd, minutes_ago=45)
    assert connectivity()["status"] == "unknown"


def test_scheduler_queues_without_duplicates(upd):
    from app.updates import pending, schedule_host_checks
    assert schedule_host_checks() == ["check"]
    assert schedule_host_checks() == []
    for f in os.listdir(upd / "requests"):
        os.remove(upd / "requests" / f)
    write(upd, "releases-meta.json", {"checked_at": iso(datetime.now(timezone.utc)), "ok": True})
    connected(upd, minutes_ago=20)
    assert schedule_host_checks() == ["connectivity"]
    assert [p["action"] for p in pending()] == ["connectivity"]


def test_queue_rejects_unsafe_names(upd):
    from app.updates import queue
    for action, arg in (("install", "1.0.0;rm -rf /"), ("install", "../x"), ("channel", "beta"), ("shell", None)):
        with pytest.raises(ValueError):
            queue(action, arg)
    assert os.listdir(upd / "requests") == []


def test_api_roles_and_install(upd):
    from app.db import Base, SessionLocal, engine
    from app.deps import current_user
    from app.main import app
    from app.models import AuditLog, User

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add_all([User(username="vic", password_hash="x", role="viewer"),
                    User(username="olga", password_hash="x", role="operator"),
                    User(username="ada", password_hash="x", role="admin")])
        db.commit()

    def as_user(name):
        def dep():
            with SessionLocal() as db:
                return db.query(User).filter_by(username=name).one()
        app.dependency_overrides[current_user] = dep

    write(upd, "releases.json", RELEASES)
    write(upd, "status.json", {"channel": "production", "updater_version": 1})
    c = TestClient(app, headers={"X-Steward": "1"})
    try:
        for who in ("vic", "olga"):
            as_user(who)
            st = c.get("/api/updates").json()
            assert st["can_manage"] is False and st["channel"] == "production"
            assert st["available"]["version"] == "0.2.1"
            assert c.get("/api/system/connectivity").json()["status"] == "unknown"
            assert c.post("/api/updates/refresh").status_code == 403
            assert c.put("/api/updates/channel", json={"channel": "development"}).status_code == 403
            assert c.post("/api/updates/install", json={"version": "0.2.1"}).status_code == 403
            assert c.get("/api/updates/log").status_code == 403

        as_user("ada")
        assert c.get("/api/updates").json()["can_manage"] is True
        # Not connected: install refused.
        assert c.post("/api/updates/install", json={"version": "0.2.1"}).status_code == 409
        connected(upd)
        assert c.post("/api/updates/install", json={"version": "0.3.0-dev.10"}).status_code == 409
        r = c.post("/api/updates/install", json={"version": "0.2.1"})
        assert r.status_code == 200 and r.json()["install_running"] is True
        assert any(n.endswith("-install-0.2.1") for n in os.listdir(upd / "requests"))
        assert c.post("/api/updates/install", json={"version": "0.2.1"}).status_code == 409
        assert c.put("/api/updates/channel", json={"channel": "development"}).status_code == 409

        for f in os.listdir(upd / "requests"):
            os.remove(upd / "requests" / f)
        r = c.put("/api/updates/channel", json={"channel": "development"})
        assert r.status_code == 200 and r.json()["channel_pending"] == "development"
        assert c.put("/api/updates/channel", json={"channel": "beta"}).status_code == 422
        with SessionLocal() as db:
            actions = [a.action for a in db.query(AuditLog).all()]
        assert actions == ["update.install", "update.channel"]
    finally:
        app.dependency_overrides.clear()
