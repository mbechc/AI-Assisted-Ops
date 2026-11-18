from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.inventory import collect, history, load
from app.models import DataSource, InventoryDevice
from app.security import encrypt_secret
from tests.conftest import cc_client


def test_snapshot_tracks_changes(mock_cc):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    src = DataSource(name="mock", base_url="http://mockcc", username="admin", secret_enc=encrypt_secret("mock"))
    db.add(src)
    db.flush()
    cc = cc_client(mock_cc)
    cc.connect()

    snap = collect(db, src.id, cc)
    assert snap.device_count == len(mock_cc.estate.devices)
    inv = load(db, src.id)
    assert len(inv.devices) == snap.device_count
    assert inv.assignments == mock_cc.estate.assignments

    ap = next(d for d in mock_cc.estate.devices if d["family"] == "Unified AP")
    old_name = ap["hostname"]
    ap["hostname"] = "aabb.ccdd.eeff"
    gone = mock_cc.estate.devices.pop()
    collect(db, src.id, cc)

    row = db.get(InventoryDevice, (src.id, ap["id"]))
    assert row.hostname == "aabb.ccdd.eeff" and row.previous_hostname == old_name
    removed = db.get(InventoryDevice, (src.id, gone["id"]))
    assert removed.removed_at is not None
    from datetime import datetime, timedelta, timezone
    h = history(db, src.id, "hostname", datetime.now(timezone.utc) - timedelta(hours=1))
    assert h[ap["id"]][0].old == old_name
    assert gone["id"] not in {d.id for d in load(db, src.id).devices}
    assert db.scalars(select(InventoryDevice)).first() is not None
    db.close()
