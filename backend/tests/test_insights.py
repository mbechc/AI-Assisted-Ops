"""Reviewer-feedback insights: counts and hints without AI; AI summary only on request, validated and cached."""
from datetime import datetime, timezone

from app.framework import insights as ins
from app.models import WorkItem
from tests.test_ai_modes import _client, update_dir  # noqa: F401

TASK = "ap_site_assignment"


def _ev(signal, agrees=True):
    return {"signal": signal, "label": signal.title(), "available": True, "weight": 0.3, "agrees": agrees}


def _seed(db):
    now = datetime.now(timezone.utc)
    for n in range(16):
        bad = n < 10
        db.add(WorkItem(task_key=TASK, target_key=f"ap{n}", target_label=f"AP{n}", action_type="assign_site",
                        group_label="CPH-B01" if n < 12 else "AAR-B02", payload={}, evidence=[_ev("name"), _ev("rf")]
                        if bad else [_ev("rf")], status="rejected" if bad else "done", decided_by="op",
                        decided_at=now, decision_note="name prefix belongs to the old building" if bad else None))
    db.add(WorkItem(task_key=TASK, target_key="auto", target_label="auto", action_type="assign_site", payload={},
                    status="done", decided_by="auto", decided_at=now))
    db.commit()


class FakeLLM:
    name, label = "local", "Local AI"
    calls = 0

    def chat_json(self, system, user, schema, num_ctx=4096):
        FakeLLM.calls += 1
        assert "name prefix belongs to the old building" in user
        return {"summary": "Name prefixes are stale after moves.",
                "themes": [{"theme": "Stale prefixes", "count": 6, "example": "old building"}],
                "suggestions": [{"param": "weight_name", "value": "0.1", "why": "names mislead"},
                                {"param": "rm -rf", "value": "1", "why": "x"},
                                {"param": "name_min_samples", "value": "not a number", "why": "x"}]}


def test_rule_based_insights(update_dir):
    c, db = _client("viewer")
    _seed(db)
    d = c.get(f"/api/tasks/{TASK}/insights").json()
    assert d["decided"] == 16 and d["rejected"] == 10 and d["ai_mode"] == "none" and d["ai"] is None
    name = next(s for s in d["signals"] if s["name"] == "name")
    assert name["bad_rate"] == 1.0
    assert any("weight_name" in h for h in d["hints"])
    assert any("CPH-B01" in h for h in d["hints"])
    assert len(d["notes"]) == 10
    assert c.post(f"/api/tasks/{TASK}/insights/ai").status_code == 403
    db.close()


def test_ai_summary_requires_ai(update_dir):
    c, db = _client("admin")
    _seed(db)
    r = c.post(f"/api/tasks/{TASK}/insights/ai")
    assert r.status_code == 400 and "AI is off" in r.json()["detail"]
    db.close()


def test_ai_summary_validated_and_cached(update_dir, monkeypatch):
    import app.api.tasks as tasks_api
    c, db = _client("admin")
    _seed(db)
    monkeypatch.setattr(tasks_api, "get_provider", lambda db: FakeLLM())
    monkeypatch.setattr(tasks_api, "effective_mode", lambda db: "local")
    FakeLLM.calls = 0
    d = c.post(f"/api/tasks/{TASK}/insights/ai").json()
    assert d["ai"]["summary"].startswith("Name prefixes")
    assert [s["param"] for s in d["ai"]["suggestions"]] == ["weight_name"], "unknown or invalid params dropped"
    assert d["ai"]["suggestions"][0]["value"] == 0.1
    again = c.get(f"/api/tasks/{TASK}/insights").json()
    assert again["ai"]["summary"] == d["ai"]["summary"]
    c.post(f"/api/tasks/{TASK}/insights/ai")
    assert FakeLLM.calls == 1, "one AI summary per task per day"
    db.close()


def test_outcome_mapping():
    assert ins._outcome("rejected", None, None) == "rejected"
    assert ins._outcome("done", {"x": 1}, None) == "overridden"
    assert ins._outcome("superseded", None, {"matched": True}) == "approved"
    assert ins._outcome("superseded", None, {}) is None
