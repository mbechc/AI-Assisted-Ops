from app.models import WorkItem
from tests.conftest import setup_task, truth


def test_telemetry_classification_and_resync(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "telemetry_baseline")
    result = defn.analyze(ctx)
    t = truth(mock_cc)["telemetry_truth"]
    props = {p.target_key: p for p in result.proposals}
    assert set(props) == set(t), (set(props) ^ set(t))
    assert all(props[d].payload["class"] == cls for d, cls in t.items())
    assert all(props[d].action_type == ("resync_device" if cls == "sync_stale" else "report") for d, cls in t.items())
    assert all(props[d].blocked_reason for d, cls in t.items() if cls != "sync_stale")

    stale = [props[d] for d, cls in t.items() if cls == "sync_stale"][:6]
    items = [WorkItem(id=3000 + n, task_key=defn.key, target_key=p.target_key, target_label=p.target_label,
                      action_type=p.action_type, payload=p.payload, status="executing") for n, p in enumerate(stale)]
    res = defn.execute(ctx, items)
    assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]
    db.close()
