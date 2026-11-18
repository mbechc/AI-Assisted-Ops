"""Inventory snapshot status and the approved site-hierarchy baseline."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.deps import current_user, get_db, require
from app.inventory import latest_snapshot
from app.models import DataSource, InventorySnapshot, User
from app.usecases.site_hierarchy import active_baseline, approve_baseline

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _source(db: Session, source_id: int) -> DataSource:
    s = db.get(DataSource, source_id)
    if s is None or s.kind != "catalyst_center":
        raise HTTPException(404, "Catalyst Center source not found")
    return s


@router.get("/{source_id}/snapshots")
def snapshots(source_id: int, limit: int = 20, db: Session = Depends(get_db), _=Depends(current_user)):
    _source(db, source_id)
    rows = db.scalars(select(InventorySnapshot).where(InventorySnapshot.source_id == source_id)
                      .order_by(InventorySnapshot.taken_at.desc()).limit(min(limit, 200))).all()
    return [{"id": r.id, "taken_at": r.taken_at, "release": r.release, "devices": r.device_count, "aps": r.ap_count,
             "sites": len(r.sites or []), "stats": r.stats} for r in rows]


@router.get("/{source_id}/baseline")
def get_baseline(source_id: int, db: Session = Depends(get_db), _=Depends(current_user)):
    _source(db, source_id)
    b = active_baseline(db, source_id)
    snap = latest_snapshot(db, source_id)
    return {"baseline": {"id": b.id, "approved_at": b.approved_at, "approved_by": b.approved_by,
                         "sites": len(b.sites)} if b else None,
            "current": {"taken_at": snap.taken_at, "sites": len(snap.sites or [])} if snap else None}


@router.post("/{source_id}/baseline")
def post_baseline(source_id: int, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    _source(db, source_id)
    snap = latest_snapshot(db, source_id)
    if snap is None:
        raise HTTPException(400, "no inventory snapshot yet: run the inventory collector first")
    prev = active_baseline(db, source_id)
    b = approve_baseline(db, source_id, list(snap.sites or []), admin.username)
    audit(db, admin.username, "inventory.baseline_approved", "source", source_id, sites=len(b.sites),
          snapshot_id=snap.id, previous=prev.id if prev else None)
    db.commit()
    return {"id": b.id, "sites": len(b.sites), "approved_at": b.approved_at}
