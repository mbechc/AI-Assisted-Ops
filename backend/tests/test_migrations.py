"""Fresh installs run the migrations, tests use create_all: both must produce the same schema."""
import os
import subprocess
import sys
import tempfile

from sqlalchemy import create_engine

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_migrations_match_models():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db import Base
    import app.models  # noqa: F401

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{tmp}/m.db"
        env = {**os.environ, "DATABASE_URL_OVERRIDE": url}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, check=True,
                       capture_output=True)
        engine = create_engine(url)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        engine.dispose()
    assert diff == [], diff
