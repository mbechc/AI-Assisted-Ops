from app.models import WorkItem
from tests.conftest import setup_task, truth


def _items(defn, props, start=6000):
    return [WorkItem(id=start + n, task_key=defn.key, target_key=p.target_key, action_type=p.action_type,
                     payload=p.payload, status="executing") for n, p in enumerate(props)]


def test_unsaved_configuration(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "unsaved_configuration")
    result = defn.analyze(ctx)
    t = truth(mock_cc)
    want = {d for d, days in t["unsaved_truth"].items() if days >= 3}
    assert want and any(days == 0 for days in t["unsaved_truth"].values()), "mock needs old and recent changes"
    props = {p.target_key: p for p in result.proposals}
    unreachable = set(t["transient_down"]) | set(t["flapping"])
    assert want - unreachable <= set(props) <= want, (set(props) - want, want - unreachable - set(props))
    assert result.stats["recent"] > 0, "changes from the last hours are left alone"

    can_write = mock_cc.VER >= (2, 3, 7)
    assert {p.action_type for p in props.values()} == {"write_memory" if can_write else "report"}
    assert all(bool(p.blocked_reason) != can_write for p in props.values())

    sample = list(props.values())[:3]
    res = defn.execute(ctx, _items(defn, sample))
    if can_write:
        assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]
        assert not {s.target_key for s in sample} & set(truth(mock_cc)["unsaved_truth"])
        again = {p.target_key for p in defn.analyze(ctx).proposals}
        assert not again & {s.target_key for s in sample}
    else:
        assert all(not r.success for r in res.values())
    db.close()
