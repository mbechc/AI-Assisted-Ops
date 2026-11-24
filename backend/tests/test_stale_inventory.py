from app.models import TaskConfig, WorkItem
from app.usecases.expected_down import EXCLUSION_TAG
from tests.conftest import setup_task, truth


def test_stale_inventory_proposes_long_unreachable_devices(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "stale_inventory")
    t = truth(mock_cc)
    stale = set(t["stale"])
    assert len(stale) >= 5, "mock needs devices unreachable for months"
    props = {p.target_key: p for p in defn.analyze(ctx).proposals}
    assert set(props) == stale, set(props) ^ stale
    assert not set(t["transient_down"]) & set(props), "briefly-down devices are not stale"
    assert {p.action_type for p in props.values()} == {"delete_device"}
    assert defn.max_autonomy == 1, "deleting is never automatic"

    # Excluded on purpose (maintenance mode or tag) or on an expected-down prefix: kept.
    kept = sorted(stale)[:2]
    ctx.cc.wait_all(ctx.cc.exclude_devices(kept, 30, "test", EXCLUSION_TAG))
    ed = db.get(TaskConfig, "expected_down_devices")
    prefix_dev = next(p for d, p in props.items() if d not in kept)
    ed.params = {**ed.params, "expected_down_hostname_prefixes": prefix_dev.payload["hostname"]}
    db.flush()
    again = {p.target_key: p for p in defn.analyze(ctx).proposals}
    assert set(again) == stale - set(kept) - {prefix_dev.target_key}

    victim = next(iter(again.values()))
    item = WorkItem(id=7000, task_key=defn.key, target_key=victim.target_key, action_type="delete_device",
                    payload=victim.payload, status="executing")
    res = defn.execute(ctx, [item])[7000]
    assert res.success and res.verified, res.detail
    assert victim.target_key not in truth(mock_cc)["hostnames"]
    assert victim.target_key not in {p.target_key for p in defn.analyze(ctx).proposals}
    db.close()
