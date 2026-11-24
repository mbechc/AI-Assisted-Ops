import logging
import time
from typing import Any

from app.connectors.catalyst_center.capabilities import INCLUDE_BY_MECHANISM, OPERATIONS, Candidate
from app.connectors.catalyst_center.http import CCHttp, CCNotSupported, Recorder
from app.connectors.catalyst_center.models import (
    ApConfig, ComplianceRow, Device, Interface, Link, PoePort, RfNeighbor, Site, TaskHandle, TaskResult,
)
from app.connectors.catalyst_center.version import Version, detect_release, format_version, parse_version

log = logging.getLogger(__name__)

PER_OBJECT_MISSES_BEFORE_UNSUPPORTED = 5


class CapabilityUnavailable(Exception):
    def __init__(self, op: str, version: Version | None):
        super().__init__(f"operation '{op}' is not available on Catalyst Center {format_version(version)}")
        self.op = op


class CatalystCenter:
    """Version-aware Catalyst Center facade returning normalized models."""

    def __init__(self, base_url: str, username: str, password: str, verify: bool | str = False,
                 rate_limit_rps: float = 5.0, capability_state: dict | None = None,
                 recorder: Recorder | None = None):
        self.http = CCHttp(base_url, username, password, verify=verify,
                           rate_limit_rps=rate_limit_rps, recorder=recorder)
        state = capability_state or {}
        self.version: Version | None = parse_version(state.get("version"))
        self.version_source: str = state.get("version_source", "")
        self._ops: dict[str, dict] = {op: dict(v) for op, v in (state.get("ops") or {}).items()}
        self._write_refused_at: str | None = state.get("write_refused_at")
        self._misses: dict[tuple[str, str], int] = {}
        self._hits: set[tuple[str, str]] = set()

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------ lifecycle

    def connect(self, redetect: bool = True) -> Version | None:
        self.http.authenticate()
        if redetect or self.version is None:
            version, source = detect_release(self.http)
            if version != self.version:
                self._ops = {}  # release changed (or first contact): re-probe everything
            self.version, self.version_source = version, source
        return self.version

    def capability_state(self) -> dict:
        if self.http.write_refused_at:
            self._write_refused_at = self.http.write_refused_at
        elif self.http.write_ok:
            self._write_refused_at = None
        return {
            "version": format_version(self.version) if self.version else None,
            "version_source": self.version_source,
            "ops": self._ops,
            "write_refused_at": self._write_refused_at,
        }

    def capability_report(self) -> list[dict]:
        rows = []
        for op, cands in OPERATIONS.items():
            st = self._ops.get(op, {})
            rows.append({
                "operation": op,
                "selected": st.get("selected"),
                "candidates": [{
                    "name": c.name, "api": c.api, "path": c.path, "verified": c.verified,
                    "applies": c.applies(self.version),
                    "status": (st.get("candidates") or {}).get(c.name, "untested" if c.applies(self.version) else "n/a"),
                } for c in cands],
            })
        return rows

    # ------------------------------------------------------------ resolver

    def _candidates(self, op: str) -> list[Candidate]:
        st = self._ops.get(op, {})
        statuses = st.get("candidates") or {}
        cands = [c for c in OPERATIONS[op] if c.applies(self.version) and statuses.get(c.name) != "unsupported"]
        selected = st.get("selected")
        cands.sort(key=lambda c: 0 if c.name == selected else 1)
        return cands

    def _mark(self, op: str, cand: str, status: str) -> None:
        st = self._ops.setdefault(op, {"selected": None, "candidates": {}})
        st.setdefault("candidates", {})[cand] = status
        if status == "ok":
            st["selected"] = cand
        elif st.get("selected") == cand:
            st["selected"] = None

    def call(self, op: str, *args: Any) -> Any:
        for cand in self._candidates(op):
            try:
                result = cand.fn(self.http, *args)
            except CCNotSupported:
                if cand.per_object:
                    key = (op, cand.name)
                    if key in self._hits:
                        return None  # endpoint exists, this object simply has no data
                    self._misses[key] = self._misses.get(key, 0) + 1
                    if self._misses[key] < PER_OBJECT_MISSES_BEFORE_UNSUPPORTED:
                        return None
                log.info("Catalyst Center %s: candidate %s unsupported on %s", op, cand.name,
                         format_version(self.version))
                self._mark(op, cand.name, "unsupported")
                continue
            self._hits.add((op, cand.name))
            self._mark(op, cand.name, "ok")
            return result
        raise CapabilityUnavailable(op, self.version)

    def supports(self, op: str) -> bool:
        return bool(self._candidates(op))

    # ------------------------------------------------------------ typed operations

    def get_sites(self) -> list[Site]:
        return self.call("get_sites")

    def get_devices(self) -> list[Device]:
        return self.call("get_devices")

    def get_devices_by_id(self, ids: list[str]) -> dict[str, Device]:
        return {d.id: d for d in self.call("get_devices_by_id", list(ids))} if ids else {}

    def get_site_assignments(self, sites: list[Site]) -> dict[str, str]:
        return self.call("get_site_assignments", sites)

    def get_links(self) -> list[Link]:
        return self.call("get_links")

    def get_interfaces(self, device_id: str) -> list[Interface] | None:
        return self.call("get_interfaces", device_id)

    def get_poe_ports(self, device_id: str) -> list[PoePort] | None:
        return self.call("get_poe_ports", device_id)

    def get_config_compliance(self, device_ids: list[str] | None = None) -> list[ComplianceRow]:
        return self.call("get_config_compliance", device_ids)

    def write_memory(self, device_ids: list[str]) -> TaskHandle:
        return self.call("write_memory", device_ids)

    def run_compliance(self, device_ids: list[str], categories: list[str]) -> TaskHandle:
        return self.call("run_compliance", device_ids, categories)

    def delete_device(self, device_id: str) -> TaskHandle:
        return self.call("delete_device", device_id)

    def get_ap_config(self, ap: Device) -> ApConfig | None:
        return self.call("get_ap_config", ap)

    def get_ap_rf_neighbors(self, ap: Device) -> list[RfNeighbor] | None:
        return self.call("get_ap_rf_neighbors", ap)

    def assign_devices_to_site(self, site: Site, devices: list[Device]) -> TaskHandle:
        return self.call("assign_devices_to_site", site, devices)

    def rename_aps(self, renames: list[tuple[Device, str]]) -> TaskHandle:
        return self.call("rename_aps", renames)

    def set_device_role(self, device: Device, role: str) -> TaskHandle:
        return self.call("set_device_role", device, role)

    def sync_devices(self, device_ids: list[str]) -> TaskHandle:
        return self.call("sync_devices", device_ids)

    def get_device_health(self) -> dict[str, dict]:
        return self.call("get_device_health")

    def list_excluded(self, tag_name: str) -> dict[str, dict]:
        return self.call("list_excluded", tag_name)

    def exclude_devices(self, device_ids: list[str], days: int, description: str, tag_name: str) -> list[TaskHandle]:
        return self.call("exclude_devices", device_ids, days, description, tag_name)

    def include_devices(self, excluded: dict[str, dict], tag_name: str) -> list[TaskHandle]:
        """Reverse exclusions with the mechanism that created each one."""
        handles: list[TaskHandle] = []
        maint_refs = [e["ref"] for e in excluded.values() if e.get("mechanism") == "maintenance"]
        by_tag: dict[str, list[str]] = {}
        for d, e in excluded.items():
            if e.get("mechanism") == "tag":
                by_tag.setdefault(e.get("description") or tag_name, []).append(d)
        if maint_refs:
            handles += INCLUDE_BY_MECHANISM["maintenance"](self.http, maint_refs, tag_name)
        for name, devices in by_tag.items():
            handles += INCLUDE_BY_MECHANISM["tag"](self.http, devices, name)
        return handles

    def wait_all(self, handles: list[TaskHandle], timeout: float = 600) -> TaskResult:
        results = [self.wait_for(h, timeout=timeout) for h in handles]
        failed = [r for r in results if not r.success]
        if failed:
            return TaskResult(False, "; ".join(r.detail for r in failed)[:500], failed[0].raw)
        return TaskResult(True, f"{len(results)} task(s) succeeded")

    def wait_for(self, handle: TaskHandle, timeout: float = 600, interval: float = 3) -> TaskResult:
        if not handle.id:
            return TaskResult(False, "Catalyst Center returned no task id")
        op = "task_status" if handle.kind == "task" else "execution_status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.call(op, handle)
            if result is not None:
                return result
            time.sleep(interval)
        return TaskResult(False, f"timed out after {timeout:.0f}s waiting for {handle.kind} {handle.id}")
