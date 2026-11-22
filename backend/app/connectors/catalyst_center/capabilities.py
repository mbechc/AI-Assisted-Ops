"""Declarative capability map: logical operation -> ordered, release-ranged candidates.

Each candidate implements one way of performing the operation on a range of
Catalyst Center releases and returns normalized models. The resolver tries the
applicable candidates in order and remembers which one works for a source, so
2.3.x and 3.x quirks never leak into use cases.

`verified=False` marks endpoints that have not yet been confirmed against a lab
recording; they are probed at runtime and simply reported as unsupported if the
release does not expose them.
"""
from dataclasses import dataclass
from typing import Any, Callable

from app.connectors.catalyst_center.http import CCHttp
from app.connectors.catalyst_center.models import (
    ApConfig, Device, Interface, Link, RfNeighbor, Site, TaskHandle, TaskResult,
)
from app.connectors.catalyst_center.version import Version

NEW_SITE_API = (2, 3, 7, 6)


@dataclass(frozen=True)
class Candidate:
    name: str
    api: str
    path: str
    fn: Callable[..., Any]
    min_version: Version | None = None
    max_version: Version | None = None  # exclusive
    verified: bool = True
    per_object: bool = False  # 404 may mean "object not found" rather than "endpoint missing"

    def applies(self, version: Version | None) -> bool:
        if version is None:
            return True
        if self.min_version and version < self.min_version:
            return False
        if self.max_version and version >= self.max_version:
            return False
        return True


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


# ---------------------------------------------------------------- sites

def _site_type_legacy(item: dict) -> str:
    for info in item.get("additionalInfo") or []:
        if info.get("nameSpace") == "Location":
            t = (info.get("attributes") or {}).get("type")
            if t:
                return str(t).lower()
    return "global" if item.get("name") == "Global" else "area"


def sites_v1(http: CCHttp) -> list[Site]:
    out = []
    for it in http.paged("/dna/intent/api/v1/sites", limit=500):
        hierarchy = _first(it, "nameHierarchy", "siteNameHierarchy") or it.get("name", "")
        typ = str(it.get("type") or "").lower() or ("global" if hierarchy == "Global" else "area")
        out.append(Site(id=it["id"], name=it.get("name", ""), parent_id=it.get("parentId"),
                        hierarchy=hierarchy, type=typ))
    return out


def sites_legacy(http: CCHttp) -> list[Site]:
    out = []
    for it in http.paged("/dna/intent/api/v1/site", limit=500):
        hierarchy = _first(it, "siteNameHierarchy", "groupNameHierarchy") or it.get("name", "")
        out.append(Site(id=it["id"], name=it.get("name", ""), parent_id=it.get("parentId"),
                        hierarchy=hierarchy, type=_site_type_legacy(it)))
    return out


# ---------------------------------------------------------------- devices

def _device(it: dict) -> Device:
    return Device(
        id=it["id"],
        hostname=_first(it, "hostname", "name") or it["id"],
        mgmt_ip=_first(it, "managementIpAddress", "managementAddress"),
        mac=_first(it, "macAddress"),
        eth_mac=_first(it, "apEthernetMacAddress"),
        family=it.get("family") or "",
        type=it.get("type"),
        platform=it.get("platformId"),
        serial=it.get("serialNumber"),
        reachability=it.get("reachabilityStatus"),
        role=it.get("role"),
        wlc_ip=_first(it, "associatedWlcIp", "apManagerInterfaceIp"),
        software_version=it.get("softwareVersion"),
        snmp_location=_first(it, "snmpLocation", "location", "locationName"),
        series=it.get("series"),
        collection_status=it.get("collectionStatus"),
        reachability_failure=_first(it, "reachabilityFailureReason"),
        error_code=_first(it, "errorCode"),
        error_description=_first(it, "errorDescription"),
        last_update_ms=_int(_first(it, "lastUpdateTime")),
        uptime=_first(it, "upTime"),
    )


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def devices_v1(http: CCHttp) -> list[Device]:
    return [_device(it) for it in http.paged("/dna/intent/api/v1/network-device", limit=500)]


def devices_by_id_v1(http: CCHttp, ids: list[str]) -> list[Device]:
    out: list[Device] = []
    for i in range(0, len(ids), 40):
        body = http.get("/dna/intent/api/v1/network-device", {"id": ",".join(ids[i:i + 40])})
        out += [_device(it) for it in (body or {}).get("response") or []]
    return out


# ---------------------------------------------------------------- site membership

def _ordered_sites(sites: list[Site]) -> list[Site]:
    # Parents first so the most specific (deepest) site wins when a release
    # also reports child-site members on the parent.
    return sorted((s for s in sites if s.type != "global"), key=lambda s: s.hierarchy.count("/"))


def assignments_v1(http: CCHttp, sites: list[Site]) -> dict[str, str]:
    out: dict[str, str] = {}
    for site in _ordered_sites(sites):
        for it in http.paged("/dna/intent/api/v1/networkDevices/assignedToSite",
                             {"siteId": site.id}, limit=500):
            dev = _first(it, "deviceId", "id")
            if dev:
                out[dev] = it.get("siteId") or site.id
    return out


def assignments_legacy(http: CCHttp, sites: list[Site]) -> dict[str, str]:
    out: dict[str, str] = {}
    limit = 500
    for site in _ordered_sites(sites):
        offset = 1
        while True:
            body = http.get(f"/dna/intent/api/v1/membership/{site.id}", {"offset": offset, "limit": limit})
            groups = (body or {}).get("device") or []
            count = 0
            for group in groups:
                member_site = group.get("siteId") or site.id
                for dev in group.get("response") or []:
                    if dev.get("id"):
                        out[dev["id"]] = member_site
                        count += 1
            if count < limit:
                break
            offset += limit
    return out


# ---------------------------------------------------------------- topology

def links_physical_topology(http: CCHttp) -> list[Link]:
    body = http.get("/dna/intent/api/v1/topology/physical-topology")
    resp = (body or {}).get("response") or {}
    out = []
    for ln in resp.get("links") or []:
        if ln.get("source") and ln.get("target"):
            out.append(Link(a_id=ln["source"], b_id=ln["target"],
                            a_port=ln.get("startPortName"), b_port=ln.get("endPortName")))
    return out


def interfaces_v1(http: CCHttp, device_id: str) -> list[Interface]:
    body = http.get(f"/dna/intent/api/v1/interface/network-device/{device_id}")
    out = []
    for it in (body or {}).get("response") or []:
        kbps = _int(it.get("speed"))
        out.append(Interface(
            device_id=device_id, name=it.get("portName") or it.get("name") or "",
            speed_mbps=kbps // 1000 if kbps else None, duplex=it.get("duplex"), status=it.get("status"),
            admin_status=it.get("adminStatus"), description=it.get("description")))
    return out


# ---------------------------------------------------------------- wireless (via Catalyst Center only)

def ap_config_summary(http: CCHttp, ap: Device) -> ApConfig | None:
    key = ap.eth_mac or ap.mac
    if not key:
        return None
    body = http.get("/dna/intent/api/v1/wireless/accesspoint-configuration/summary", {"key": key})
    it = body.get("response", body) if isinstance(body, dict) else {}
    if isinstance(it, list):
        it = it[0] if it else {}
    if not it:
        return None
    return ApConfig(
        ap_id=ap.id,
        location=_first(it, "location", "apLocation"),
        site_tag=_first(it, "siteTagName", "siteTag"),
        policy_tag=_first(it, "policyTagName", "policyTag"),
        primary_wlc=_first(it, "primaryControllerName", "primaryWlcName"),
    )


def rf_neighbors_assurance(http: CCHttp, ap: Device) -> list[RfNeighbor]:
    body = http.get(f"/dna/data/api/v1/accessPoints/{ap.id}/rfNeighbors")
    items = body.get("response", []) if isinstance(body, dict) else (body or [])
    out = []
    for it in items or []:
        rssi = _first(it, "rssi", "rssiDbm", "neighborRssi")
        out.append(RfNeighbor(
            neighbor_id=_first(it, "neighborDeviceId", "neighborId", "deviceId"),
            neighbor_name=_first(it, "neighborApName", "neighborName", "apName"),
            neighbor_mac=_first(it, "neighborApMac", "neighborMac", "macAddress"),
            rssi=float(rssi) if rssi is not None else None,
            band=_first(it, "band", "frequencyBand"),
        ))
    return out


# ---------------------------------------------------------------- write: assign to site

def assign_apply_v1(http: CCHttp, site: Site, devices: list[Device]) -> TaskHandle:
    body = http.request("POST", "/dna/intent/api/v1/networkDevices/assignToSite/apply",
                        json={"deviceIds": [d.id for d in devices], "siteId": site.id})
    resp = (body or {}).get("response") or {}
    return TaskHandle(kind="task", id=resp.get("taskId"), url=resp.get("url"))


def assign_legacy(http: CCHttp, site: Site, devices: list[Device]) -> TaskHandle:
    body = http.request("POST", f"/dna/system/api/v1/site/{site.id}/device",
                        json={"device": [{"ip": d.mgmt_ip} for d in devices if d.mgmt_ip]},
                        headers={"__runsync": "false", "__persistbapioutput": "true"})
    return TaskHandle(kind="execution", id=(body or {}).get("executionId"),
                      url=(body or {}).get("executionStatusUrl"))


# ---------------------------------------------------------------- async task status
# Return None while still running.

def task_status_v1_tasks(http: CCHttp, handle: TaskHandle) -> TaskResult | None:
    body = http.get(f"/dna/intent/api/v1/tasks/{handle.id}")
    resp = (body or {}).get("response") or {}
    status = str(resp.get("status", "")).upper()
    if status in ("PENDING", "IN_PROGRESS", ""):
        return None
    if status == "SUCCESS":
        return TaskResult(True, "success", resp)
    detail = ""
    try:
        d = http.get(f"/dna/intent/api/v1/tasks/{handle.id}/detail")
        detail = ((d or {}).get("response") or {}).get("failureReason") or ""
    except Exception:  # noqa: BLE001 - detail is best effort
        pass
    return TaskResult(False, detail or status, resp)


def task_status_legacy(http: CCHttp, handle: TaskHandle) -> TaskResult | None:
    body = http.get(f"/dna/intent/api/v1/task/{handle.id}")
    resp = (body or {}).get("response") or {}
    if resp.get("isError"):
        return TaskResult(False, resp.get("failureReason") or resp.get("progress") or "error", resp)
    if resp.get("endTime"):
        return TaskResult(True, resp.get("progress") or "success", resp)
    return None


def execution_status(http: CCHttp, handle: TaskHandle) -> TaskResult | None:
    path = handle.url or f"/dna/intent/api/v1/dnacaap/management/execution-status/{handle.id}"
    body = http.get(path) or {}
    status = str(body.get("status", "")).upper()
    if status in ("IN_PROGRESS", "PENDING", ""):
        return None
    if status == "SUCCESS":
        return TaskResult(True, "success", body)
    return TaskResult(False, str(body.get("bapiError") or status), body)


# ---------------------------------------------------------------- write: AP rename (Catalyst Center AP config API)

def _handle(body: dict | None) -> TaskHandle:
    resp = (body or {}).get("response") or {}
    if isinstance(resp, list):
        resp = resp[0] if resp else {}
    return TaskHandle(kind="task", id=resp.get("taskId") or (body or {}).get("taskId"), url=resp.get("url"))


def _ap_rename_body(renames: list[tuple[Device, str]]) -> dict:
    return {"apList": [{"apName": d.hostname, "macAddress": d.mac, "apNameNew": new} for d, new in renames],
            "configureApName": True, "configureAdminStatus": False, "configureApMode": False,
            "configureLedStatus": False, "configureLocation": False}


def rename_aps_v2(http: CCHttp, renames: list[tuple[Device, str]]) -> TaskHandle:
    return _handle(http.request("POST", "/dna/intent/api/v2/wireless/accesspoint-configuration",
                                json=_ap_rename_body(renames)))


def rename_aps_v1(http: CCHttp, renames: list[tuple[Device, str]]) -> TaskHandle:
    return _handle(http.request("POST", "/dna/intent/api/v1/wireless/accesspoint-configuration",
                                json=_ap_rename_body(renames)))


# ---------------------------------------------------------------- write: device role

def set_role_brief_v1(http: CCHttp, device: Device, role: str) -> TaskHandle:
    return _handle(http.request("PUT", "/dna/intent/api/v1/network-device/brief",
                                json={"id": device.id, "role": role, "roleSource": "MANUAL"}))


# ---------------------------------------------------------------- write: device resync

def sync_devices_v1(http: CCHttp, device_ids: list[str]) -> TaskHandle:
    return _handle(http.request("PUT", "/dna/intent/api/v1/network-device/sync", params={"forceSync": "false"},
                                json=list(device_ids)))


# ---------------------------------------------------------------- device health (assurance)

def _health_row(it: dict) -> tuple[str | None, dict]:
    dev = _first(it, "id", "deviceId", "uuid", "networkDeviceId")
    score = _first(it, "overallHealthScore", "overallHealth", "healthScore")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return dev, {"health": score, "reachability": _first(it, "reachabilityStatus", "reachabilityHealth")}


def health_assurance_devices(http: CCHttp) -> dict[str, dict]:
    out = {}
    for it in http.paged("/dna/data/api/v1/assuranceNetworkDevices", limit=500):
        dev, row = _health_row(it)
        if dev:
            out[dev] = row
    return out


def health_device_health_v1(http: CCHttp) -> dict[str, dict]:
    out = {}
    for it in http.paged("/dna/intent/api/v1/device-health", limit=500):
        dev, row = _health_row(it)
        if dev:
            out[dev] = row
    return out


# ---------------------------------------------------------------- exclusion: maintenance mode, tag fallback
# list -> {device_id: {"mechanism", "ref", "until_ms"}}; exclude/include -> list[TaskHandle]

MAINT_PATH = "/dna/intent/api/v1/networkDevices/maintenanceSchedules"


def excluded_maintenance(http: CCHttp, tag_name: str) -> dict[str, dict]:
    out = {}
    for it in http.paged(MAINT_PATH, limit=500):
        sched = it.get("maintenanceSchedule") or {}
        for dev in it.get("networkDeviceIds") or []:
            out[dev] = {"mechanism": "maintenance", "ref": it.get("id"), "until_ms": sched.get("endTime"),
                        "description": it.get("description")}
    return out


def exclude_maintenance(http: CCHttp, device_ids: list[str], days: int, description: str,
                        tag_name: str) -> list[TaskHandle]:
    import time
    start = int(time.time() * 1000) + 60_000
    # One schedule per device, so reversing one device never re-includes others.
    return [_handle(http.request("POST", MAINT_PATH, json={
        "description": description[:200], "networkDeviceIds": [dev],
        "maintenanceSchedule": {"startTime": start, "endTime": start + days * 86_400_000, "recurrence": None}}))
        for dev in device_ids]


def include_maintenance(http: CCHttp, refs: list[str], tag_name: str) -> list[TaskHandle]:
    return [_handle(http.request("DELETE", f"{MAINT_PATH}/{ref}")) for ref in sorted(set(refs)) if ref]


def _tag_id(http: CCHttp, tag_name: str, create: bool) -> str | None:
    body = http.get("/dna/intent/api/v1/tag", {"name": tag_name})
    for it in (body or {}).get("response") or []:
        if it.get("name") == tag_name:
            return it.get("id")
    if not create:
        return None
    http.request("POST", "/dna/intent/api/v1/tag",
                 json={"name": tag_name, "description": "Excluded from assurance by AIOps", "systemTag": False})
    for _ in range(10):
        body = http.get("/dna/intent/api/v1/tag", {"name": tag_name})
        for it in (body or {}).get("response") or []:
            if it.get("name") == tag_name:
                return it.get("id")
    raise RuntimeError(f"tag {tag_name} was not created")


def excluded_tag(http: CCHttp, tag_name: str) -> dict[str, dict]:
    tag = _tag_id(http, tag_name, create=False)  # raises CCNotSupported when the tag API is absent
    if not tag:
        return {}
    body = http.get(f"/dna/intent/api/v1/tag/{tag}/member", {"memberType": "networkdevice"})
    return {m.get("instanceUuid") or m.get("id"): {"mechanism": "tag", "ref": tag, "until_ms": None,
                                                    "description": tag_name}
            for m in (body or {}).get("response") or [] if m.get("instanceUuid") or m.get("id")}


def exclude_tag(http: CCHttp, device_ids: list[str], days: int, description: str, tag_name: str) -> list[TaskHandle]:
    tag = _tag_id(http, tag_name, create=True)
    return [_handle(http.request("POST", f"/dna/intent/api/v1/tag/{tag}/member",
                                 json={"networkdevice": list(device_ids)}))]


def include_tag(http: CCHttp, device_ids: list[str], tag_name: str) -> list[TaskHandle]:
    tag = _tag_id(http, tag_name, create=False)
    if not tag:
        return []
    return [_handle(http.request("DELETE", f"/dna/intent/api/v1/tag/{tag}/member/{d}")) for d in device_ids]


OPERATIONS: dict[str, list[Candidate]] = {
    "get_sites": [
        Candidate("sites_v1", "intent v1 (new site design)", "GET /dna/intent/api/v1/sites", sites_v1,
                  min_version=NEW_SITE_API),
        Candidate("site_legacy", "intent v1 (legacy)", "GET /dna/intent/api/v1/site", sites_legacy),
    ],
    "get_devices": [
        Candidate("network_device_v1", "intent v1", "GET /dna/intent/api/v1/network-device", devices_v1),
    ],
    "get_devices_by_id": [
        Candidate("network_device_ids_v1", "intent v1", "GET /dna/intent/api/v1/network-device?id=...",
                  devices_by_id_v1),
    ],
    "get_site_assignments": [
        Candidate("assigned_to_site_v1", "intent v1 (new site design)",
                  "GET /dna/intent/api/v1/networkDevices/assignedToSite", assignments_v1,
                  min_version=NEW_SITE_API),
        Candidate("membership_legacy", "intent v1 (legacy)", "GET /dna/intent/api/v1/membership/{siteId}",
                  assignments_legacy),
    ],
    "get_links": [
        Candidate("physical_topology_v1", "intent v1", "GET /dna/intent/api/v1/topology/physical-topology",
                  links_physical_topology),
    ],
    "get_interfaces": [
        Candidate("interfaces_by_device_v1", "intent v1",
                  "GET /dna/intent/api/v1/interface/network-device/{deviceId}", interfaces_v1, per_object=True),
    ],
    "get_ap_config": [
        Candidate("ap_config_summary_v1", "intent v1",
                  "GET /dna/intent/api/v1/wireless/accesspoint-configuration/summary", ap_config_summary,
                  per_object=True),
    ],
    "get_ap_rf_neighbors": [
        Candidate("rf_neighbors_assurance", "data v1 (assurance)",
                  "GET /dna/data/api/v1/accessPoints/{id}/rfNeighbors", rf_neighbors_assurance,
                  min_version=(3, 1), verified=False, per_object=True),
    ],
    "assign_devices_to_site": [
        Candidate("assign_apply_v1", "intent v1 (new site design)",
                  "POST /dna/intent/api/v1/networkDevices/assignToSite/apply", assign_apply_v1,
                  min_version=NEW_SITE_API),
        Candidate("site_device_legacy", "system v1 (legacy, async execution)",
                  "POST /dna/system/api/v1/site/{siteId}/device", assign_legacy, max_version=(3, 0)),
    ],
    "task_status": [
        Candidate("tasks_v1", "intent v1 (/tasks)", "GET /dna/intent/api/v1/tasks/{id}", task_status_v1_tasks,
                  min_version=(2, 3, 7)),
        Candidate("task_legacy", "intent v1 (/task)", "GET /dna/intent/api/v1/task/{id}", task_status_legacy,
                  per_object=True),
    ],
    "execution_status": [
        Candidate("execution_status_v1", "dnacaap", "GET /dna/intent/api/v1/dnacaap/management/execution-status/{id}",
                  execution_status, per_object=True),
    ],
    "rename_aps": [
        Candidate("ap_config_v2", "intent v2", "POST /dna/intent/api/v2/wireless/accesspoint-configuration",
                  rename_aps_v2, min_version=(2, 3, 7), verified=False),
        Candidate("ap_config_v1", "intent v1", "POST /dna/intent/api/v1/wireless/accesspoint-configuration",
                  rename_aps_v1, verified=False),
    ],
    "set_device_role": [
        Candidate("network_device_brief_v1", "intent v1", "PUT /dna/intent/api/v1/network-device/brief",
                  set_role_brief_v1, verified=False),
    ],
    "sync_devices": [
        Candidate("network_device_sync_v1", "intent v1", "PUT /dna/intent/api/v1/network-device/sync",
                  sync_devices_v1, verified=False),
    ],
    "get_device_health": [
        Candidate("assurance_network_devices", "data v1 (assurance)", "GET /dna/data/api/v1/assuranceNetworkDevices",
                  health_assurance_devices, min_version=(2, 3, 7, 6), verified=False),
        Candidate("device_health_v1", "intent v1", "GET /dna/intent/api/v1/device-health", health_device_health_v1,
                  verified=False),
    ],
    "list_excluded": [
        Candidate("maintenance_v1", "intent v1 (maintenance mode)", f"GET {MAINT_PATH}", excluded_maintenance,
                  min_version=NEW_SITE_API, verified=False),
        Candidate("tag_v1", "intent v1 (tag fallback)", "GET /dna/intent/api/v1/tag/{id}/member", excluded_tag,
                  verified=False),
    ],
    "exclude_devices": [
        Candidate("maintenance_v1", "intent v1 (maintenance mode)", f"POST {MAINT_PATH}", exclude_maintenance,
                  min_version=NEW_SITE_API, verified=False),
        Candidate("tag_v1", "intent v1 (tag fallback)", "POST /dna/intent/api/v1/tag/{id}/member", exclude_tag,
                  verified=False),
    ],
}

# Reversal must use the mechanism that excluded the device, so it bypasses the resolver.
INCLUDE_BY_MECHANISM = {"maintenance": include_maintenance, "tag": include_tag}
