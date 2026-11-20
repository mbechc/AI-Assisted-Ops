"""cache_set is an upsert: concurrent writers of one key never fail, and readers see the latest value."""
from app.db import Base, SessionLocal, engine
from app.store import cache_get, cache_set


def _fresh():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def test_two_sessions_write_the_same_new_key():
    _fresh()
    a, b = SessionLocal(), SessionLocal()
    assert cache_get(a, "k") is None and cache_get(b, "k") is None
    cache_set(a, "k", {"v": 1}, 60)
    a.commit()
    cache_set(b, "k", {"v": 2}, 60)
    b.commit()
    with SessionLocal() as c:
        assert cache_get(c, "k") == {"v": 2}
    a.close()
    b.close()


def test_overwrite_visible_in_the_same_session():
    _fresh()
    with SessionLocal() as s:
        cache_set(s, "k", [1], 60)
        assert cache_get(s, "k") == [1]
        cache_set(s, "k", [2], 60)
        assert cache_get(s, "k") == [2]


def test_expired_entry_is_not_returned():
    _fresh()
    with SessionLocal() as s:
        cache_set(s, "k", [1], -1)
        assert cache_get(s, "k") is None
