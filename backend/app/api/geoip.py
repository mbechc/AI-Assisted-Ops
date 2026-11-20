"""Optional GeoIP source: .mmdb upload, versions (activate / rollback) and location -> building mapping."""
import os
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.connectors import geoip
from app.deps import current_user, get_db, require
from app.models import DataSource, GeoipFile, GeoipMapping, User
from app.store import cache_get
from app.usecases.ap_site import sites_cache_key

router = APIRouter(prefix="/sources", tags=["geoip"])


def _geo_source(db: Session, source_id: int) -> DataSource:
    s = db.get(DataSource, source_id)
    if s is None or s.kind != "geoip":
        raise HTTPException(404, "GeoIP source not found")
    return s


def file_out(f: GeoipFile) -> dict:
    return {"id": f.id, "sha256": f.sha256, "filename": f.filename, "size": f.size, "uploaded_at": f.uploaded_at,
            "uploaded_by": f.uploaded_by, "active": f.active, "meta": f.meta}


@router.post("/{source_id}/upload")
async def upload(source_id: int, request: Request, filename: str = "upload.mmdb", activate: bool = True,
                 db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    """Raw body upload (Content-Type: application/octet-stream) of a MaxMind-format database."""
    s = _geo_source(db, source_id)
    fd, tmp = geoip.upload_tempfile(s.id)
    total = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in request.stream():
                total += len(chunk)
                if total > geoip.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "file too large")
                fh.write(chunk)
        gf = geoip.store_upload(db, s, filename, tmp, admin.username, activate=activate)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    audit(db, admin.username, "geoip.uploaded", "source", s.id, file=gf.filename, sha256=gf.sha256,
          size=gf.size, activated=activate)
    s.last_status = f"ok: {gf.filename} ({gf.meta.get('database_type')})"
    db.commit()
    return file_out(gf)


@router.get("/{source_id}/geoip")
def geoip_state(source_id: int, cc_source_id: int | None = None, db: Session = Depends(get_db),
                _=Depends(current_user)):
    s = _geo_source(db, source_id)
    files = db.scalars(select(GeoipFile).where(GeoipFile.source_id == s.id).order_by(GeoipFile.id.desc())).all()
    maps = db.scalars(select(GeoipMapping).where(GeoipMapping.source_id == s.id)
                      .order_by(GeoipMapping.location_key)).all()
    return {"source": {"id": s.id, "name": s.name, "enabled": s.enabled},
            "fields": geoip.geoip_fields(s), "default_fields": geoip.DEFAULT_FIELDS,
            "files": [file_out(f) for f in files],
            "mappings": [{"id": m.id, "location_key": m.location_key, "site_id": m.site_id,
                          "site_hierarchy": m.site_hierarchy, "created_by": m.created_by,
                          "created_at": m.created_at} for m in maps],
            "learning": learning_report(db, s.id, cc_source_id)}


def learning_report(db: Session, geo_source_id: int, cc_source_id: int | None) -> dict:
    """Per location key: how many devices with a site fall in each building, and the learned building."""
    from app.inventory import load
    from app.usecases.ap_site.learners import KeyBuildingModel, SiteIndex

    if cc_source_id is None:
        src = db.scalars(select(DataSource).where(DataSource.kind == "catalyst_center", DataSource.enabled.is_(True))
                         .order_by(DataSource.id)).first()
        cc_source_id = src.id if src else None
    provider = geoip.load_provider(db, geo_source_id)
    inv = load(db, cc_source_id) if cc_source_id else None
    if provider is None or inv is None:
        return {"cc_source_id": cc_source_id, "available": False, "devices": 0, "covered": 0, "keys": [],
                "reason": "no active GeoIP file" if provider is None else "no inventory snapshot yet"}
    try:
        index = SiteIndex(inv.sites)
        counts: dict[str, Counter] = defaultdict(Counter)
        unassigned: Counter = Counter()
        covered = 0
        for d in inv.devices:
            key = provider.location_key(d.mgmt_ip)
            if not key:
                continue
            covered += 1
            b = index.building_of(inv.assignments.get(d.id))
            if b:
                counts[key][b] += 1
            else:
                unassigned[key] += 1
        model = KeyBuildingModel(index)
        model.counts = counts
        keys = []
        for key in sorted(set(counts) | set(unassigned)):
            pred = model.predict(key)
            c = counts.get(key, Counter())
            keys.append({
                "location_key": key, "devices_with_site": sum(c.values()), "devices_without_site": unassigned[key],
                "top_buildings": [{"site_id": b, "site": index.label(b), "devices": n} for b, n in c.most_common(3)],
                "learned_site_id": pred.site_id if pred else None,
                "learned_site": index.label(pred.site_id) if pred else None,
                "purity": round(pred.purity, 2) if pred else None,
                "mapped_site_id": provider.mapping_for(key),
            })
        return {"cc_source_id": cc_source_id, "available": True, "devices": len(inv.devices), "covered": covered,
                "keys": keys}
    finally:
        provider.close()


class FieldsIn(BaseModel):
    fields: list[str]


@router.put("/{source_id}/geoip/fields")
def set_fields(source_id: int, body: FieldsIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    s = _geo_source(db, source_id)
    fields = [f.strip() for f in body.fields if f.strip()][:20]
    caps = dict(s.capabilities or {})
    caps["geoip"] = {**(caps.get("geoip") or {}), "fields": fields}
    s.capabilities = caps
    audit(db, admin.username, "geoip.fields_updated", "source", s.id, fields=fields)
    db.commit()
    return {"fields": geoip.geoip_fields(s)}


@router.post("/{source_id}/geoip/files/{file_id}/activate")
def activate(source_id: int, file_id: int, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    s = _geo_source(db, source_id)
    prev = geoip.active_file(db, s.id)
    try:
        gf = geoip.activate_file(db, s.id, file_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    audit(db, admin.username, "geoip.activated", "source", s.id, file=gf.filename, sha256=gf.sha256,
          previous=prev.sha256 if prev else None)
    db.commit()
    return file_out(gf)


@router.delete("/{source_id}/geoip/files/{file_id}")
def remove_file(source_id: int, file_id: int, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    s = _geo_source(db, source_id)
    try:
        geoip.delete_file(db, s.id, file_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(db, admin.username, "geoip.file_deleted", "source", s.id, file_id=file_id)
    db.commit()
    return {"ok": True}


class MappingIn(BaseModel):
    location_key: str
    site_id: str
    cc_source_id: int | None = None


@router.put("/{source_id}/geoip/mappings")
def put_mapping(source_id: int, body: MappingIn, db: Session = Depends(get_db),
                user: User = Depends(require("admin"))):
    s = _geo_source(db, source_id)
    hierarchy = ""
    if body.cc_source_id is not None:
        site = next((x for x in cache_get(db, sites_cache_key(body.cc_source_id)) or [] if x["id"] == body.site_id), None)
        if site is None:
            raise HTTPException(400, "unknown site")
        if site["type"] not in ("building", "floor"):
            raise HTTPException(400, "map a location to a building")
        hierarchy = site["hierarchy"]
    m = db.scalars(select(GeoipMapping).where(GeoipMapping.source_id == s.id,
                                              GeoipMapping.location_key == body.location_key)).first()
    if m is None:
        m = GeoipMapping(source_id=s.id, location_key=body.location_key[:255], site_id=body.site_id,
                         site_hierarchy=hierarchy, created_by=user.username)
        db.add(m)
    else:
        m.site_id, m.site_hierarchy, m.created_by = body.site_id, hierarchy, user.username
    audit(db, user.username, "geoip.mapping_set", "source", s.id, location_key=body.location_key,
          site_id=body.site_id, site=hierarchy)
    db.commit()
    return {"ok": True}


@router.delete("/{source_id}/geoip/mappings/{mapping_id}")
def delete_mapping(source_id: int, mapping_id: int, db: Session = Depends(get_db),
                   user: User = Depends(require("admin"))):
    s = _geo_source(db, source_id)
    m = db.get(GeoipMapping, mapping_id)
    if m is None or m.source_id != s.id:
        raise HTTPException(404, "not found")
    db.delete(m)
    audit(db, user.username, "geoip.mapping_deleted", "source", s.id, location_key=m.location_key)
    db.commit()
    return {"ok": True}
