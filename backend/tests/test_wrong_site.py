from sqlalchemy import select

from app.framework.runner import _store_results
from app.models import TaskRun, WorkItem
from tests.conftest import setup_task, truth


def test_wrong_site_without_geoip(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "site_reassignment")
    result = defn.analyze(ctx)
    wrong = truth(mock_cc)["wrong_site"]
    assert wrong, "mock estate must contain wrongly placed APs"
    assert result.stats["signals_available"]["geoip"] is False

    props = {p.target_key: p for p in result.proposals}
    real_truth = mock_cc.estate.truth
    hits = [d for d in wrong if d in props]
    recall = len(hits) / len(wrong)
    precision = len(hits) / max(1, len(props))
    assert recall >= 0.8, f"recall {len(hits)}/{len(wrong)}"
    assert precision >= 0.9, f"precision {len(hits)}/{len(props)}: " + ", ".join(
        f"{p.target_label} {p.payload['current_site']} -> {p.payload['site_hierarchy']}"
        for k, p in props.items() if k not in wrong)[:2000]
    unblocked = [props[d] for d in hits if not props[d].blocked_reason]
    exact = sum(p.payload["site_id"] == real_truth[p.target_key] for p in unblocked)
    assert exact / max(1, len(unblocked)) >= 0.95
    for p in props.values():
        ev = {e["signal"]: e for e in p.evidence}
        assert ev["geoip"]["detail"] == "not configured"
        assert p.payload["auto_eligible"] is True
    db.close()


def test_wrong_site_execute_and_resolve(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "site_reassignment")
    run = TaskRun(task_key=defn.key, status="running", source_id=ctx.source.id)
    db.add(run)
    db.flush()
    _store_results(db, ctx, defn, cfg, run, defn.analyze(ctx))
    items = db.scalars(select(WorkItem).where(WorkItem.blocked_reason.is_(None))).all()[:5]
    assert items
    for i in items:
        i.status = "executing"
    res = defn.execute(ctx, items)
    assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]

    ctx.params["snapshot_max_age_hours"] = 0
    again = defn.analyze(ctx)
    fixed = {i.target_key for i in items}
    assert not fixed & {p.target_key for p in again.proposals}
    assert all(again.resolved[t]["actual_site_id"] == mock_cc.estate.truth[t] for t in fixed)
    db.close()
