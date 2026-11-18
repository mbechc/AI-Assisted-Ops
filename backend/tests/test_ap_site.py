from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.framework import registry
from app.framework.base import RunContext
from app.framework.runner import _store_results, ensure_task_configs
from app.models import DataSource, TaskConfig, TaskRun, WorkItem
from app.security import encrypt_secret
from app.store import set_setting
from tests.conftest import cc_client


def _setup(mock_module):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    ensure_task_configs(db)
    src = DataSource(name="mock", base_url="http://mockcc", username="admin", secret_enc=encrypt_secret("mock"))
    db.add(src)
    db.flush()
    defn = registry.get("ap_site_assignment")
    cfg = db.get(TaskConfig, defn.key)
    cfg.params = {**cfg.params, "llm_enabled": False}
    ctx = RunContext(db, defn.key, src, dict(cfg.params))
    ctx._client = cc_client(mock_module)
    ctx._client.connect()
    return db, defn, cfg, ctx


def test_proposals_match_ground_truth(mock_cc):
    db, defn, cfg, ctx = _setup(mock_cc)
    result = defn.analyze(ctx)
    truth = mock_cc.estate.truth
    props = result.proposals
    assert len(props) == result.stats["aps_unassigned"]

    def building(fid):
        return mock_cc.SITES[fid]["parentId"]

    high = [p for p in props if p.rule_confidence >= 85 and not p.blocked_reason]
    assert len(high) > 0.5 * len(props), f"only {len(high)} of {len(props)} high-confidence proposals"
    exact = sum(p.payload["site_id"] == truth[p.target_key] for p in high)
    assert exact / len(high) >= 0.97, f"high-confidence floor accuracy {exact}/{len(high)}"

    with_building = [p for p in props if p.payload["building_id"]]
    b_ok = sum(p.payload["building_id"] == building(truth[p.target_key]) for p in with_building)
    assert b_ok / len(with_building) >= 0.97

    if mock_cc.V3:
        assert result.stats["signals_available"]["rf"] is True
    else:
        assert result.stats["signals_available"]["rf"] is False
    db.close()


def test_autonomy_cap_and_auto_execution(mock_cc):
    db, defn, cfg, ctx = _setup(mock_cc)
    run = TaskRun(task_key=defn.key, status="running", source_id=ctx.source.id)
    db.add(run)
    db.flush()

    cfg.autonomy_level, cfg.autonomy_cap = 2, 1  # selected L2 but capped at L1
    stats = _store_results(db, ctx, defn, cfg, run, defn.analyze(ctx))
    assert stats["effective_autonomy"] == 1 and stats["items_auto_approved"] == 0

    cfg.autonomy_cap, cfg.auto_threshold, cfg.max_auto_per_run = 2, 90, 20
    set_setting(db, "global_autonomy_cap", 3)
    stats = _store_results(db, ctx, defn, cfg, run, defn.analyze(ctx))
    assert stats["effective_autonomy"] == 2
    assert stats["items_auto_approved"] == 20
    assert stats["items_created"] == 0  # second run refreshes, never duplicates

    set_setting(db, "global_autonomy_cap", 1)
    stats = _store_results(db, ctx, defn, cfg, run, defn.analyze(ctx))
    assert stats["effective_autonomy"] == 1

    approved = db.scalars(select(WorkItem).where(WorkItem.status == "approved")).all()
    for i in approved:
        i.status = "executing"
    results = defn.execute(ctx, approved)
    assert all(r.success and r.verified for r in results.values()), [r.detail for r in results.values()]
    truth = mock_cc.estate.truth
    assert sum(i.payload["site_id"] == truth[i.target_key] for i in approved) >= 19

    # Executed APs are now assigned: their remaining open items (none) and other runs see them as resolved.
    again = defn.analyze(ctx)
    assert all(p.target_key not in {i.target_key for i in approved} for p in again.proposals)
    db.close()


def test_override_requires_floor(mock_cc):
    db, defn, cfg, ctx = _setup(mock_cc)
    run = TaskRun(task_key=defn.key, status="running", source_id=ctx.source.id)
    db.add(run)
    db.flush()
    _store_results(db, ctx, defn, cfg, run, defn.analyze(ctx))
    item = db.scalars(select(WorkItem)).first()
    building = next(s for s in mock_cc.estate.sites if s["type"] == "building")
    floor = next(s for s in mock_cc.estate.sites if s["type"] == "floor")
    try:
        defn.apply_override(ctx, item, {"site_id": building["id"]})
        assert False
    except ValueError as exc:
        assert "floor" in str(exc)
    out = defn.apply_override(ctx, item, {"site_id": floor["id"]})
    assert out["site_type"] == "floor"
    db.close()
