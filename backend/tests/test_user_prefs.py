from fastapi.testclient import TestClient


def test_theme_is_saved_per_user():
    from app.db import Base, SessionLocal, engine
    from app.deps import current_user
    from app.main import app
    from app.models import User

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add_all([User(username="alice", password_hash="x", role="viewer"),
                    User(username="bob", password_hash="x", role="admin")])
        db.commit()

    def as_user(name):
        def dep():
            with SessionLocal() as db:
                return db.query(User).filter_by(username=name).one()
        app.dependency_overrides[current_user] = dep

    c = TestClient(app, headers={"X-AIOps": "1"})
    try:
        as_user("alice")
        assert c.get("/api/auth/me").json()["theme"] == "dark"
        r = c.put("/api/auth/me/preferences", json={"theme": "light"})
        assert r.status_code == 200 and r.json()["theme"] == "light"
        assert c.put("/api/auth/me/preferences", json={"theme": "pink"}).status_code == 422
        as_user("bob")
        assert c.get("/api/auth/me").json()["theme"] == "dark"
        as_user("alice")
        assert c.get("/api/auth/me").json()["theme"] == "light"
    finally:
        app.dependency_overrides.clear()
