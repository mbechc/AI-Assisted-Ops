"""Mock Catalyst Center for integration tests. MOCK_CC_RELEASE selects the API surface:

* 2.3.x (< 2.3.7.6): legacy site/membership APIs, async "execution" assign, /task polling,
  release via /dnac-release, no RF neighbour API.
* 3.x: new site design APIs (/sites, assignedToSite, assignToSite/apply), /tasks polling,
  release only via /dnac-packages (dnac-release returns 404), RF neighbours via Assurance.
"""
import base64
import os
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from mockcc.data import apply_drift, build

RELEASE = os.environ.get("MOCK_CC_RELEASE", "2.3.5.6")
VER = tuple(int(x) for x in RELEASE.split("."))
NEW_API = VER >= (2, 3, 7, 6)
V3 = VER >= (3, 0)
TOKEN = "mock-token-" + uuid.uuid4().hex
estate = build(seed=int(os.environ.get("MOCK_CC_SEED", "1")), total_aps=int(os.environ.get("MOCK_CC_APS", "6000")))
SITES = {s["id"]: s for s in estate.sites}
DEVICES = {d["id"]: d for d in estate.devices}
TASKS: dict[str, dict] = {}
LOCK = threading.Lock()
COUNTER = {"n": 0}
DRIFT: dict = {}
ORIGINAL_SITES: list = []

app = FastAPI(title=f"Mock Catalyst Center {RELEASE}")


def not_found():
    return JSONResponse({"message": "The requested resource could not be found"}, status_code=404)


@app.middleware("http")
async def auth_and_throttle(request: Request, call_next):
    path = request.url.path
    if path in ("/dna/system/api/v1/auth/token", "/healthz") or path.startswith("/mock/"):
        return await call_next(request)
    if request.headers.get("X-Auth-Token") != TOKEN:
        return JSONResponse({"message": "Unauthorized"}, status_code=401)
    with LOCK:
        COUNTER["n"] += 1
        n = COUNTER["n"]
    if n % 250 == 0:
        return JSONResponse({"message": "Too many requests"}, status_code=429, headers={"Retry-After": "1"})
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True, "release": RELEASE}


@app.post("/dna/system/api/v1/auth/token")
def token(request: Request):
    auth = request.headers.get("Authorization", "")
    try:
        user, pw = base64.b64decode(auth.split()[1]).decode().split(":", 1)
    except Exception:  # noqa: BLE001
        raise HTTPException(401, "bad auth")
    if (user, pw) != ("admin", "mock"):
        raise HTTPException(401, "bad credentials")
    return {"Token": TOKEN}


@app.get("/dna/intent/api/v1/dnac-release")
def release():
    if V3:
        return not_found()
    return {"response": {"name": "dnac", "displayVersion": RELEASE, "installedVersion": RELEASE}, "version": "1.0"}


@app.get("/dna/intent/api/v1/dnac-packages")
def packages():
    return {"response": [{"name": "assurance", "version": "3.1.3.1000"}, {"name": "dnac", "version": RELEASE}]}


def _page(items, offset: int, limit: int):
    if limit > 500:
        raise HTTPException(400, "limit must be <= 500")
    start = max(0, int(offset) - 1)
    return items[start:start + int(limit)]


# ------------------------------------------------------------- sites

@app.get("/dna/intent/api/v1/sites")
def sites_new(offset: int = 1, limit: int = 500):
    if not NEW_API:
        return not_found()
    items = [{"id": s["id"], "parentId": s["parentId"], "name": s["name"], "nameHierarchy": s["hierarchy"],
              "type": s["type"]} for s in estate.sites]
    return {"response": _page(items, offset, limit), "version": "1.0"}


@app.get("/dna/intent/api/v1/site")
def sites_legacy(offset: int = 1, limit: int = 500):
    if V3:
        return not_found()
    items = []
    for s in estate.sites:
        info = [] if s["type"] == "global" else [{"nameSpace": "Location", "attributes": {"type": s["type"]}}]
        items.append({"id": s["id"], "parentId": s["parentId"], "name": s["name"],
                      "siteNameHierarchy": s["hierarchy"], "additionalInfo": info})
    return {"response": _page(items, offset, limit)}


# ------------------------------------------------------------- devices / membership

@app.get("/dna/intent/api/v1/network-device")
def network_device(offset: int = 1, limit: int = 500, family: str | None = None, id: str | None = None):
    if id:
        ids = id.split(",")
        return {"response": [DEVICES[i] for i in ids if i in DEVICES], "version": "1.0"}
    if offset == 1 and not family:
        # every full inventory read sees flapping devices in the opposite state
        with LOCK:
            COUNTER["listings"] = COUNTER.get("listings", 0) + 1
            down = COUNTER["listings"] % 2 == 0
        for d in estate.flapping:
            DEVICES[d]["reachabilityStatus"] = "Unreachable" if down else "Reachable"
    items = [d for d in estate.devices if not family or d["family"] == family]
    return {"response": _page(items, offset, limit), "version": "1.0"}


@app.get("/dna/intent/api/v1/networkDevices/assignedToSite")
def assigned_to_site(siteId: str, offset: int = 1, limit: int = 500):
    if not NEW_API:
        return not_found()
    items = [{"deviceId": d, "siteId": s, "siteNameHierarchy": SITES[s]["hierarchy"], "siteType": SITES[s]["type"]}
             for d, s in estate.assignments.items() if s == siteId]
    return {"response": _page(items, offset, limit), "version": "1.0"}


@app.get("/dna/intent/api/v1/membership/{site_id}")
def membership(site_id: str, offset: int = 1, limit: int = 500):
    if V3:
        return not_found()
    if site_id not in SITES:
        return not_found()
    members = [DEVICES[d] for d, s in estate.assignments.items() if s == site_id]
    page = _page(members, offset, limit)
    return {"site": {"response": [], "version": "1.0"},
            "device": [{"response": page, "version": "1.0", "siteId": site_id}] if page else []}


@app.get("/dna/intent/api/v1/topology/physical-topology")
def topology():
    nodes = [{"id": d["id"], "label": d["hostname"], "ip": d["managementIpAddress"], "family": d["family"]}
             for d in estate.devices]
    return {"response": {"nodes": nodes, "links": estate.links}, "version": "1.0"}


def _port_speed(name: str) -> str:
    n = name.lower()
    return "100000000" if n.startswith("hundred") else "10000000" if n.startswith("ten") else "1000000"


def _ports() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for link in estate.links:
        fault = estate.uplink_truth.get(link["target"])
        down = link["source"] in estate.expected_down or link["target"] in estate.expected_down
        for dev, port in ((link["source"], link["startPortName"]), (link["target"], link["endPortName"])):
            out.setdefault(dev, []).append({
                "id": f"{dev[:8]}-{port}", "deviceId": dev, "portName": port, "interfaceType": "Physical",
                "speed": "100000" if fault else _port_speed(port),
                "duplex": "HalfDuplex" if fault == "half_duplex" else "FullDuplex",
                "status": "down" if down else "up", "adminStatus": "UP", "portMode": "access",
                "description": "AP uplink" if DEVICES[link["target"]]["family"] == "Unified AP" else "",
            })
    return out


PORTS = _ports()
# AP id behind each switch port, for ports whose state changes between reads.
PORT_AP = {(ln["source"], ln["startPortName"]): ln["target"] for ln in estate.links}
READS: dict[str, int] = {}


def _port_status(dev: str, port: dict, n: int) -> str:
    ap = PORT_AP.get((dev, port["portName"]))
    if ap in estate.errdis_truth:
        return "down" if n % 2 == 0 else "up"  # err-disabled, re-enabled, err-disabled again ...
    if ap in estate.errdis_once:
        return "down" if n == 2 else "up"
    return port["status"]


@app.get("/dna/intent/api/v1/interface/network-device/{device_id}")
def interfaces(device_id: str):
    if device_id not in DEVICES:
        return not_found()
    with LOCK:
        READS[device_id] = n = READS.get(device_id, 0) + 1
    return {"response": [{**p, "status": _port_status(device_id, p, n)} for p in PORTS.get(device_id, [])],
            "version": "1.0"}


@app.get("/dna/intent/api/v1/network-device/{device_id}/interface/poe-detail")
def poe_detail(device_id: str):
    if device_id not in DEVICES:
        return not_found()
    out = []
    for ln in estate.links:
        ap = DEVICES.get(ln["target"])
        if ln["source"] != device_id or not ap or ap["family"] != "Unified AP":
            continue
        down = device_id in estate.expected_down or ap["id"] in estate.expected_down
        alloc = 30.0 if ap["id"] in estate.power_truth else 45.0
        out.append({"interfaceName": ln["startPortName"], "adminStatus": "AUTO", "operStatus": "OFF" if down else "ON",
                    "maxPortPower": "60.0", "allocatedPower": "0.0" if down else f"{alloc:.1f}",
                    "portPowerDrawn": "0.0" if down else f"{alloc - 4:.1f}", "pdDeviceModel": ap["platformId"],
                    "pdClassSignal": "4" if alloc <= 30 else "5"})
    return {"response": out, "version": "1.0"}


# ------------------------------------------------------------- wireless

@app.get("/dna/intent/api/v1/wireless/accesspoint-configuration/summary")
def ap_config(key: str):
    cfg = estate.ap_config.get(key)
    if cfg is None:
        return not_found()
    return cfg


@app.get("/dna/data/api/v1/accessPoints/{ap_id}/rfNeighbors")
def rf_neighbors(ap_id: str):
    if not V3:
        return not_found()
    return {"response": estate.rf.get(ap_id, []), "version": "1.0"}


# ------------------------------------------------------------- assignment + async

def _assign(site_id: str, device_ids: list[str]) -> str | None:
    site = SITES.get(site_id)
    if site is None:
        return "site not found"
    if site["type"] not in ("building", "floor"):
        return "devices can only be assigned to a building or floor"
    for d in device_ids:
        if d not in DEVICES:
            return f"device {d} not found"
        if DEVICES[d]["family"] == "Unified AP" and site["type"] != "floor":
            return "access points can only be assigned to a floor"
    for d in device_ids:
        estate.assignments[d] = site_id
    return None


@app.post("/dna/intent/api/v1/networkDevices/assignToSite/apply")
async def assign_apply(request: Request):
    if not NEW_API:
        return not_found()
    body = await request.json()
    err = _assign(body.get("siteId"), body.get("deviceIds") or [])
    tid = str(uuid.uuid4())
    TASKS[tid] = {"polls": 0, "error": err}
    return JSONResponse({"response": {"taskId": tid, "url": f"/dna/intent/api/v1/tasks/{tid}"}, "version": "1.0"},
                        status_code=202)


@app.post("/dna/system/api/v1/site/{site_id}/device")
async def assign_legacy(site_id: str, request: Request):
    if V3:
        return not_found()
    body = await request.json()
    ips = {d.get("ip") for d in body.get("device") or []}
    ids = [d["id"] for d in estate.devices if d["managementIpAddress"] in ips]
    err = _assign(site_id, ids) if ids else "no devices matched"
    eid = str(uuid.uuid4())
    TASKS[eid] = {"polls": 0, "error": err}
    return JSONResponse({"executionId": eid,
                         "executionStatusUrl": f"/dna/intent/api/v1/dnacaap/management/execution-status/{eid}",
                         "message": "The request has been accepted for execution"}, status_code=202)


def _poll(tid: str) -> dict | None:
    t = TASKS.get(tid)
    if t is None:
        return None
    t["polls"] += 1
    return t


@app.get("/dna/intent/api/v1/tasks/{tid}")
def task_new(tid: str):
    if not NEW_API:
        return not_found()
    t = _poll(tid)
    if t is None:
        return not_found()
    status = "PENDING" if t["polls"] < 2 else ("FAILURE" if t["error"] else "SUCCESS")
    return {"response": {"id": tid, "status": status, "endTime": 1 if status != "PENDING" else None}, "version": "1.0"}


@app.get("/dna/intent/api/v1/tasks/{tid}/detail")
def task_new_detail(tid: str):
    t = TASKS.get(tid)
    if t is None or not NEW_API:
        return not_found()
    return {"response": {"id": tid, "failureReason": t["error"]}, "version": "1.0"}


@app.get("/dna/intent/api/v1/task/{tid}")
def task_legacy(tid: str):
    t = _poll(tid)
    if t is None:
        return not_found()
    done = t["polls"] >= 2
    return {"response": {"id": tid, "isError": bool(done and t["error"]), "failureReason": t["error"],
                         "progress": "done" if done else "in progress", "endTime": 1 if done else None}}


@app.get("/dna/intent/api/v1/dnacaap/management/execution-status/{eid}")
def execution(eid: str):
    t = _poll(eid)
    if t is None:
        return not_found()
    status = "IN_PROGRESS" if t["polls"] < 2 else ("FAILURE" if t["error"] else "SUCCESS")
    return {"executionId": eid, "status": status, "bapiError": t["error"] or ""}


# ------------------------------------------------------------- specialist operations

def _new_task(err: str | None = None) -> JSONResponse:
    tid = str(uuid.uuid4())
    TASKS[tid] = {"polls": 0, "error": err}
    return JSONResponse({"response": {"taskId": tid, "url": f"/dna/intent/api/v1/task/{tid}"}, "version": "1.0"},
                        status_code=202)


async def _rename(request: Request):
    body = await request.json()
    by_mac = {d["macAddress"]: d for d in estate.devices if d["family"] == "Unified AP"}
    for entry in body.get("apList") or []:
        dev = by_mac.get(entry.get("macAddress"))
        if dev is None or dev["hostname"] != entry.get("apName"):
            return _new_task(f"AP {entry.get('apName')} / {entry.get('macAddress')} not found")
        if not entry.get("apNameNew"):
            return _new_task("apNameNew missing")
    for entry in body.get("apList") or []:
        dev = by_mac[entry["macAddress"]]
        dev["hostname"] = entry["apNameNew"]
        if dev.get("apEthernetMacAddress") in estate.ap_config:
            estate.ap_config[dev["apEthernetMacAddress"]]["apName"] = entry["apNameNew"]
    return _new_task()


@app.post("/dna/intent/api/v1/wireless/accesspoint-configuration")
async def ap_config_v1(request: Request):
    return await _rename(request)


@app.post("/dna/intent/api/v2/wireless/accesspoint-configuration")
async def ap_config_v2(request: Request):
    if VER < (2, 3, 7):
        return not_found()
    return await _rename(request)


@app.put("/dna/intent/api/v1/network-device/brief")
async def device_brief(request: Request):
    body = await request.json()
    dev = DEVICES.get(body.get("id"))
    if dev is None:
        return _new_task("device not found")
    if body.get("role") not in ("ACCESS", "DISTRIBUTION", "CORE", "BORDER ROUTER", "UNKNOWN"):
        return _new_task("invalid role")
    dev["role"] = body["role"]
    return _new_task()


@app.put("/dna/intent/api/v1/network-device/sync")
async def device_sync(request: Request):
    import time as _t
    ids = await request.json()
    for d in ids or []:
        if d not in DEVICES:
            return _new_task(f"device {d} not found")
    for d in ids:
        if estate.telemetry_truth.get(d) == "sync_stale":
            DEVICES[d]["lastUpdateTime"] = int(_t.time() * 1000)
            estate.telemetry_truth.pop(d)
    return _new_task()


def _health(d: dict) -> int | None:
    if d["id"] in estate.telemetry_truth and estate.telemetry_truth[d["id"]] != "sync_stale":
        return None
    if d["reachabilityStatus"] != "Reachable":
        return 0
    return 7 + (int(d["id"][:2], 16) % 4)


@app.get("/dna/intent/api/v1/device-health")
def device_health(offset: int = 1, limit: int = 500):
    if V3:
        return not_found()
    items = [{"uuid": d["id"], "name": d["hostname"], "ipAddress": d["managementIpAddress"],
              "overallHealth": _health(d), "reachabilityHealth": d["reachabilityStatus"]} for d in estate.devices]
    return {"response": _page(items, offset, limit), "totalCount": len(items), "version": "1.0"}


@app.get("/dna/data/api/v1/assuranceNetworkDevices")
def assurance_devices(offset: int = 1, limit: int = 500):
    if not NEW_API:
        return not_found()
    items = [{"id": d["id"], "name": d["hostname"], "managementIpAddress": d["managementIpAddress"],
              "overallHealthScore": _health(d), "reachabilityStatus": d["reachabilityStatus"]} for d in estate.devices]
    return {"response": _page(items, offset, limit), "version": "1.0"}


def _compliance() -> dict[str, dict]:
    import time as _t
    now = int(_t.time() * 1000)
    out = {}
    for d in estate.devices:
        if d["family"] == "Unified AP":
            continue
        days = estate.unsaved_truth.get(d["id"])
        out[d["id"]] = {"deviceUuid": d["id"], "complianceType": "RUNNING_CONFIG", "state": "SUCCESS",
                        "status": "COMPLIANT" if days is None else "NON_COMPLIANT",
                        "lastUpdateTime": now - (days * 86_400_000 if days else 2 * 3_600_000)}
    return out


COMPLIANCE = _compliance()


@app.get("/dna/intent/api/v1/compliance/detail")
def compliance_detail(complianceType: str | None = None, complianceStatus: str | None = None,
                      deviceUuid: str | None = None, offset: int = 1, limit: int = 500):
    ids = set(deviceUuid.split(",")) if deviceUuid else None
    items = [c for c in COMPLIANCE.values() if (not complianceType or c["complianceType"] == complianceType)
             and (not complianceStatus or c["status"] == complianceStatus) and (ids is None or c["deviceUuid"] in ids)]
    return {"response": _page(items, offset, limit), "version": "1.0"}


@app.post("/dna/intent/api/v1/network-device-config/write-memory")
async def write_memory(request: Request):
    if VER < (2, 3, 7):
        return not_found()
    ids = (await request.json()).get("deviceId") or []
    if not ids or any(d not in COMPLIANCE for d in ids):
        return _new_task("unknown device")
    for d in ids:
        COMPLIANCE[d]["status"] = "COMPLIANT"
        estate.unsaved_truth.pop(d, None)
    return _new_task()


@app.post("/dna/intent/api/v1/compliance/")
async def compliance_run(request: Request):
    return _new_task()


@app.delete("/dna/intent/api/v1/network-device/{device_id}")
def delete_device(device_id: str, cleanConfig: bool = False):
    if device_id not in DEVICES:
        return not_found()
    DEVICES.pop(device_id)
    estate.devices[:] = [d for d in estate.devices if d["id"] != device_id]
    estate.assignments.pop(device_id, None)
    COMPLIANCE.pop(device_id, None)
    return _new_task()


MAINT: dict[str, dict] = {}
TAGS: dict[str, dict] = {}


@app.get("/dna/intent/api/v1/networkDevices/maintenanceSchedules")
def maint_list(offset: int = 1, limit: int = 500):
    if not NEW_API:
        return not_found()
    return {"response": _page(list(MAINT.values()), offset, limit), "version": "1.0"}


@app.post("/dna/intent/api/v1/networkDevices/maintenanceSchedules")
async def maint_create(request: Request):
    if not NEW_API:
        return not_found()
    body = await request.json()
    ids = body.get("networkDeviceIds") or []
    if not ids or any(d not in DEVICES for d in ids):
        return _new_task("unknown device")
    sid = str(uuid.uuid4())
    MAINT[sid] = {"id": sid, "description": body.get("description"), "networkDeviceIds": ids,
                  "maintenanceSchedule": body.get("maintenanceSchedule")}
    return _new_task()


@app.delete("/dna/intent/api/v1/networkDevices/maintenanceSchedules/{sid}")
def maint_delete(sid: str):
    if not NEW_API:
        return not_found()
    if MAINT.pop(sid, None) is None:
        return not_found()
    return _new_task()


@app.get("/dna/intent/api/v1/tag")
def tag_list(name: str | None = None):
    return {"response": [{"id": t["id"], "name": t["name"]} for t in TAGS.values() if not name or t["name"] == name],
            "version": "1.0"}


@app.post("/dna/intent/api/v1/tag")
async def tag_create(request: Request):
    body = await request.json()
    tid = str(uuid.uuid4())
    TAGS[tid] = {"id": tid, "name": body["name"], "members": set()}
    return _new_task()


@app.post("/dna/intent/api/v1/tag/{tag_id}/member")
async def tag_add(tag_id: str, request: Request):
    if tag_id not in TAGS:
        return not_found()
    body = await request.json()
    ids = body.get("networkdevice") or []
    if any(d not in DEVICES for d in ids):
        return _new_task("unknown device")
    TAGS[tag_id]["members"].update(ids)
    return _new_task()


@app.get("/dna/intent/api/v1/tag/{tag_id}/member")
def tag_members(tag_id: str, memberType: str = "networkdevice"):
    if tag_id not in TAGS:
        return not_found()
    return {"response": [{"instanceUuid": d} for d in sorted(TAGS[tag_id]["members"])], "version": "1.0"}


@app.delete("/dna/intent/api/v1/tag/{tag_id}/member/{member_id}")
def tag_remove(tag_id: str, member_id: str):
    if tag_id not in TAGS or member_id not in TAGS[tag_id]["members"]:
        return not_found()
    TAGS[tag_id]["members"].discard(member_id)
    return _new_task()


def excluded_ids() -> set[str]:
    out = {d for m in MAINT.values() for d in m["networkDeviceIds"]}
    for t in TAGS.values():
        out |= t["members"]
    return out


# ------------------------------------------------------------- test oracle (not part of the CC API)

@app.post("/mock/drift")
def drift():
    """Apply hierarchy drift (rename, move, add, remove); returns what changed. Idempotent."""
    import copy
    with LOCK:
        if not DRIFT:
            ORIGINAL_SITES[:] = copy.deepcopy(estate.sites)
            DRIFT.update(apply_drift(estate))
            SITES.clear()
            SITES.update({s["id"]: s for s in estate.sites})
    return DRIFT


@app.delete("/mock/drift")
def undo_drift():
    with LOCK:
        if DRIFT:
            estate.sites[:] = ORIGINAL_SITES
            estate.site_ids_by_name = {s["hierarchy"]: s["id"] for s in estate.sites}
            SITES.clear()
            SITES.update({s["id"]: s for s in estate.sites})
            DRIFT.clear()
    return {"ok": True}


@app.post("/mock/reset_names")
def reset_names():
    """Factory-reset the names of ~1% of the APs (the hostname-reset use case); returns their ids."""
    for d, factory in estate.name_reset.items():
        DEVICES[d]["hostname"] = factory
    return sorted(estate.name_reset)


@app.get("/mock/geoip.mmdb")
def geoip_mmdb():
    """A GeoIP database matching the estate: one location key per building /24, 10% of buildings missing."""
    import tempfile
    from collections import Counter, defaultdict

    from fastapi.responses import Response

    from app.connectors.geoip.writer import write_mmdb

    by_net: dict[str, Counter] = defaultdict(Counter)
    for dev_id, floor in estate.truth.items():
        ip = DEVICES[dev_id]["managementIpAddress"]
        b = SITES[floor]["parentId"] if SITES[floor]["type"] == "floor" else floor
        by_net[ip.rsplit(".", 1)[0] + ".0/24"][b] += 1
    networks = []
    for net, c in by_net.items():
        b = c.most_common(1)[0][0]
        if sum(map(ord, b)) % 10 == 0:
            continue
        networks.append((net, {"site": f"LOC {SITES[b]['name'].upper()}", "country": {"iso_code": "DK"}}))
    with tempfile.NamedTemporaryFile(suffix=".mmdb") as fh:
        write_mmdb(fh.name, networks, description=f"Mock GeoIP for {RELEASE}")
        data = open(fh.name, "rb").read()
    return Response(data, media_type="application/octet-stream")


@app.get("/mock/truth")
def truth():
    return {"release": RELEASE, "truth": estate.truth,
            "unassigned": [d for d in estate.truth if d not in estate.assignments],
            "assignments": estate.assignments,
            "name_truth": estate.name_truth,
            "hostnames": {d["id"]: d["hostname"] for d in estate.devices},
            "wrong_site": {a: w for a, w in estate.wrong_site.items() if estate.assignments.get(a) == w},
            "role_truth": estate.role_truth,
            "roles": {d["id"]: d["role"] for d in estate.devices if d["id"] in estate.role_truth},
            "telemetry_truth": estate.telemetry_truth,
            "uplink_truth": estate.uplink_truth,
            "expected_down": sorted(estate.expected_down), "flapping": sorted(estate.flapping),
            "transient_down": sorted(estate.transient_down), "excluded": sorted(excluded_ids()),
            "stacks": estate.stacks,
            "power_truth": sorted(estate.power_truth),
            "errdis_truth": _port_truth(estate.errdis_truth), "errdis_once": _port_truth(estate.errdis_once),
            "unsaved_truth": estate.unsaved_truth,
            "stale": sorted(d["id"] for d in estate.devices if d["reachabilityStatus"] == "Unreachable"
                            and d["lastUpdateTime"] < _now_ms() - 90 * 86_400_000)}


def _now_ms() -> int:
    import time as _t
    return int(_t.time() * 1000)


def _port_truth(aps: set[str]) -> list[dict]:
    return [{"switch": sw, "port": port, "ap": ap} for (sw, port), ap in PORT_AP.items() if ap in aps]
