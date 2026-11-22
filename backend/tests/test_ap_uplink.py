from app.connectors.catalyst_center.models import Interface
from app.models import WorkItem
from app.usecases.ap_uplink import classify, port_key
from tests.conftest import setup_task, truth


def test_port_key_matches_short_and_long_names():
    assert port_key("GigabitEthernet1/0/5") == port_key("Gi1/0/5") == "gi1/0/5"
    assert port_key("TenGigabitEthernet1/1/1") == port_key("Te1/1/1")
    assert port_key("GigabitEthernet1/0/5") != port_key("GigabitEthernet1/0/50")


def test_classify():
    def intf(speed, duplex):
        return Interface(device_id="d", name="Gi1/0/1", speed_mbps=speed, duplex=duplex, status="up")

    assert classify(intf(100, "HalfDuplex"), 1000) == "half_duplex"
    assert classify(intf(1000, "HalfDuplex"), 1000) == "half_duplex"
    assert classify(intf(100, "FullDuplex"), 1000) == "low_speed"
    assert classify(intf(100, "FullDuplex"), 100) is None
    assert classify(intf(1000, "AutoNegotiate"), 1000) is None
    assert classify(intf(None, None), 1000) is None


def test_ap_uplink_report_matches_truth(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "ap_uplink_health")
    result = defn.analyze(ctx)
    t = truth(mock_cc)["uplink_truth"]
    assert t, "mock must contain bad AP uplinks"
    props = {p.target_key: p for p in result.proposals}
    assert set(props) == set(t), (set(props) ^ set(t))
    for ap, cls in t.items():
        p = props[ap]
        assert p.payload["class"] == cls
        assert p.payload["speed_mbps"] == 100
        assert p.action_type == "report" and p.blocked_reason
        assert p.group_label == p.payload["switch"]
    assert result.stats["ports"] > 500
    assert not result.stats.get("port_not_found")

    items = [WorkItem(id=4000 + n, task_key=defn.key, target_key=p.target_key, action_type="report",
                      payload=p.payload, status="executing") for n, p in enumerate(result.proposals[:3])]
    assert all(not r.success for r in defn.execute(ctx, items).values())
    db.close()
