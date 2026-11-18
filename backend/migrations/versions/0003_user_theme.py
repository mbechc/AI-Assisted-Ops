"""per-user colour theme

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("theme", sa.String(16), nullable=False, server_default="dark"))


def downgrade() -> None:
    op.drop_column("users", "theme")
