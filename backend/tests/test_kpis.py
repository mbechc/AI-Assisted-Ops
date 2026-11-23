from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def test_review_kpis_per_category():
    from app.db import Base, SessionLocal, engine
    from app.deps import current_user
    from app.main import app
    from app.models import WorkItem

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    with SessionLocal() as db:
        def item(status, created=now, **kw):
            db.add(WorkItem(task_key="ap_hostname_reset", target_key=f"t{len(db.new)}", target_label="x",
                            action_type="rename_ap", status=status, created_at=created, **kw))
        recent = now - timedelta(hours=2)
        item("proposed")
        item("proposed", old)
        item("done", old, executed_at=recent, decided_at=recent)
        item("done", old, executed_at=old, decided_at=old)
        item("superseded", old, decided_at=recent, outcome={"actual_site_id": "x"})
        item("superseded", old, decided_at=recent, outcome={"removed_from_inventory": True})
        db.add(WorkItem(task_key="site_hierarchy_drift", target_key="a", target_label="a", action_type="report",
                        status="reported", created_at=now))
        db.commit()

    app.dependency_overrides[current_user] = lambda: None
    try:
        rows = {r["key"]: r for r in TestClient(app).get("/api/items/kpis", headers={"X-AIOps": "1"}).json()}
    finally:
        app.dependency_overrides.clear()
    assert "cc_inventory_snapshot" not in rows
    h = rows["ap_hostname_reset"]
    assert (h["open"], h["new_24h"], h["executed_24h"], h["resolved_24h"], h["fixed_24h"]) == (2, 1, 1, 1, 2)
    # 24 h ago: the old proposal, the later-executed item and both later-superseded items were open.
    assert (h["open_24h_ago"], h["change_24h"]) == (4, -2)
    assert len(h["history"]) == 30 * 4 + 1 and h["history"][-1] == 2
    assert h["history"][0] == 0, "nothing existed a month ago"
    assert rows["device_role_correction"]["history"] == [0] * (30 * 4 + 1)
    d = rows["site_hierarchy_drift"]
    assert (d["open"], d["reported"], d["new_24h"]) == (1, 1, 1)


def test_dashboard_ranking_by_relative_movement():
    from app.api.tasks import KPI_TILES, _rank

    def row(name, now, day_ago, week_ago=None):
        return {"key": name, "name": name, "open": now, "open_24h_ago": day_ago,
                "open_7d_ago": day_ago if week_ago is None else week_ago}

    rows = _rank([
        row("big_backlog_flat", 900, 900),
        row("doubled", 40, 20),             # +100% in 24 h
        row("tiny_noise", 2, 1),            # +100% but only one item: noise
        row("halved", 50, 100),             # -50% in 24 h
        row("slow_week", 130, 128, 100),    # +30% in 7 days, weighted half
        row("new", 12, 0),
        row("cleared", 0, 8),
        row("quiet_a", 0, 0), row("quiet_b", 0, 0),
        row("small_backlog", 4, 4),
    ])
    order = [r["key"] for r in rows]
    by = {r["key"]: r for r in rows}
    assert order[:5] == ["new", "doubled", "cleared", "halved", "slow_week"]  # ties: more open first
    assert by["new"]["reason"] == "New: 12 open in 24 h"
    assert by["cleared"]["reason"] == "Cleared: all 8 closed in 24 h"
    assert by["doubled"]["reason"] == "Growing 100% in 24 h"
    assert by["halved"]["reason"] == "Declining 50% in 24 h"
    assert by["slow_week"]["reason"] == "Growing 30% in 7 days"
    assert by["tiny_noise"]["score"] == 0
    assert by["big_backlog_flat"]["reason"] == "Open backlog"
    featured = [r["key"] for r in rows if r["featured"]]
    assert len(featured) == KPI_TILES and featured[5] == "big_backlog_flat"
    assert not by["quiet_a"]["featured"] and by["quiet_a"]["reason"] == "Quiet"
