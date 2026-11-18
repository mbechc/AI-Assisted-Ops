from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_url = get_settings().database_url
if _url in ("sqlite://", "sqlite:///:memory:"):
    # One shared in-memory database across threads (tests run endpoints in a thread pool).
    engine = create_engine(_url, poolclass=StaticPool, connect_args={"check_same_thread": False})
elif _url.startswith("sqlite"):
    engine = create_engine(_url)
else:
    engine = create_engine(_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
