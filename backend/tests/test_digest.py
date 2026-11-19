"""Shift summary and owner reports: template always, AI wording only when it passes the fact checks."""
from datetime import datetime, timedelta, timezone

from app.framework import digest
from app.models import TaskRun, WorkItem
from tests.test_ai_modes import _client, update_dir  # noqa: F401


def _seed(db):
    now = datetime.now(timezone.utc)
    for n in range(3):
        db.add(WorkItem(task_key="telemetry_baseline", target_key=f"d{n}", target_label=f"sw{n} (10.0.0.{n})",
                        action_type="report", group_label="Credential failure", status="proposed",
                        payload={"class": "credential", "hostname": f"sw{n}", "site": "Global/DK/CPH/CPH-B01",
                                 "error_description": "Authentication failed"}))
    db.add(WorkItem(task_key="telemetry_baseline", target_key="s1", target_label="r1", action_type="resync_device",
                    group_label="Stale sync", status="done", decided_by="auto", decided_at=now, executed_at=now))
    db.add(WorkItem(task_key="ap_site_assignment", target_key="old", target_label="old", action_type="assign_site",
                    status="done", decided_by="op", decided_at=now - timedelta(days=3),
                    executed_at=now - timedelta(days=3), created_at=now - timedelta(days=3)))
    db.add(TaskRun(task_key="expected_down_devices", status="failed", error="Catalyst Center timeout"))
    db.commit()


class FakeLLM:
    name, label = "cloud", "Cloud AI"

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def chat_json(self, system, user, schema, num_ctx=4096):
        self.calls += 1
        return self.answer


def test_shift_template_without_ai(update_dir):
    c, db = _client("viewer")
    _seed(db)
    d = c.get("/api/digest/shift?hours=12").json()
    assert d["ai_mode"] == "none" and d["ai"] is None
    text = " ".join(d["lines"])
    assert "1 task run(s) failed" in text and "Catalyst Center timeout" in text
    assert "1 executed automatically" in text
    tel = next(t for t in d["tasks"] if t["key"] == "telemetry_baseline")
    assert tel["new"] == 4 and tel["open"] == 3
    assert not any(t["key"] == "ap_site_assignment" and t["done"] for t in d["tasks"]), "outside the window"
    assert c.get("/api/digest/shift?hours=5").status_code == 400
    assert c.post("/api/digest/shift/ai", json={"hours": 12}).status_code == 403
    db.close()


def test_shift_ai_numbers_checked(update_dir, monkeypatch):
    import app.api.digest as api
    c, db = _client("operator")
    _seed(db)
    good = FakeLLM({"narrative": "One run failed (timeout). 3 credential failures are open; 1 resync ran."})
    monkeypatch.setattr(api, "get_provider", lambda db: good)
    d = c.post("/api/digest/shift/ai", json={"hours": 12}).json()
    assert d["ai"]["narrative"].startswith("One run failed")
    assert c.get("/api/digest/shift?hours=12").json()["ai"]["narrative"], "cached for the same facts"

    bad = FakeLLM({"narrative": "477 devices are down."})
    monkeypatch.setattr(api, "get_provider", lambda db: bad)
    d = c.post("/api/digest/shift/ai", json={"hours": 24}).json()
    assert d["ai"]["narrative"] is None and "numbers not in the facts" in d["ai"]["rejected_reason"]
    assert d["lines"], "template still shown"
    db.close()


def test_shift_ai_off(update_dir):
    c, db = _client("operator")
    r = c.post("/api/digest/shift/ai", json={"hours": 12})
    assert r.status_code == 400 and "AI is off" in r.json()["detail"]
    db.close()


def test_owner_reports(update_dir, monkeypatch):
    import app.api.digest as api
    c, db = _client("operator")
    _seed(db)
    g = c.get("/api/digest/owner-reports").json()["groups"]
    assert len(g) == 1 and g[0]["class"] == "credential" and g[0]["count"] == 3
    assert all(f"sw{n}" in g[0]["body"] for n in range(3)) and "Authentication failed (3)" in g[0]["body"]

    partial = FakeLLM({"title": "Fix creds", "body": "Devices sw0 and sw1 need new credentials."})
    monkeypatch.setattr(api, "get_provider", lambda db: partial)
    r = c.post("/api/digest/owner-reports/ai", json={"cls": "credential"}).json()
    assert r["ai"]["body"] is None and "left out 1 device" in r["ai"]["rejected_reason"]
    assert c.post("/api/digest/owner-reports/ai", json={"cls": "snmp"}).status_code == 404
    db.close()


def test_owner_report_ai_accepted(update_dir, monkeypatch):
    import app.api.digest as api
    c, db = _client("operator")
    _seed(db)
    full = FakeLLM({"title": "Credential failure on 3 switches", "body": "sw0, sw1 and sw2: update credentials."})
    monkeypatch.setattr(api, "get_provider", lambda db: full)
    r = c.post("/api/digest/owner-reports/ai", json={"cls": "credential"}).json()
    assert r["ai"]["title"] == "Credential failure on 3 switches"
    assert c.get("/api/digest/owner-reports").json()["groups"][0]["ai"]["body"]
    db.close()


def test_numbers_check():
    assert digest._numbers_ok("3 open, 12 hours", {"a": 3, "hours": 12})
    assert not digest._numbers_ok("40 open", {"a": 3})
