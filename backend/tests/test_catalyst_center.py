"""Contract tests: identical normalized results across Catalyst Center releases."""
from app.connectors.catalyst_center import CapabilityUnavailable
from tests.conftest import cc_client


def test_release_detection(mock_cc):
    cc = cc_client(mock_cc)
    version = cc.connect()
    assert ".".join(map(str, version)) == mock_cc.RELEASE
    expected = "dnac-packages" if mock_cc.V3 else "dnac-release"
    assert cc.version_source.startswith(expected)


def test_inventory_and_sites_normalized(mock_cc):
    cc = cc_client(mock_cc)
    cc.connect()
    sites = cc.get_sites()
    assert {s.type for s in sites} == {"global", "area", "building", "floor"}
    assert all(s.hierarchy.startswith("Global") for s in sites)
    devices = cc.get_devices()
    aps = [d for d in devices if d.is_ap]
    assert len(aps) > 500 and all(a.eth_mac for a in aps)
    assignments = cc.get_site_assignments(sites)
    unassigned = [a for a in aps if a.id not in assignments]
    assert 0.1 < len(unassigned) / len(aps) < 0.3
    assert len(cc.get_links()) > 0


def test_capability_selection_per_release(mock_cc):
    cc = cc_client(mock_cc)
    cc.connect()
    sites = cc.get_sites()
    cc.get_site_assignments([s for s in sites if s.type == "floor"][:2])
    state = cc.capability_state()["ops"]
    if mock_cc.V3:
        assert state["get_sites"]["selected"] == "sites_v1"
        assert state["get_site_assignments"]["selected"] == "assigned_to_site_v1"
    else:
        assert state["get_sites"]["selected"] == "site_legacy"
        assert state["get_site_assignments"]["selected"] == "membership_legacy"


def test_rf_neighbors_only_where_exposed(mock_cc):
    cc = cc_client(mock_cc)
    cc.connect()
    ap = next(d for d in cc.get_devices() if d.is_ap)
    if mock_cc.V3:
        assert cc.get_ap_rf_neighbors(ap)
    else:
        try:
            cc.get_ap_rf_neighbors(ap)
            assert False, "expected CapabilityUnavailable"
        except CapabilityUnavailable:
            pass
    cfg = cc.get_ap_config(ap)
    assert cfg is not None and cfg.site_tag


def test_assign_and_verify(mock_cc):
    cc = cc_client(mock_cc)
    cc.connect()
    sites = cc.get_sites()
    devices = cc.get_devices()
    assignments = cc.get_site_assignments(sites)
    ap = next(d for d in devices if d.is_ap and d.id not in assignments)
    floor = next(s for s in sites if s.type == "floor")
    handle = cc.assign_devices_to_site(floor, [ap])
    assert handle.kind == ("task" if mock_cc.V3 else "execution")
    result = cc.wait_for(handle, timeout=30, interval=0)
    assert result.success, result.detail
    assert cc.get_site_assignments([floor]).get(ap.id) == floor.id


def test_specialist_operations(mock_cc):
    cc = cc_client(mock_cc)
    cc.connect()
    devices = {d.id: d for d in cc.get_devices()}
    ap = next(d for d in devices.values() if d.is_ap)
    assert cc.wait_for(cc.rename_aps([(ap, "RENAMED-AP01")]), timeout=30, interval=0).success
    assert mock_cc.DEVICES[ap.id]["hostname"] == "RENAMED-AP01"

    sw = next(d for d in devices.values() if d.family == "Switches and Hubs")
    assert cc.wait_for(cc.set_device_role(sw, "CORE"), timeout=30, interval=0).success
    assert mock_cc.DEVICES[sw.id]["role"] == "CORE"

    stale = next(iter(k for k, v in mock_cc.estate.telemetry_truth.items() if v == "sync_stale"))
    assert cc.wait_for(cc.sync_devices([stale]), timeout=30, interval=0).success
    assert stale not in mock_cc.estate.telemetry_truth

    health = cc.get_device_health()
    assert len(health) == len(devices)
    broken = next(k for k, v in mock_cc.estate.telemetry_truth.items() if v in ("credential", "snmp"))
    assert health[broken]["health"] is None

    tag = "STEWARD-EXCLUDED"
    assert cc.list_excluded(tag) == {}
    assert cc.wait_all(cc.exclude_devices([sw.id], 30, "test", tag), timeout=30).success
    excluded = cc.list_excluded(tag)
    assert excluded[sw.id]["mechanism"] == ("maintenance" if mock_cc.V3 else "tag")
    assert cc.wait_all(cc.include_devices({sw.id: excluded[sw.id]}, tag), timeout=30).success
    assert sw.id not in cc.list_excluded(tag)
    state = cc.capability_state()["ops"]
    assert state["get_device_health"]["selected"] == ("assurance_network_devices" if mock_cc.V3 else "device_health_v1")
    assert state["rename_aps"]["selected"] == ("ap_config_v2" if mock_cc.V3 else "ap_config_v1")


def test_ap_to_building_is_rejected(mock_cc):
    cc = cc_client(mock_cc)
    cc.connect()
    sites = cc.get_sites()
    ap = next(d for d in cc.get_devices() if d.is_ap)
    building = next(s for s in sites if s.type == "building")
    result = cc.wait_for(cc.assign_devices_to_site(building, [ap]), timeout=30, interval=0)
    assert not result.success and "floor" in result.detail
