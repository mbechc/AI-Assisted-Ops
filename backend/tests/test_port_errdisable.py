from app.connectors.catalyst_center.models import Interface
from app.usecases.ap_uplink import port_key
from app.usecases.port_errdisable import port_state
from tests.conftest import setup_task, truth


def test_port_state():
    def intf(status, admin="UP"):
        return Interface(device_id="d", name="Gi1/0/1", status=status, admin_status=admin)

    assert port_state(intf("up")) == "up"
    assert port_state(intf("down")) == "down"
    assert port_state(intf("err-disabled")) == "err"
    assert port_state(intf("down", "DOWN")) == "admin_down"


def test_repeat_errdisabled_ports(mock_cc):
    db, defn, cfg, ctx = setup_task(mock_cc, "repeat_errdisabled_ports")
    t = truth(mock_cc)
    repeat = {f"{x['switch']}:{port_key(x['port'])}" for x in t["errdis_truth"]}
    once = {f"{x['switch']}:{port_key(x['port'])}" for x in t["errdis_once"]}
    assert repeat and once
    down_switches = set(t["transient_down"]) | set(t["expected_down"])
    want = {k for k in repeat if k.split(":")[0] not in down_switches}

    # The mock takes repeat ports down on every second read; three times down needs six runs.
    for n in range(5):
        assert not defn.analyze(ctx).proposals, f"nothing is repeated yet after run {n + 1}"
    result = defn.analyze(ctx)
    props = {p.target_key: p for p in result.proposals}
    assert want <= set(props) <= repeat, (set(props) - repeat, want - set(props))
    assert not set(props) & once, "a port that went down once is not reported"
    for p in props.values():
        assert p.payload["count"] == 3 and p.payload["neighbour_family"] == "Unified AP"
        assert p.action_type == "report" and p.blocked_reason
    assert result.stats["ports"] > 500
    db.close()
