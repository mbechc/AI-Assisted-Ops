import pytest
from sqlalchemy import select

from app.framework import runner
from app.models import TaskRun, WorkItem
from tests.conftest import setup_task


@pytest.mark.parametrize("mock_cc", ["2.3.5.6"], indirect=True)
def test_long_fingerprints_fit_the_column(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "site_hierarchy_drift")
    run = TaskRun(task_key=defn.key, status="running", source_id=ctx.source.id)
    db.add(run)
    db.flush()
    result = defn.analyze(ctx)
    assert any(len(p.fingerprint or "") > 64 for p in result.proposals)
    runner._store_results(db, ctx, defn, cfg, run, result)
    items = db.scalars(select(WorkItem)).all()
    assert items and all(len(i.fingerprint or "") <= 64 for i in items)
    again = runner._store_results(db, ctx, defn, cfg, run, defn.analyze(ctx))
    assert again["items_created"] == 0
    db.close()


@pytest.mark.parametrize("mock_cc", ["2.3.5.6"], indirect=True)
def test_worker_start_fails_only_its_orphaned_runs(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "site_hierarchy_drift")
    mine = TaskRun(task_key="site_hierarchy_drift", status="running")
    other = TaskRun(task_key="telemetry_baseline", status="running")
    db.add_all([mine, other])
    db.commit()
    assert runner.fail_orphaned_runs("inventory") == 1
    db.expire_all()
    assert db.get(TaskRun, mine.id).status == "failed"
    assert db.get(TaskRun, other.id).status == "running"
    db.close()


@pytest.mark.parametrize("mock_cc", ["2.3.5.6"], indirect=True)
def test_failed_save_marks_run_failed(mock_cc, monkeypatch):
    db, defn, cfg, ctx = setup_task(mock_cc, "site_hierarchy_drift")
    cfg.source_id = ctx.source.id
    run = TaskRun(task_key=defn.key, status="queued", source_id=ctx.source.id)
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    def broken(db, *a, **kw):
        db.add(WorkItem(task_key=None, target_key="x", target_label="x", action_type="report"))
        return {}

    monkeypatch.setattr(runner, "_store_results", broken)
    monkeypatch.setattr(runner, "dispatch_approved", lambda *a: 0)
    monkeypatch.setattr(runner, "resolve_source", lambda db, d, c: ctx.source and db.get(type(ctx.source), ctx.source.id))
    monkeypatch.setattr(type(defn), "analyze", lambda self, c: type("R", (), {"stats": {}, "proposals": [], "resolved": {}})())
    runner.execute_run(run_id)

    from app.db import SessionLocal
    with SessionLocal() as s:
        r = s.get(TaskRun, run_id)
        assert r.status == "failed" and r.finished_at is not None
