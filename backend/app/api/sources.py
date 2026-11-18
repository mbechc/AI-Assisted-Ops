import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.connectors.base import CONNECTOR_KINDS
from app.connectors.catalyst_center import CapabilityUnavailable, CatalystCenter
from app.deps import current_user, get_db, require
from app.models import DataSource, User
from app.security import encrypt_secret
from app.sources import build_cc_client, save_capabilities
from app.store import cache_get
from app.usecases.ap_site import sites_cache_key

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceIn(BaseModel):
    name: str
    kind: str = "catalyst_center"
    base_url: str = ""
    username: str = ""
    password: str | None = None
    verify_tls: bool = False
    ca_bundle: str | None = None
    rate_limit_rps: int = 5
    enabled: bool = True


def source_out(s: DataSource) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "base_url": s.base_url, "username": s.username,
            "verify_tls": s.verify_tls, "has_ca_bundle": bool(s.ca_bundle), "rate_limit_rps": s.rate_limit_rps,
            "enabled": s.enabled, "detected_version": s.detected_version, "last_check_at": s.last_check_at,
            "last_status": s.last_status, "version_source": (s.capabilities or {}).get("version_source")}


@router.get("")
def list_sources(db: Session = Depends(get_db), _=Depends(current_user)):
    return [source_out(s) for s in db.scalars(select(DataSource).order_by(DataSource.id)).all()]


@router.post("")
def create_source(body: SourceIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    kind = CONNECTOR_KINDS.get(body.kind)
    if kind is None or not kind.implemented:
        raise HTTPException(400, f"connector '{body.kind}' is not available in this release")
    if body.kind == "catalyst_center" and not (body.password and body.base_url and body.username):
        raise HTTPException(400, "URL, username and password required")
    if db.scalars(select(DataSource).where(DataSource.name == body.name)).first():
        raise HTTPException(409, "a source with this name exists")
    s = DataSource(name=body.name, kind=body.kind, base_url=body.base_url.rstrip("/"), username=body.username,
                   secret_enc=encrypt_secret(body.password or ""), verify_tls=body.verify_tls,
                   ca_bundle=body.ca_bundle or None, rate_limit_rps=max(1, body.rate_limit_rps), enabled=body.enabled)
    db.add(s)
    db.flush()
    audit(db, admin.username, "source.created", "source", s.id, name=s.name, url=s.base_url)
    db.commit()
    return source_out(s)


@router.put("/{source_id}")
def update_source(source_id: int, body: SourceIn, db: Session = Depends(get_db),
                  admin: User = Depends(require("admin"))):
    s = db.get(DataSource, source_id)
    if s is None:
        raise HTTPException(404, "not found")
    url_changed = s.base_url != body.base_url.rstrip("/")
    s.name, s.base_url, s.username = body.name, body.base_url.rstrip("/"), body.username
    s.verify_tls, s.rate_limit_rps, s.enabled = body.verify_tls, max(1, body.rate_limit_rps), body.enabled
    if body.ca_bundle is not None:
        s.ca_bundle = body.ca_bundle or None
    if body.password:
        s.secret_enc = encrypt_secret(body.password)
    if url_changed:
        s.capabilities, s.detected_version = {}, None
    audit(db, admin.username, "source.updated", "source", s.id, name=s.name, url=s.base_url,
          password_changed=bool(body.password))
    db.commit()
    return source_out(s)


@router.delete("/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    s = db.get(DataSource, source_id)
    if s is None:
        raise HTTPException(404, "not found")
    db.delete(s)
    audit(db, admin.username, "source.deleted", "source", source_id, name=s.name)
    db.commit()
    return {"ok": True}


@router.post("/{source_id}/test")
def test_source(source_id: int, db: Session = Depends(get_db), user: User = Depends(require("operator"))):
    s = db.get(DataSource, source_id)
    if s is None:
        raise HTTPException(404, "not found")
    started = time.monotonic()
    if s.kind == "geoip":
        return _test_geoip(db, s, user, started)
    probes: list[dict] = []
    client: CatalystCenter | None = None
    try:
        client = build_cc_client(s)
        client._ops = {}  # full re-probe on explicit test
        client.connect(redetect=True)

        def probe(name: str, fn):
            t0 = time.monotonic()
            try:
                res = fn()
                probes.append({"probe": name, "ok": True, "result": res, "ms": int((time.monotonic() - t0) * 1000)})
            except CapabilityUnavailable as exc:
                probes.append({"probe": name, "ok": False, "result": str(exc), "ms": int((time.monotonic() - t0) * 1000)})
            except Exception as exc:  # noqa: BLE001
                probes.append({"probe": name, "ok": False, "result": f"error: {exc}", "ms": int((time.monotonic() - t0) * 1000)})

        sites = []
        devices = []

        def p_sites():
            sites.extend(client.get_sites())
            return f"{len(sites)} sites"

        def p_devices():
            devices.extend(client.get_devices())
            return f"{len(devices)} devices, {sum(d.is_ap for d in devices)} APs"

        probe("sites", p_sites)
        probe("devices", p_devices)
        floors = [x for x in sites if x.type in ("floor", "building")][:3]
        probe("site assignments (sample)", lambda: f"{len(client.get_site_assignments(floors))} members in {len(floors)} sites")
        probe("physical topology", lambda: f"{len(client.get_links())} links")
        aps = [d for d in devices if d.is_ap][:6]
        probe("AP configuration (sample)", lambda: f"{sum(1 for a in aps if client.get_ap_config(a))} of {len(aps)} APs returned data")
        probe("AP RF neighbours (sample)", lambda: f"{sum(1 for a in aps if client.get_ap_rf_neighbors(a))} of {len(aps)} APs returned data")
        status = "ok"
    except Exception as exc:  # noqa: BLE001
        status = f"error: {exc}"
    if client is not None:
        save_capabilities(db, s, client, status=status)
        report = client.capability_report()
        client.close()
    else:
        s.last_status = status
        report = []
    audit(db, user.username, "source.tested", "source", s.id, status=status, version=s.detected_version)
    db.commit()
    return {"status": status, "version": s.detected_version,
            "version_source": (s.capabilities or {}).get("version_source"),
            "elapsed_ms": int((time.monotonic() - started) * 1000), "probes": probes, "capabilities": report}


def _test_geoip(db: Session, s: DataSource, user: User, started: float) -> dict:
    from app.connectors import geoip
    provider = geoip.load_provider(db, s.id) if s.enabled else None
    if provider is None:
        status = "error: no active GeoIP file uploaded" if s.enabled else "error: source disabled"
        probes = [{"probe": "active file", "ok": False, "result": status, "ms": 0}]
    else:
        meta = provider.file.meta or {}
        status = "ok"
        probes = [{"probe": "active file", "ok": True, "ms": 0,
                   "result": f"{provider.file.filename} ({meta.get('database_type')}, "
                             f"{meta.get('sample_with_location')} of {meta.get('sample_networks')} sampled networks "
                             f"have a location key)"},
                  {"probe": "operator mappings", "ok": True, "result": str(len(provider.mappings)), "ms": 0}]
        provider.close()
    s.last_status = status
    s.last_check_at = datetime.now(timezone.utc)
    audit(db, user.username, "source.tested", "source", s.id, status=status)
    db.commit()
    return {"status": status, "version": None, "version_source": None,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "probes": probes, "capabilities": []}


@router.get("/{source_id}/capabilities")
def capabilities(source_id: int, db: Session = Depends(get_db), _=Depends(current_user)):
    s = db.get(DataSource, source_id)
    if s is None:
        raise HTTPException(404, "not found")
    c = CatalystCenter(s.base_url, s.username, "", capability_state=s.capabilities or {})
    try:
        return {"version": s.detected_version, "capabilities": c.capability_report()}
    finally:
        c.close()


@router.get("/{source_id}/sites")
def sites(source_id: int, type: str | None = None, db: Session = Depends(get_db), _=Depends(current_user)):
    data = cache_get(db, sites_cache_key(source_id)) or []
    if type:
        data = [x for x in data if x["type"] == type]
    return sorted(data, key=lambda x: x["hierarchy"])
