"""inventory platform, series and serial as text

Revision ID: 0005
Revises: 0004

Catalyst Center joins the values of every stack member with commas, so these exceed any fixed length.
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

COLUMNS = ("platform", "series", "serial")


def upgrade() -> None:
    with op.batch_alter_table("inventory_devices") as b:
        for c in COLUMNS:
            b.alter_column(c, type_=sa.Text, existing_type=sa.String(128), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("inventory_devices") as b:
        for c in COLUMNS:
            b.alter_column(c, type_=sa.String(128), existing_type=sa.Text, existing_nullable=True,
                           postgresql_using=f"left({c}, 128)")
