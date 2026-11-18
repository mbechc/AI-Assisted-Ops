"""initial schema (frozen as of release 0.1.0)

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("last_login", TZ, nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("verify_tls", sa.Boolean, nullable=False),
        sa.Column("ca_bundle", sa.Text, nullable=True),
        sa.Column("rate_limit_rps", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("detected_version", sa.String(64), nullable=True),
        sa.Column("capabilities", sa.JSON, nullable=False),
        sa.Column("last_check_at", TZ, nullable=True),
        sa.Column("last_status", sa.Text, nullable=True),
        sa.Column("created_at", TZ, nullable=False),
    )

    op.create_table(
        "task_configs",
        sa.Column("task_key", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("cron", sa.String(64), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("autonomy_level", sa.Integer, nullable=False),
        sa.Column("autonomy_cap", sa.Integer, nullable=False),
        sa.Column("auto_threshold", sa.Integer, nullable=False),
        sa.Column("max_auto_per_run", sa.Integer, nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("last_scheduled_at", TZ, nullable=True),
        sa.Column("updated_at", TZ, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )

    op.create_table(
        "task_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("triggered_by", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("finished_at", TZ, nullable=True),
        sa.Column("stats", sa.JSON, nullable=False),
        sa.Column("log", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", TZ, nullable=False),
    )
    op.create_index("ix_task_runs_task_key", "task_runs", ["task_key"])

    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("first_run_id", sa.Integer, nullable=True),
        sa.Column("last_run_id", sa.Integer, nullable=True),
        sa.Column("target_key", sa.String(255), nullable=False),
        sa.Column("target_label", sa.String(255), nullable=False),
        sa.Column("group_label", sa.String(255), nullable=True),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("confidence", sa.Integer, nullable=False),
        sa.Column("rule_confidence", sa.Integer, nullable=False),
        sa.Column("llm_confidence", sa.Integer, nullable=True),
        sa.Column("llm_status", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("concerns", sa.JSON, nullable=False),
        sa.Column("blocked_reason", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("decided_at", TZ, nullable=True),
        sa.Column("decision_note", sa.Text, nullable=True),
        sa.Column("override_payload", sa.JSON, nullable=True),
        sa.Column("executed_at", TZ, nullable=True),
        sa.Column("exec_result", sa.JSON, nullable=True),
        sa.Column("verify_status", sa.String(16), nullable=True),
        sa.Column("outcome", sa.JSON, nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_index("ix_work_items_task_status", "work_items", ["task_key", "status"])
    op.create_index("ix_work_items_task_target", "work_items", ["task_key", "target_key"])
    op.create_index("ix_work_items_group_label", "work_items", ["group_label"])
    op.create_index("ix_work_items_confidence", "work_items", ["confidence"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=True),
        sa.Column("object_id", sa.String(128), nullable=True),
        sa.Column("details", sa.JSON, nullable=False),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
    )

    op.create_table(
        "cache_entries",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
    )
    op.create_index("ix_cache_entries_expires_at", "cache_entries", ["expires_at"])


def downgrade() -> None:
    for t in ("cache_entries", "settings", "audit_log", "work_items", "task_runs", "task_configs",
              "data_sources", "users"):
        op.drop_table(t)
