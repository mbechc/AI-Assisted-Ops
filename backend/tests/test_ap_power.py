from app.connectors.catalyst_center.models import PoePort
from app.models import WorkItem
from app.usecases.ap_power import REQUIRED_W, classify, required_watts, watts_table
from tests.conftest import setup_task, truth


def test_required_watts_by_model_prefix():
    assert required_watts("C9130AXI-E", REQUIRED_W) == 45
    assert required_watts("CW9166I-ROW", REQUIRED_W) == 45
    assert required_watts("AIR-AP2802I-E-K9", REQUIRED_W) == 30
    assert required_watts("C9130AXI-E, C9130AXI-E", REQUIRED_W) == 45
    assert required_watts("Unknown-AP", REQUIRED_W) is None
    table = watts_table("CW9130=30; C9999=12, broken, C9105=x")
    assert required_watts("C9130AXI-E", table) == 30 and required_watts("C9999I", table) == 12
    assert table["C9105"] == REQUIRED_W["C9105"]


def test_classify():
    def port(alloc, status="ON"):
        return PoePort(device_id="d", name="Gi1/0/1", oper_status=status, allocated_w=alloc)

    assert classify(port(30.0), 45) == "derated"
    assert classify(port(44.8), 45) is None
    assert classify(port(45.0), 45) is None
    assert classify(port(0.0, "OFF"), 45) is None
    assert classify(port(None, "POWER_DENY"), 45) == "denied"
    assert classify(port(0.0, "faulty"), 15.4) == "denied"


def test_ap_power_report_matches_truth(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "ap_power_shortfall")
    result = defn.analyze(ctx)
    t = set(truth(mock_cc)["power_truth"])
    assert t, "mock must contain APs on too little power"
    props = {p.target_key: p for p in result.proposals}
    assert set(props) == t, set(props) ^ t
    for p in props.values():
        assert p.payload["class"] == "derated" and p.payload["allocated_w"] == 30 and p.payload["required_w"] == 45
        assert p.action_type == "report" and p.blocked_reason and p.group_label == p.payload["switch"]
    assert result.stats["ports"] > 500 and not result.stats.get("port_not_found")
    items = [WorkItem(id=5000 + n, task_key=defn.key, target_key=p.target_key, action_type="report",
                      payload=p.payload, status="executing") for n, p in enumerate(result.proposals[:2])]
    assert all(not r.success for r in defn.execute(ctx, items).values())
    db.close()
