"""global autonomy cap defaults to L1 on fresh installs

Revision ID: 0004
Revises: 0003

The code default dropped from L3 to L1. Existing installs that never changed the cap get the old value
written explicitly, so an upgrade does not change their behaviour. On a fresh install this runs before any
user exists, so nothing is written and the new default applies.
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.JSON))


def upgrade() -> None:
    conn = op.get_bind()
    has_users = conn.execute(sa.text("SELECT 1 FROM users LIMIT 1")).first() is not None
    has_cap = conn.execute(sa.text("SELECT 1 FROM settings WHERE key = 'global_autonomy_cap'")).first() is not None
    if has_users and not has_cap:
        op.bulk_insert(settings, [{"key": "global_autonomy_cap", "value": {"v": 3}}])


def downgrade() -> None:
    pass
