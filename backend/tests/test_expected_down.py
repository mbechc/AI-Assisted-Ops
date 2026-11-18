from app.inventory import collect
from app.models import WorkItem
from tests.conftest import setup_task, truth


def _items(defn, props, start=2000):
    return [WorkItem(id=start + n, task_key=defn.key, target_key=p.target_key, target_label=p.target_label,
                     action_type=p.action_type, payload=p.payload, status="executing") for n, p in enumerate(props)]


def test_expected_down_and_flapping(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "expected_down_devices")
    for _ in range(5):
        collect(db, ctx.source.id, ctx.cc)
    result = defn.analyze(ctx)
    t = truth(mock_cc)
    expected = set(t["expected_down"]) | set(t["flapping"])
    props = {p.target_key: p for p in result.proposals if p.action_type == "exclude_device"}
    assert set(t["expected_down"]) <= set(props), "all long-down devices must be proposed"
    assert len(set(t["flapping"]) & set(props)) / max(1, len(t["flapping"])) >= 0.9
    assert not set(t["transient_down"]) & set(props), "briefly-down devices must be left alone"
    assert set(props) <= expected, set(props) - expected

    # Execute: maintenance mode on 2.3.7.6+, tag on older releases; then they are not proposed again.
    sample = [props[d] for d in t["expected_down"][:3]]
    res = defn.execute(ctx, _items(defn, sample))
    assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]
    mech = {r.data["mechanism"] for r in res.values()}
    assert mech == ({"maintenance"} if mock_cc.NEW_API else {"tag"})
    again = defn.analyze(ctx)
    assert not {p.target_key for p in again.proposals if p.action_type == "exclude_device"} & {s.target_key for s in sample}
    db.close()


def test_reverse_check_includes_recovered_device(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "expected_down_devices", stable_hours=1)
    collect(db, ctx.source.id, ctx.cc)
    t = truth(mock_cc)
    noisy = set(t["expected_down"]) | set(t["flapping"]) | set(t["transient_down"])
    healthy = next(d["id"] for d in mock_cc.estate.devices
                   if d["id"] not in noisy and d["family"] == "Switches and Hubs" and d["reachabilityStatus"] == "Reachable")
    ctx.cc.wait_all(ctx.cc.exclude_devices([healthy], 30, "manual test", "AIOPS-EXCLUDED"))
    result = defn.analyze(ctx)
    inc = [p for p in result.proposals if p.action_type == "include_device"]
    assert [p.target_key for p in inc] == [healthy]
    res = defn.execute(ctx, _items(defn, inc))
    assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]
    assert healthy not in truth(mock_cc)["excluded"]
    db.close()
