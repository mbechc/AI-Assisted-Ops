import os
import struct

from fastapi.testclient import TestClient

from app import dockerapi
from app.connectors.geoip.writer import write_mmdb
from app.db import Base, SessionLocal, engine
from app.deps import current_user
from app.framework import registry
from app.models import User


def _frame(kind: int, text: str) -> bytes:
    b = text.encode()
    return bytes([kind, 0, 0, 0]) + struct.pack(">I", len(b)) + b


def test_log_frames_and_levels():
    data = _frame(1, "2026-01-01T00:00:00Z a INFO x\n") + _frame(2, "2026-01-01T00:00:01Z b ERROR boom\n")
    chunks = [data[:5], data[5:20], data[20:]]
    out = list(dockerapi._frames(iter(chunks)))
    assert [k for k, _ in out] == [1, 2]
    assert out[1][1].endswith(b"ERROR boom\n")
    assert dockerapi.level_of("2026 INFO [worker-x] app: hi") == "info"
    assert dockerapi.level_of("WARNING [api] x") == "warn"
    assert dockerapi.level_of("CRITICAL boom") == "error"


def _client(role="admin"):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    u = User(username="t", password_hash="x", role=role)
    db.add(u)
    db.commit()
    from app.main import app
    registry.load_all()
    app.dependency_overrides[current_user] = lambda: db.get(User, u.id)
    return TestClient(app, headers={"X-Steward": "1"}), db


def test_runners_without_docker_or_redis(monkeypatch):
    monkeypatch.setenv("LOGPROXY_URL", "http://127.0.0.1:9")
    from app.config import get_settings
    get_settings.cache_clear()
    c, db = _client()
    r = c.get("/api/runners")
    assert r.status_code == 200, r.text
    body = r.json()
    names = {w["name"] for w in body["workers"]}
    assert names == {"default", "wireless", "inventory", "health"}
    assert body["docker_error"]
    sched = {s["task_key"]: s for s in body["schedule"]}
    assert {"ap_site_assignment", "site_reassignment", "ap_hostname_reset", "site_hierarchy_drift",
            "expected_down_devices", "telemetry_baseline", "device_role_correction",
            "cc_inventory_snapshot"} <= set(sched)
    assert sched["cc_inventory_snapshot"]["next"], "collector runs every 2 hours"
    wireless = next(w for w in body["workers"] if w["name"] == "wireless")
    assert "ap_hostname_reset" in wireless["tasks"]
    assert c.get("/api/admin/logs/services").status_code == 503
    db.close()


def test_logs_admin_only():
    c, db = _client(role="operator")
    assert c.get("/api/admin/logs/stream?services=api").status_code == 403
    db.close()


def test_geoip_upload_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOIP_DIR", str(tmp_path))
    from app.config import get_settings
    get_settings.cache_clear()
    c, db = _client()
    src = c.post("/api/sources", json={"name": "geo", "kind": "geoip"}).json()
    p = str(tmp_path / "x.mmdb")
    write_mmdb(p, [("10.0.0.0/24", {"site": "HQ"})])
    r = c.post(f"/api/sources/{src['id']}/upload?filename=x.mmdb", content=open(p, "rb").read(),
               headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True
    bad = c.post(f"/api/sources/{src['id']}/upload?filename=bad.mmdb", content=b"not a database",
                 headers={"Content-Type": "application/octet-stream"})
    assert bad.status_code == 400
    state = c.get(f"/api/sources/{src['id']}/geoip").json()
    assert len(state["files"]) == 1 and state["learning"]["available"] is False
    t = c.post(f"/api/sources/{src['id']}/test").json()
    assert t["status"] == "ok"
    assert not [f for f in os.listdir(os.path.join(str(tmp_path), str(src["id"]))) if f.endswith(".upload")]
    db.close()
