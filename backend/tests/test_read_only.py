"""A Catalyst Center user with OBSERVER-ROLE: Steward assesses, every change is refused and nothing changes."""
from app.api.sources import source_out
from app.models import WorkItem
from tests.conftest import cc_client, setup_task, truth


def test_observer_account_refuses_changes(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "device_role_correction")
    ctx._client = cc_client(mock_cc, "observer")
    ctx._client.connect()

    result = defn.analyze(ctx)
    assert result.proposals, "a read-only account still gets the full assessment"

    before = truth(mock_cc)["roles"]
    props = result.proposals[:3]
    items = [WorkItem(id=7000 + n, task_key=defn.key, target_key=p.target_key, target_label=p.target_label,
                      action_type=p.action_type, payload=p.payload, status="executing") for n, p in enumerate(props)]
    res = defn.execute(ctx, items)
    assert res and all(not r.success for r in res.values())
    assert all("refused the change: the account has no write permission" in r.detail for r in res.values()), \
        [r.detail for r in res.values()]
    assert truth(mock_cc)["roles"] == before

    ctx.close()
    state = ctx.source.capabilities
    assert state["write_refused_at"]
    assert (state["ops"].get("set_device_role") or {}).get("candidates", {}).get("set_role_brief_v1") != "unsupported"
    assert source_out(ctx.source)["write_refused_at"] == state["write_refused_at"]

    ctx._client = cc_client(mock_cc)
    ctx._client._write_refused_at = state["write_refused_at"]
    ctx._client.connect()
    ok = defn.execute(ctx, [WorkItem(id=7100, task_key=defn.key, target_key=props[0].target_key,
                                     target_label=props[0].target_label, action_type=props[0].action_type,
                                     payload=props[0].payload, status="executing")])
    assert all(r.success for r in ok.values()), [r.detail for r in ok.values()]
    ctx.close()
    assert ctx.source.capabilities["write_refused_at"] is None, "a successful change clears the refusal"
    db.close()
