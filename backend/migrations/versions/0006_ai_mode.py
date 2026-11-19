"""work item AI provider (local or cloud)

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("work_items") as b:
        b.add_column(sa.Column("llm_provider", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("work_items") as b:
        b.drop_column("llm_provider")
