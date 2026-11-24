import os
import subprocess
import sys
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _setup():
    from app.db import Base, SessionLocal, engine
    from app.models import DataSource, TaskRun, User, WorkItem
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    db.add(User(username="ada", password_hash="x", role="admin"))
    db.add(User(username="olga", password_hash="x", role="operator"))
    a = DataSource(name="Real CC", base_url="https://cc", username="u", secret_enc="x")
    b = DataSource(name="Mock CC", base_url="http://mock", username="u", secret_enc="x")
    db.add_all([a, b])
    db.flush()
    for src, status in ((a, "proposed"), (a, "approved"), (a, "done"), (b, "proposed")):
        db.add(WorkItem(task_key="ap_site_assignment", source_id=src.id, target_key=f"{src.id}-{status}",
                        target_label="ap", action_type="assign", status=status))
    db.add(TaskRun(task_key="ap_site_assignment", source_id=a.id, status="success"))
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


def test_delete_source_closes_open_items_and_keeps_history():
    from app.models import AuditLog, DataSource, TaskRun, WorkItem
    db, real, mock = _setup()
    app, c = _client("olga")
    try:
        assert c.delete(f"/api/sources/{real}").status_code == 403
        app, c = _client("ada")
        r = c.delete(f"/api/sources/{real}")
        assert r.status_code == 200 and r.json()["open_items_closed"] == 2
        db.expire_all()
        assert db.get(DataSource, real) is None and db.get(DataSource, mock) is not None
        by_target = {i.target_key: i for i in db.query(WorkItem).all()}
        assert by_target[f"{real}-proposed"].status == "superseded"
        assert by_target[f"{real}-proposed"].outcome == {"source_deleted": True}
        assert by_target[f"{real}-approved"].status == "superseded"
        assert by_target[f"{real}-done"].status == "done"
        assert by_target[f"{mock}-proposed"].status == "proposed"
        assert db.query(TaskRun).count() == 1
        assert [a.action for a in db.query(AuditLog).all()] == ["source.deleted"]
        assert c.delete(f"/api/sources/{real}").status_code == 404
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_delete_refused_while_running_and_queued_runs_cancelled():
    from app.models import TaskRun
    db, real, _ = _setup()
    running = TaskRun(task_key="ap_site_assignment", source_id=real, status="running")
    queued = TaskRun(task_key="wrong_site", source_id=real, status="queued")
    db.add_all([running, queued])
    db.commit()
    app, c = _client("ada")
    try:
        r = c.delete(f"/api/sources/{real}")
        assert r.status_code == 409 and "running run" in r.json()["detail"]
        running.status = "success"
        db.commit()
        r = c.delete(f"/api/sources/{real}")
        assert r.status_code == 200 and r.json()["queued_runs_cancelled"] == 1
        db.expire_all()
        assert db.get(TaskRun, queued.id).status == "failed"
        assert "Real CC deleted" in db.get(TaskRun, queued.id).error
    finally:
        app.dependency_overrides.clear()
        db.close()


def _alembic(url: str, target: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=BACKEND, check=True,
                   capture_output=True, env={**os.environ, "DATABASE_URL_OVERRIDE": url})


def test_autonomy_cap_is_l1_on_fresh_install_and_kept_on_upgrade():
    from app.store import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["global_autonomy_cap"] == 1
    with tempfile.TemporaryDirectory() as tmp:
        fresh = f"sqlite:///{tmp}/fresh.db"
        _alembic(fresh, "head")
        e = create_engine(fresh)
        with e.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM settings")).scalar() == 0
        e.dispose()

        old = f"sqlite:///{tmp}/old.db"
        _alembic(old, "0003")
        e = create_engine(old)
        with e.begin() as conn:
            conn.execute(text("INSERT INTO users (username, password_hash, role, is_active, theme, created_at) "
                              "VALUES ('admin', 'x', 'admin', 1, 'dark', CURRENT_TIMESTAMP)"))
        _alembic(old, "head")
        with e.connect() as conn:
            v = conn.execute(text("SELECT value FROM settings WHERE key = 'global_autonomy_cap'")).scalar()
        e.dispose()
        assert '"v": 3' in v or '"v":3' in v
