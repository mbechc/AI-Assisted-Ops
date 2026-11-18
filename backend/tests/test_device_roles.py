from app.models import WorkItem
from tests.conftest import setup_task, truth


def test_roles_from_topology(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "device_role_correction")
    result = defn.analyze(ctx)
    t = truth(mock_cc)
    wrong = {d: r for d, r in t["role_truth"].items() if t["roles"][d] != r}
    assert wrong, "mock must contain mis-tagged switches"
    props = {p.target_key: p for p in result.proposals}
    assert set(props) == set(wrong), (len(set(props) - set(wrong)), len(set(wrong) - set(props)))
    assert all(props[d].payload["role"] == r for d, r in wrong.items())

    cores = [props[d] for d, r in wrong.items() if r == "CORE"][:2]
    assert cores and all(p.rule_confidence >= 80 for p in cores)
    items = [WorkItem(id=4000 + n, task_key=defn.key, target_key=p.target_key, target_label=p.target_label,
                      action_type=p.action_type, payload=p.payload, status="executing") for n, p in enumerate(cores)]
    res = defn.execute(ctx, items)
    assert all(r.success and r.verified for r in res.values()), [r.detail for r in res.values()]
    db.close()
