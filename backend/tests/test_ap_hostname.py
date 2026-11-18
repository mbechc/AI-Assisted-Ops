from fastapi.testclient import TestClient

from app.inventory import collect
from app.models import WorkItem
from tests.conftest import setup_task, truth


def test_hostname_reset_history_and_convention(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "ap_hostname_reset")
    collect(db, ctx.source.id, ctx.cc)  # original names
    TestClient(mock_cc.app).post("/mock/reset_names")
    collect(db, ctx.source.id, ctx.cc)  # some APs reverted to factory names
    result = defn.analyze(ctx)
    t = truth(mock_cc)
    name_truth = t["name_truth"]
    resets = set(mock_cc.estate.name_reset)
    props = {p.target_key: p for p in result.proposals}
    assert resets, "mock must reset some AP names"

    hist = [props[a] for a in resets if a in props]
    assert len(hist) == len(resets)
    assert all(p.payload["method"] == "history" and p.rule_confidence >= 90 for p in hist)
    assert all(p.payload["hostname_new"] == name_truth[p.target_key] for p in hist)

    conv = [p for p in props.values() if p.payload["method"] == "convention"]
    assert conv
    unique = [p for p in conv if p.rule_confidence >= 80]
    if unique:
        exact = sum(p.payload["hostname_new"] == name_truth.get(p.target_key) for p in unique)
        assert exact / len(unique) >= 0.9, f"{exact}/{len(unique)}"
    # Every proposed name follows the floor prefix of the intended name and is unique.
    names = [p.payload["hostname_new"] for p in props.values()]
    assert len(names) == len(set(n.lower() for n in names))
    pref_ok = sum(p.payload["hostname_new"].rsplit("-AP", 1)[0] == name_truth[p.target_key].rsplit("-AP", 1)[0]
                  for p in conv if p.target_key in name_truth)
    assert pref_ok / len(conv) >= 0.95

    # Execute the history-based renames and verify through Catalyst Center.
    items = []
    for n, p in enumerate(hist[:5]):
        it = WorkItem(id=1000 + n, task_key=defn.key, target_key=p.target_key, target_label=p.target_label,
                      action_type=p.action_type, payload=p.payload, status="executing")
        items.append(it)
    res = defn.execute(ctx, items)
    assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]
    db.close()
