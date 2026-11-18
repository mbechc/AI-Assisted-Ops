"""Shared inventory snapshot: collected once, read by every specialist.

The collector stores the current state of each device and appends a history row only when a
tracked field changes. Specialists read the snapshot and history instead of each pulling the full
inventory from Catalyst Center, and get history-based signals (hostname resets, flapping devices).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.connectors.catalyst_center import CatalystCenter
from app.connectors.catalyst_center.models import Device, Link, Site
from app.models import DeviceStateHistory, InventoryDevice, InventorySnapshot

TRACKED = ("hostname", "reachability", "collection_status", "site_id", "role", "mgmt_ip", "software_version",
           "platform")
SNAPSHOT_RETENTION_DAYS = 30
HISTORY_RETENTION_DAYS = 400
UNREACHABLE = ("unreachable",)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _ms_to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def is_unreachable(reachability: str | None) -> bool:
    return (reachability or "").strip().lower() in UNREACHABLE


def collect(db: Session, source_id: int, cc: CatalystCenter, log=lambda m: None) -> InventorySnapshot:
    sites = cc.get_sites()
    devices = cc.get_devices()
    assignments = cc.get_site_assignments(sites)
    try:
        links = cc.get_links()
    except Exception as exc:  # noqa: BLE001 - topology is optional for the snapshot
        log(f"topology unavailable: {exc}")
        links = []
    ts = now()
    existing = {d.device_id: d for d in db.scalars(select(InventoryDevice).where(InventoryDevice.source_id == source_id))}
    changes = 0
    seen: set[str] = set()
    for dev in devices:
        seen.add(dev.id)
        row = existing.get(dev.id)
        new = {
            "hostname": dev.hostname or "", "mgmt_ip": dev.mgmt_ip, "mac": dev.mac, "eth_mac": dev.eth_mac,
            "family": dev.family or "", "platform": dev.platform, "series": dev.series, "role": dev.role,
            "software_version": dev.software_version, "serial": dev.serial, "wlc_ip": dev.wlc_ip,
            "reachability": dev.reachability, "reachability_failure": dev.reachability_failure,
            "collection_status": dev.collection_status, "error_code": dev.error_code,
            "error_description": dev.error_description, "last_update_at": _ms_to_dt(dev.last_update_ms),
            "site_id": assignments.get(dev.id),
        }
        if row is None:
            row = InventoryDevice(source_id=source_id, device_id=dev.id, first_seen=ts,
                                  unreachable_since=ts if is_unreachable(dev.reachability) else None, **new)
            db.add(row)
        else:
            if row.removed_at is not None:
                db.add(DeviceStateHistory(source_id=source_id, device_id=dev.id, ts=ts, field="presence",
                                          old="removed", new="present"))
                row.removed_at = None
            for f in TRACKED:
                old_v, new_v = getattr(row, f), new[f]
                if (old_v or None) != (new_v or None):
                    db.add(DeviceStateHistory(source_id=source_id, device_id=dev.id, ts=ts, field=f,
                                              old=None if old_v is None else str(old_v),
                                              new=None if new_v is None else str(new_v)))
                    changes += 1
                    if f == "hostname" and old_v:
                        row.previous_hostname = old_v
                    if f == "reachability":
                        row.unreachable_since = ts if is_unreachable(new_v) else None
            if is_unreachable(new["reachability"]) and row.unreachable_since is None:
                row.unreachable_since = ts
            for k, v in new.items():
                setattr(row, k, v)
        row.last_seen = ts
    removed = 0
    for dev_id, row in existing.items():
        if dev_id not in seen and row.removed_at is None:
            row.removed_at = ts
            db.add(DeviceStateHistory(source_id=source_id, device_id=dev_id, ts=ts, field="presence",
                                      old="present", new="removed"))
            removed += 1
    aps = sum(1 for d in devices if d.is_ap)
    snap = InventorySnapshot(
        source_id=source_id, taken_at=ts,
        release=".".join(map(str, cc.version)) if cc.version else None,
        device_count=len(devices), ap_count=aps, sites=[s.to_dict() for s in sites],
        links=[[ln.a_id, ln.b_id, ln.a_port, ln.b_port] for ln in links],
        stats={"changes": changes, "removed": removed, "assigned": len(assignments), "links": len(links),
               "new": len(seen - set(existing))},
    )
    db.add(snap)
    cutoff = ts - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    db.execute(delete(InventorySnapshot).where(InventorySnapshot.source_id == source_id,
                                               InventorySnapshot.taken_at < cutoff)
               .execution_options(synchronize_session=False))
    db.execute(delete(DeviceStateHistory).where(DeviceStateHistory.ts < ts - timedelta(days=HISTORY_RETENTION_DAYS))
               .execution_options(synchronize_session=False))
    db.flush()
    log(f"Inventory snapshot: {len(devices)} devices ({aps} APs), {len(sites)} sites, {len(links)} links, "
        f"{changes} field changes, {snap.stats['new']} new, {removed} removed")
    return snap


def record_change(db: Session, source_id: int, device_id: str, field: str, value) -> None:
    """Apply a change the platform itself made in Catalyst Center, so the snapshot stays coherent."""
    row = db.get(InventoryDevice, (source_id, device_id))
    if row is None:
        return
    old = getattr(row, field)
    if (old or None) == (value or None):
        return
    db.add(DeviceStateHistory(source_id=source_id, device_id=device_id, ts=now(), field=field,
                              old=None if old is None else str(old), new=None if value is None else str(value)))
    if field == "hostname" and old:
        row.previous_hostname = old
    setattr(row, field, value)


# ------------------------------------------------------------------ readers

@dataclass
class Inventory:
    """What specialists work on: normalized models rebuilt from the latest snapshot."""
    snapshot: InventorySnapshot
    sites: list[Site]
    devices: list[Device]
    rows: dict[str, InventoryDevice]
    assignments: dict[str, str]
    links: list[Link]

    @property
    def age_hours(self) -> float:
        taken = self.snapshot.taken_at
        if taken.tzinfo is None:
            taken = taken.replace(tzinfo=timezone.utc)
        return (now() - taken).total_seconds() / 3600


def latest_snapshot(db: Session, source_id: int) -> InventorySnapshot | None:
    return db.scalars(select(InventorySnapshot).where(InventorySnapshot.source_id == source_id)
                      .order_by(InventorySnapshot.taken_at.desc(), InventorySnapshot.id.desc())).first()


def _device_from_row(r: InventoryDevice) -> Device:
    return Device(id=r.device_id, hostname=r.hostname, mgmt_ip=r.mgmt_ip, mac=r.mac, family=r.family,
                  platform=r.platform, serial=r.serial, reachability=r.reachability, role=r.role,
                  wlc_ip=r.wlc_ip, software_version=r.software_version, eth_mac=r.eth_mac, series=r.series,
                  collection_status=r.collection_status, reachability_failure=r.reachability_failure,
                  error_code=r.error_code, error_description=r.error_description)


def load(db: Session, source_id: int) -> Inventory | None:
    snap = latest_snapshot(db, source_id)
    if snap is None:
        return None
    rows = {r.device_id: r for r in db.scalars(select(InventoryDevice).where(
        InventoryDevice.source_id == source_id, InventoryDevice.removed_at.is_(None)))}
    return Inventory(
        snapshot=snap,
        sites=[Site(**s) for s in snap.sites],
        devices=[_device_from_row(r) for r in rows.values()],
        rows=rows,
        assignments={d: r.site_id for d, r in rows.items() if r.site_id},
        links=[Link(a_id=a, b_id=b, a_port=pa, b_port=pb) for a, b, pa, pb in snap.links],
    )


def ensure_fresh(ctx, max_age_hours: float = 3.0) -> Inventory:
    """Latest snapshot, collecting inline when there is none or it is too old (standalone runs, first run)."""
    inv = load(ctx.db, ctx.source.id)
    if inv is None or inv.age_hours > max_age_hours:
        ctx.log("Inventory snapshot missing or stale; collecting now")
        collect(ctx.db, ctx.source.id, ctx.cc, ctx.log)
        inv = load(ctx.db, ctx.source.id)
    else:
        ctx.log(f"Using inventory snapshot from {inv.snapshot.taken_at:%Y-%m-%d %H:%M} UTC "
                f"({inv.age_hours:.1f} h old, {len(inv.devices)} devices)")
    return inv


def history(db: Session, source_id: int, field: str, since: datetime,
            device_ids: set[str] | None = None) -> dict[str, list[DeviceStateHistory]]:
    q = select(DeviceStateHistory).where(DeviceStateHistory.source_id == source_id,
                                         DeviceStateHistory.field == field, DeviceStateHistory.ts >= since)
    out: dict[str, list[DeviceStateHistory]] = {}
    for h in db.scalars(q.order_by(DeviceStateHistory.ts)):
        if device_ids is None or h.device_id in device_ids:
            out.setdefault(h.device_id, []).append(h)
    return out
