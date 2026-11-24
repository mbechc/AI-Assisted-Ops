from fastapi.testclient import TestClient


def _setup():
    from app.db import Base, SessionLocal, engine
    from app.models import DataSource, TaskConfig, User, WorkItem
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    db.add_all([User(username="ada", password_hash="x", role="admin"),
                User(username="olga", password_hash="x", role="operator"),
                User(username="vic", password_hash="x", role="viewer")])
    a = DataSource(name="CatC A", base_url="https://a", username="u", secret_enc="x")
    b = DataSource(name="CatC B", base_url="https://b", username="u", secret_enc="x")
    db.add_all([a, b, TaskConfig(task_key="ap_hostname_reset", cron="0 * * * *", auto_threshold=90),
                TaskConfig(task_key="ap_site_assignment", cron="0 * * * *", auto_threshold=95)])
    db.flush()
    rows = [  # source, task, group, confidence, status, blocked
        (a, "ap_hostname_reset", "CPH-B01", 97, "proposed", None),
        (a, "ap_hostname_reset", "CPH-B01", 60, "proposed", None),
        (a, "ap_hostname_reset", "AAR_B02", 99, "proposed", "pick a floor"),
        (a, "ap_site_assignment", "CPH-B01", 96, "done", None),
        (b, "ap_hostname_reset", "CPH-B01", 98, "proposed", None),
        (b, "ap_site_assignment", "ODE-B01", 50, "proposed", None),
    ]
    for n, (src, task, group, conf, status, blocked) in enumerate(rows):
        db.add(WorkItem(task_key=task, source_id=src.id, target_key=f"t{n}", target_label=f"AP{n}",
                        group_label=group, action_type="rename_ap", confidence=conf, status=status,
                        blocked_reason=blocked))
    db.commit()
    return db, a.id, b.id


def _client(name):
    from app.db import SessionLocal
    from app.deps import current_user
    from app.main import app
    from app.models import User

    def dep():
        with SessionLocal() as db:
            return db.query(User).filter_by(username=name).one()
    app.dependency_overrides[current_user] = dep
    return app, TestClient(app, headers={"X-Steward": "1"})


def test_filters_combine_and_facets_count_other_filters():
    db, a, b = _setup()
    app, c = _client("vic")
    try:
        def total(params):
            return c.get("/api/items", params=params).json()["total"]
        assert total({}) == 6
        assert total({"status": "proposed"}) == 5
        assert total({"source_id": [a, b], "status": "proposed"}) == 5
        assert total({"source_id": a, "status": "proposed"}) == 3
        assert total({"source_id_not": a}) == 2
        assert total({"task_key": "ap_hostname_reset", "group_not": "CPH-B01"}) == 1
        assert total({"group_contains": "cph", "min_confidence": 90}) == 3
        assert total({"target_contains": "ap1"}) == 1
        assert total({"status": "approved,done"}) == 1
        assert total({"blocked": "false", "status_not": "done"}) == 4
        assert total({"group_contains": "%"}) == 0

        f = c.get("/api/review/facets", params={"source_id": a, "status": "proposed"}).json()
        assert {s["name"]: s["count"] for s in f["sources"]} == {"CatC A": 3, "CatC B": 2}
        assert {t["value"]: t["count"] for t in f["tasks"]} == {"ap_hostname_reset": 3}
        assert {s["value"]: s["count"] for s in f["statuses"]} == {"proposed": 3, "done": 1}
        assert {g["value"]: g["count"] for g in f["groups"]} == {"CPH-B01": 2, "AAR_B02": 1}

        g = c.get("/api/items/groups", params={"status": "proposed"}).json()
        rows = {(r["task_key"], r["source_id"], r["group"]): r for r in g["groups"]}
        assert rows[("ap_hostname_reset", a, "CPH-B01")]["count"] == 2
        assert rows[("ap_hostname_reset", a, "CPH-B01")]["above_threshold"] == 1
        assert rows[("ap_hostname_reset", b, "CPH-B01")]["threshold"] == 90
        assert rows[("ap_site_assignment", b, "ODE-B01")]["above_threshold"] == 0
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_approve_all_matching_respects_the_catalyst_center_filter(monkeypatch):
    from app.api import tasks
    from app.models import WorkItem
    monkeypatch.setattr(tasks.dispatch_task, "delay", lambda *a, **k: None)
    db, a, b = _setup()
    app, c = _client("olga")
    try:
        r = c.post("/api/items/decide", json={"decision": "approve", "filter": {
            "source_id": [a], "task_key": ["ap_hostname_reset"], "group": ["CPH-B01"], "min_confidence": 90,
            "status": ["done"]}})
        assert r.status_code == 200
        db.expire_all()
        approved = {i.target_label for i in db.query(WorkItem).filter_by(status="approved")}
        assert approved == {"AP0"}  # not AP4 (CatC B), AP1 (60), AP2 (blocked) or the done item
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_saved_filters_personal_shared_and_delete_rights():
    db, a, b = _setup()
    try:
        app, c = _client("olga")
        mine = c.post("/api/review/saved-filters", json={"name": "CatC A resets", "query": f"source={a}"}).json()
        shared = c.post("/api/review/saved-filters",
                        json={"name": "Team view", "query": "status=proposed", "shared": True}).json()
        again = c.post("/api/review/saved-filters", json={"name": "catc a resets", "query": "source=2"}).json()
        assert again["id"] == mine["id"]
        assert [e["name"] for e in c.get("/api/review/saved-filters").json()] == ["catc a resets", "Team view"]

        app, c = _client("vic")
        seen = c.get("/api/review/saved-filters").json()
        assert [(e["name"], e["mine"], e["can_delete"]) for e in seen] == [("Team view", False, False)]
        assert c.delete(f"/api/review/saved-filters/{shared['id']}").status_code == 403
        assert c.delete(f"/api/review/saved-filters/{mine['id']}").status_code == 404

        app, c = _client("ada")
        assert c.delete(f"/api/review/saved-filters/{shared['id']}").status_code == 200
        app, c = _client("olga")
        assert [e["name"] for e in c.get("/api/review/saved-filters").json()] == ["catc a resets"]
    finally:
        app.dependency_overrides.clear()
        db.close()
