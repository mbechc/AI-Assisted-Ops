"""Deterministic synthetic estate resembling a large customer (default ~6000 APs, ~20% without site)."""
import random
import time
import uuid
from dataclasses import dataclass, field


def _uid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _mac(rng: random.Random) -> str:
    return ":".join(f"{rng.randrange(256):02x}" for _ in range(6))


@dataclass
class Estate:
    sites: list[dict] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    assignments: dict[str, str] = field(default_factory=dict)
    links: list[dict] = field(default_factory=list)
    ap_config: dict[str, dict] = field(default_factory=dict)  # by ethernet mac
    rf: dict[str, list[dict]] = field(default_factory=dict)  # by AP id
    truth: dict[str, str] = field(default_factory=dict)  # AP id -> real floor id
    # Ground truth for the specialist use cases (test oracle, never exposed through the CC API).
    name_truth: dict[str, str] = field(default_factory=dict)  # MAC-named AP id -> intended hostname
    wrong_site: dict[str, str] = field(default_factory=dict)  # AP id -> wrong floor it is assigned to
    role_truth: dict[str, str] = field(default_factory=dict)  # switch id -> real role
    telemetry_truth: dict[str, str] = field(default_factory=dict)  # device id -> credential|snmp|sync_stale
    expected_down: set[str] = field(default_factory=set)  # down for a long time (Dogger Bank pattern)
    flapping: set[str] = field(default_factory=set)
    transient_down: set[str] = field(default_factory=set)
    site_ids_by_name: dict[str, str] = field(default_factory=dict)
    name_reset: dict[str, str] = field(default_factory=dict)  # AP id -> factory name after POST /mock/reset_names
    stacks: list[str] = field(default_factory=list)  # switch stacks (comma-joined platform and serial)
    uplink_truth: dict[str, str] = field(default_factory=dict)  # AP id -> half_duplex|low_speed switch port


def build(seed: int = 1, total_aps: int = 6000, unassigned_ratio: float = 0.2) -> Estate:
    rng = random.Random(seed)
    e = Estate()
    glob = {"id": _uid(rng), "name": "Global", "parentId": None, "hierarchy": "Global", "type": "global"}
    country = {"id": _uid(rng), "name": "Denmark", "parentId": glob["id"], "hierarchy": "Global/Denmark", "type": "area"}
    e.sites += [glob, country]
    cities = [("Copenhagen", "CPH", 12), ("Aarhus", "AAR", 7), ("Odense", "ODE", 5)]
    floors_all: list[tuple[dict, dict, int, int, int]] = []
    for ci, (city, code, nb) in enumerate(cities):
        area = {"id": _uid(rng), "name": city, "parentId": country["id"], "hierarchy": f"Global/Denmark/{city}", "type": "area"}
        e.sites.append(area)
        wlc_id = _uid(rng)
        for b in range(1, nb + 1):
            bname = f"{code}-B{b:02d}"
            bld = {"id": _uid(rng), "name": bname, "parentId": area["id"], "hierarchy": f"{area['hierarchy']}/{bname}",
                   "type": "building"}
            e.sites.append(bld)
            if b == 1:
                e.devices.append(_device(rng, wlc_id, f"{code}-WLC01", f"10.{ci}.250.10", "Wireless Controller",
                                         "Cisco Catalyst 9800-40 Wireless Controller", "C9800-40-K9"))
                e.assignments[wlc_id] = bld["id"]
            switch_on_building = rng.random() < 0.3
            for f in range(1, rng.randint(2, 5) + 1):
                fl = {"id": _uid(rng), "name": f"Floor {f}", "parentId": bld["id"],
                      "hierarchy": f"{bld['hierarchy']}/Floor {f}", "type": "floor"}
                e.sites.append(fl)
                sw_id = _uid(rng)
                e.devices.append(_device(rng, sw_id, f"{bname}-F{f}-SW01", f"10.{ci}.{b * 8 + f}.2", "Switches and Hubs",
                                         "Cisco Catalyst 9300 Switch", "C9300-48P"))
                e.assignments[sw_id] = bld["id"] if switch_on_building else fl["id"]
                floors_all.append((bld, fl, ci, b, f))
                fl["_switch"] = sw_id
                fl["_wlc"] = wlc_id

    per_floor = max(1, total_aps // len(floors_all))
    floor_aps: dict[str, list[str]] = {}
    for bld, fl, ci, b, f in floors_all:
        ids = []
        for i in range(1, per_floor + 1):
            ap_id = _uid(rng)
            eth = _mac(rng)
            noisy = rng.random() < 0.08
            intended = f"{bld['name']}-F{f}-AP{i:03d}"
            h = eth.replace(":", "")
            name = f"AP{h[0:4]}.{h[4:8]}.{h[8:12]}" if noisy else intended  # Cisco factory-default AP name
            if noisy:
                e.name_truth[ap_id] = intended
            dev = _device(rng, ap_id, name, f"10.{ci}.{b * 8 + f}.{10 + i % 240}", "Unified AP",
                          "Cisco Catalyst 9130AXI Unified Access Point", "C9130AXI-E")
            dev["apEthernetMacAddress"] = eth
            dev["associatedWlcIp"] = f"10.{ci}.250.10"
            e.devices.append(dev)
            e.truth[ap_id] = fl["id"]
            if rng.random() >= unassigned_ratio:
                e.assignments[ap_id] = fl["id"]
            if rng.random() < 0.95:
                e.links.append({"source": fl["_switch"], "target": ap_id,
                                "startPortName": f"GigabitEthernet1/0/{i % 48 + 1}", "endPortName": "GigabitEthernet0"})
            r = rng.random()
            e.ap_config[eth] = {
                "apName": name,
                "location": f"{bld['name']} Floor {f}" if r < 0.5 else "default location",
                "siteTagName": f"ST-{bld['name']}" if rng.random() < 0.8 else "default-site-tag",
                "policyTagName": "PT-CORP",
                "primaryControllerName": f"{bld['name'][:3]}-WLC01",
            }
            ids.append(ap_id)
        floor_aps[fl["id"]] = ids

    floors_by_building: dict[str, list[dict]] = {}
    for bld, fl, *_ in floors_all:
        floors_by_building.setdefault(bld["id"], []).append(fl)
    by_id = {d["id"]: d for d in e.devices}
    for bld_id, fls in floors_by_building.items():
        for idx, fl in enumerate(fls):
            same = floor_aps[fl["id"]]
            adjacent = [a for j in (idx - 1, idx + 1) if 0 <= j < len(fls) for a in floor_aps[fls[j]["id"]]]
            for ap in same:
                nbrs = rng.sample([a for a in same if a != ap], k=min(6, len(same) - 1))
                nbrs += rng.sample(adjacent, k=min(2, len(adjacent)))
                e.rf[ap] = [{
                    "neighborApName": by_id[n]["hostname"],
                    "neighborApMac": by_id[n]["macAddress"],
                    "rssi": rng.randint(-72, -48) if n in same else rng.randint(-88, -75),
                    "band": rng.choice(["2.4GHz", "5GHz"]),
                } for n in nbrs]
    _specialist_truth(e, seed, floors_all, cities)
    _uplink_faults(e)
    for s in e.sites:
        s.pop("_switch", None)
        s.pop("_wlc", None)
    # A floor nobody uses (hierarchy hygiene finding; removed by apply_drift).
    b2 = [s for s in e.sites if s["type"] == "building"][1]
    e.sites.append({"id": "00000000-0000-4000-8000-0000000000f9", "name": "F9", "parentId": b2["id"],
                    "hierarchy": f"{b2['hierarchy']}/F9", "type": "floor"})
    e.site_ids_by_name = {s["hierarchy"]: s["id"] for s in e.sites}
    return e


def apply_drift(e: Estate) -> dict:
    """Change the hierarchy the way people do by hand: rename, add, remove and move sites."""
    by_id = {s["id"]: s for s in e.sites}
    areas = [s for s in e.sites if s["type"] == "area" and s["name"] != "Denmark" and s["name"] != "Offshore"]
    bld_a0 = [s for s in e.sites if s["type"] == "building" and s["parentId"] == areas[0]["id"]]
    renamed, moved = bld_a0[0], bld_a0[-1]
    renamed["name"] += "-RENAMED"
    moved["parentId"] = areas[1]["id"]
    added = {"id": "00000000-0000-4000-8000-0000000000a1", "name": "NEW-B99", "parentId": areas[0]["id"],
             "hierarchy": "", "type": "building"}
    e.sites.append(added)
    removed = "00000000-0000-4000-8000-0000000000f9"
    e.sites[:] = [s for s in e.sites if s["id"] != removed]
    by_id = {s["id"]: s for s in e.sites}

    def full(s):
        return s["name"] if not s["parentId"] else f"{full(by_id[s['parentId']])}/{s['name']}"

    for s in e.sites:
        s["hierarchy"] = full(s)
    e.site_ids_by_name = {s["hierarchy"]: s["id"] for s in e.sites}
    return {"renamed": [renamed["id"]], "moved": [moved["id"]], "added": [added["id"]], "removed": [removed]}


def _uplink_faults(e: Estate) -> None:
    """About 1% of AP switch ports at 100 Mbit half duplex and 1% at 100 Mbit full duplex (bad cable).

    Derived from the AP id, not the RNG, so the rest of the estate is unchanged.
    """
    aps = {d["id"] for d in e.devices if d["family"] == "Unified AP"}
    for link in e.links:
        ap = link["target"]
        if ap not in aps or ap in e.expected_down:
            continue
        h = int(ap.replace("-", "")[-6:], 16) % 100
        if h == 0:
            e.uplink_truth[ap] = "half_duplex"
        elif h == 1:
            e.uplink_truth[ap] = "low_speed"


def _specialist_truth(e: Estate, seed: int, floors_all, cities) -> None:
    """Layer core/distribution, telemetry faults, down/flapping devices and wrong sites on the base estate.

    Uses its own RNG so the base AP estate (and the AP site-assignment accuracy) is unchanged.
    """
    rng = random.Random(seed * 7919 + 1)
    now_ms = int(time.time() * 1000)
    by_building: dict[str, list] = {}
    for bld, fl, ci, b, f in floors_all:
        by_building.setdefault(bld["id"], []).append((bld, fl, ci, b, f))
    city_area = {s["name"]: s for s in e.sites if s["type"] == "area"}
    floor_switch = {}
    for d in e.devices:
        if d["family"] == "Switches and Hubs":
            floor_switch[d["id"]] = d
            e.role_truth[d["id"]] = "ACCESS"
    wlcs = [d for d in e.devices if d["family"] == "Wireless Controller"]

    # Core (2 per city) and distribution (1 per building). Odense runs 9300s as core, tagged ACCESS.
    for ci, (city, code, _) in enumerate(cities):
        first_bld = next(bld for bld, fl, c, b, f in floors_all if c == ci and b == 1)
        cores = []
        for k in (1, 2):
            cid = _uid(rng)
            if code == "ODE":
                dev = _device(rng, cid, f"ODE-B01-SW9{k}", f"10.{ci}.255.{k}", "Switches and Hubs",
                              "Cisco Catalyst 9300 Switch", "C9300-48P")
                dev["role"] = "ACCESS"
            else:
                dev = _device(rng, cid, f"{code}-CORE-0{k}", f"10.{ci}.255.{k}", "Switches and Hubs",
                              "Cisco Catalyst 9500 Switch", "C9500-48Y4C")
                dev["role"] = "CORE"
            e.devices.append(dev)
            e.assignments[cid] = first_bld["id"]
            e.role_truth[cid] = "CORE"
            cores.append(cid)
        e.links.append({"source": cores[0], "target": cores[1], "startPortName": "HundredGigE1/0/49",
                        "endPortName": "HundredGigE1/0/49"})
        for w in wlcs:
            if w["managementIpAddress"].startswith(f"10.{ci}."):
                for c in cores:
                    e.links.append({"source": c, "target": w["id"], "startPortName": "TenGigabitEthernet1/0/1",
                                    "endPortName": "TenGigabitEthernet0/0/0"})
        for bld_id, rows in by_building.items():
            bld, _, c, b, _ = rows[0]
            if c != ci:
                continue
            did = _uid(rng)
            dev = _device(rng, did, f"{bld['name']}-DS01", f"10.{ci}.254.{b}", "Switches and Hubs",
                          "Cisco Catalyst 9300 Switch", "C9300-24UX")
            dev["role"] = "DISTRIBUTION" if rng.random() >= 0.2 else "ACCESS"
            e.devices.append(dev)
            e.assignments[did] = bld_id
            e.role_truth[did] = "DISTRIBUTION"
            for c_id in cores:
                e.links.append({"source": c_id, "target": did, "startPortName": f"TenGigabitEthernet1/0/{b}",
                                "endPortName": "TenGigabitEthernet1/1/1"})
            for _, fl, *_ in rows:
                sw = next(d for d in floor_switch.values() if d["hostname"].startswith(f"{bld['name']}-F{fl['name'][-1]}-"))
                e.links.append({"source": did, "target": sw["id"], "startPortName": f"GigabitEthernet1/0/{fl['name'][-1]}",
                                "endPortName": "TenGigabitEthernet1/1/1"})
    for sid in floor_switch:
        if rng.random() < 0.02:
            floor_switch[sid]["role"] = "CORE"  # mis-tagged access switch

    # Offshore site that has been "going live" for years: always down (Dogger Bank pattern).
    country = next(s for s in e.sites if s["name"] == "Denmark")
    area = {"id": _uid(rng), "name": "Offshore", "parentId": country["id"], "hierarchy": "Global/Denmark/Offshore",
            "type": "area"}
    bld = {"id": _uid(rng), "name": "DGB-B01", "parentId": area["id"], "hierarchy": f"{area['hierarchy']}/DGB-B01",
           "type": "building"}
    fl = {"id": _uid(rng), "name": "Floor 1", "parentId": bld["id"], "hierarchy": f"{bld['hierarchy']}/Floor 1",
          "type": "floor"}
    e.sites += [area, bld, fl]
    sw = _device(rng, _uid(rng), "DGB-B01-F1-SW01", "10.9.1.2", "Switches and Hubs", "Cisco Catalyst 9300 Switch",
                 "C9300-48P")
    e.devices.append(sw)
    e.assignments[sw["id"]] = fl["id"]
    e.role_truth[sw["id"]] = "ACCESS"
    e.expected_down.add(sw["id"])
    for i in range(1, 11):
        ap = _device(rng, _uid(rng), f"DGB-B01-F1-AP{i:03d}", f"10.9.1.{10 + i}", "Unified AP",
                     "Cisco Catalyst 9130AXI Unified Access Point", "C9130AXI-E")
        ap["apEthernetMacAddress"] = _mac(rng)
        e.devices.append(ap)
        e.assignments[ap["id"]] = fl["id"]
        e.links.append({"source": sw["id"], "target": ap["id"], "startPortName": f"GigabitEthernet1/0/{i}",
                        "endPortName": "GigabitEthernet0"})
        e.expected_down.add(ap["id"])

    for d in e.devices:
        is_ap = d["family"] == "Unified AP"
        d["series"] = ("Cisco Catalyst 9100AX Series Unified Access Points" if is_ap else
                       "Cisco Catalyst 9800 Series Wireless Controllers" if d["family"] == "Wireless Controller" else
                       f"Cisco Catalyst {d['platformId'][1:5]} Series Switches")
        d["collectionStatus"] = "Managed"
        d["lastUpdateTime"] = now_ms - rng.randint(0, 6 * 3600 * 1000)
        d["errorCode"] = None
        d["errorDescription"] = None
        d["reachabilityFailureReason"] = ""
        d["upTime"] = "120 days, 3:04:11.00"
        if d["id"] in e.expected_down:
            d["reachabilityStatus"] = "Unreachable"
            d["reachabilityFailureReason"] = "SNMP Connectivity Failed"
            d["lastUpdateTime"] = now_ms - 400 * 86_400_000
            continue
        r = rng.random()
        if not is_ap:
            if r < 0.03:
                e.telemetry_truth[d["id"]] = "credential"
                d.update(collectionStatus="Partial Collection Failure", errorCode="CLI_AUTH_FAILED",
                         errorDescription="CLI authentication failed. Check the device credentials.",
                         lastUpdateTime=now_ms - 10 * 86_400_000)
            elif r < 0.06:
                e.telemetry_truth[d["id"]] = "snmp"
                d.update(collectionStatus="Could Not Synchronize", errorCode="SNMP_TIMEOUT",
                         errorDescription="SNMP timeout. Verify the SNMP version and credentials.",
                         lastUpdateTime=now_ms - 12 * 86_400_000)
            elif r < 0.10:
                e.telemetry_truth[d["id"]] = "sync_stale"
                d["lastUpdateTime"] = now_ms - rng.randint(3, 10) * 86_400_000
            elif r < 0.115:
                e.flapping.add(d["id"])
            elif r < 0.12:
                e.transient_down.add(d["id"])
                d["reachabilityStatus"] = "Unreachable"
                d["lastUpdateTime"] = now_ms - 2 * 3600 * 1000
        elif r < 0.004:
            e.flapping.add(d["id"])

    # ~2% of assigned APs sit on a floor in another building of the same city.
    floors_by_city: dict[int, list[dict]] = {}
    for b_, fl_, ci, *_ in floors_all:
        floors_by_city.setdefault(ci, []).append((b_, fl_))
    floor_city = {fl_["id"]: (ci, b_["id"]) for b_, fl_, ci, *_ in floors_all}
    for ap_id, floor_id in list(e.truth.items()):
        if e.assignments.get(ap_id) == floor_id and rng.random() < 0.02:
            ci, bld_id = floor_city[floor_id]
            other = [fl_ for b_, fl_ in floors_by_city[ci] if b_["id"] != bld_id]
            wrong = rng.choice(other)["id"]
            e.assignments[ap_id] = wrong
            e.wrong_site[ap_id] = wrong

    # ~1% of correctly named APs lose their name (factory reset) when the test triggers it.
    for d in e.devices:
        if d["family"] == "Unified AP" and d["id"] not in e.name_truth and d.get("apEthernetMacAddress") \
                and not d["hostname"].startswith("DGB-") and rng.random() < 0.01:
            h = d["apEthernetMacAddress"].replace(":", "")
            e.name_reset[d["id"]] = f"AP{h[0:4]}.{h[4:8]}.{h[8:12]}"
            e.name_truth[d["id"]] = d["hostname"]

    # Switch stacks: Catalyst Center joins the platform and serial of every member with commas.
    # Derived from existing switches without new random draws, so the rest of the estate is unchanged.
    stacks = [d for d in e.devices if d["family"] == "Switches and Hubs" and d["platformId"].startswith("C9300")
              and d["id"] not in e.expected_down][:2]
    for members, d in zip((8, 4), stacks):
        base = int(d["serialNumber"][3:])
        d["platformId"] = ", ".join([d["platformId"]] * members)
        d["serialNumber"] = ", ".join(f"FOC{base + i}" for i in range(members))
        e.stacks.append(d["id"])


def _device(rng, dev_id, hostname, ip, family, typ, platform) -> dict:
    return {
        "id": dev_id, "hostname": hostname, "managementIpAddress": ip, "macAddress": _mac(rng), "family": family,
        "type": typ, "platformId": platform, "serialNumber": f"FOC{rng.randrange(10**7, 10**8)}",
        "reachabilityStatus": "Reachable", "role": "ACCESS", "softwareVersion": "17.12.4",
        "snmpLocation": "", "associatedWlcIp": None,
    }
