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


class _Provider:
    def __init__(self, name, fail=False):
        self.name, self.label, self.fail, self.calls = name, f"{name} AI", fail, 0

    def review(self, system, prompt):
        self.calls += 1
        if self.fail:
            from app.llm.base import LLMUnavailable
            raise LLMUnavailable("quota")
        return {"confidence": 70, "rationale": "ok", "concerns": []}


def _proposals(n=3):
    from app.framework.base import Proposal
    return [Proposal(target_key=f"t{i}", target_label=f"t{i}", action_type="report", payload={},
                     rule_confidence=60, evidence=[], rationale="", llm_prompt="evidence", fingerprint=f"f{i}")
            for i in range(n)]


@pytest.mark.parametrize("mode", ["none", "local", "cloud"])
def test_llm_review_per_mode(mode):
    defn = type("D", (), {"llm_system_prompt": "sys"})()
    ctx = type("C", (), {"params": {}, "log": lambda self, m: None})()
    ctx.llm = None if mode == "none" else _Provider(mode)
    res = runner._llm_review(ctx, defn, _proposals(), {})
    if mode == "none":
        assert {r["status"] for r in res.values()} == {"off"}
        return
    assert {r["status"] for r in res.values()} == {"ok"} and ctx.llm_stats["llm_provider"] == mode
    prev = {"t0": WorkItem(target_key="t0", llm_status="ok", fingerprint="f0", llm_provider=mode, llm_confidence=70,
                           rationale="r", concerns=[])}
    other = "cloud" if mode == "local" else "local"
    ctx.llm = _Provider(mode)
    assert runner._llm_review(ctx, defn, _proposals(), prev)["t0"].get("reused")
    ctx.llm = _Provider(other)
    assert not runner._llm_review(ctx, defn, _proposals(), prev)["t0"].get("reused"), "reuse needs the same provider"


def test_run_errors_are_readable():
    from sqlalchemy.exc import IntegrityError
    from app.connectors.catalyst_center.http import CCError
    assert "Database conflict" in runner.friendly_error(IntegrityError("INSERT", {}, Exception("dup")))
    assert runner.friendly_error(CCError(503, "busy", "/x")) == "Catalyst Center request failed (HTTP 503): busy"
    assert runner.friendly_error(RuntimeError("no enabled catalyst_center data source configured\nmore")) == \
        "no enabled catalyst_center data source configured"
    old = ('Traceback (most recent call last):\n  File "/usr/local/lib/x.py", line 1\nsqlalchemy.exc.IntegrityError: '
           '(psycopg.errors.UniqueViolation) duplicate key\n')
    assert runner.display_error(old).startswith("Database error")
    assert runner.display_error("plain message") == "plain message"


def test_llm_unavailable_falls_back_to_rules():
    defn = type("D", (), {"llm_system_prompt": "sys"})()
    ctx = type("C", (), {"params": {}, "log": lambda self, m: None})()
    ctx.llm = _Provider("cloud", fail=True)
    res = runner._llm_review(ctx, defn, _proposals(), {})
    assert {r["status"] for r in res.values()} == {"unavailable"} and ctx.llm.calls == 1


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
