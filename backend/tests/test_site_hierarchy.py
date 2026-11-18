from fastapi.testclient import TestClient

from app.inventory import load
from app.usecases.site_hierarchy import approve_baseline
from tests.conftest import setup_task


def test_hierarchy_drift(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "site_hierarchy_drift")
    first = defn.analyze(ctx)
    kinds = {p.payload["kind"] for p in first.proposals}
    assert "baseline" in kinds and "empty_floor" in kinds

    inv = load(db, ctx.source.id)
    approve_baseline(db, ctx.source.id, [s.to_dict() for s in inv.sites], "test")
    assert not [p for p in defn.analyze(ctx).proposals if p.payload["kind"] in ("added", "removed", "renamed", "moved")]

    changed = TestClient(mock_cc.app).post("/mock/drift").json()
    ctx.params["snapshot_max_age_hours"] = 0
    result = defn.analyze(ctx)
    found = {(p.payload["kind"], p.payload["site_id"]) for p in result.proposals}
    for kind, ids in changed.items():
        for sid in ids:
            assert (kind, sid) in found, (kind, sid)
    assert ("building_without_floors", changed["added"][0]) in found
    assert result.stats["renamed"] == 1 and result.stats["moved"] == 1
    assert ("baseline", "none") not in found
    db.close()
