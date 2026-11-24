import json

from fastapi.testclient import TestClient

RECORD = {"terms_version": 1, "sha256": "ab" * 32, "accepted_at": "2026-11-24T10:00:00Z", "accepted_by": "morten",
          "host": "steward", "steward_version": "2026.11.2-dev.2", "method": "typed"}


def test_terms_acceptance_audited_once_and_served(tmp_path, monkeypatch):
    from app import terms
    from app.config import get_settings
    from app.db import Base, SessionLocal, engine
    from app.deps import current_user
    from app.main import app
    from app.models import AuditLog, User

    path = tmp_path / "acceptance.json"
    monkeypatch.setattr(get_settings(), "terms_file", str(path))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(username="vic", password_hash="x", role="viewer"))
        db.commit()

    def viewer():
        with SessionLocal() as db:
            return db.query(User).filter_by(username="vic").one()

    app.dependency_overrides[current_user] = viewer
    c = TestClient(app, headers={"X-Steward": "1"})
    try:
        assert terms.acceptance() is None
        assert c.get("/api/terms").json() == {"accepted": False}
        with SessionLocal() as db:
            terms.audit_acceptance(db)
            db.commit()
            assert db.query(AuditLog).count() == 0

        path.write_text(json.dumps(RECORD))
        assert c.get("/api/terms").json() == {"accepted": True, **RECORD}
        for _ in range(2):
            with SessionLocal() as db:
                terms.audit_acceptance(db)
                db.commit()
        with SessionLocal() as db:
            rows = db.query(AuditLog).filter_by(action="terms.accepted").all()
            assert len(rows) == 1 and rows[0].actor == "morten" and rows[0].object_id == "v1:2026-11-24T10:00:00Z"

        path.write_text("not json")
        assert terms.acceptance() is None
    finally:
        app.dependency_overrides.clear()
