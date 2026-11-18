"""inventory snapshot, device history, site baselines, optional GeoIP, worker heartbeats

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def _source_fk(pk: bool = False) -> sa.Column:
    return sa.Column("source_id", sa.Integer, sa.ForeignKey("data_sources.id", ondelete="CASCADE"),
                     nullable=False, primary_key=pk)


def upgrade() -> None:
    op.create_table(
        "inventory_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        _source_fk(),
        sa.Column("taken_at", TZ, nullable=False),
        sa.Column("release", sa.String(64), nullable=True),
        sa.Column("device_count", sa.Integer, nullable=False),
        sa.Column("ap_count", sa.Integer, nullable=False),
        sa.Column("sites", sa.JSON, nullable=False),
        sa.Column("links", sa.JSON, nullable=False),
        sa.Column("stats", sa.JSON, nullable=False),
    )
    op.create_index("ix_inventory_snapshots_source_id", "inventory_snapshots", ["source_id"])
    op.create_index("ix_inventory_snapshots_taken_at", "inventory_snapshots", ["taken_at"])

    op.create_table(
        "inventory_devices",
        _source_fk(pk=True),
        sa.Column("device_id", sa.String(64), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("mgmt_ip", sa.String(64), nullable=True),
        sa.Column("mac", sa.String(32), nullable=True),
        sa.Column("eth_mac", sa.String(32), nullable=True),
        sa.Column("family", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(128), nullable=True),
        sa.Column("series", sa.String(128), nullable=True),
        sa.Column("role", sa.String(32), nullable=True),
        sa.Column("software_version", sa.String(64), nullable=True),
        sa.Column("serial", sa.String(128), nullable=True),
        sa.Column("wlc_ip", sa.String(64), nullable=True),
        sa.Column("reachability", sa.String(32), nullable=True),
        sa.Column("reachability_failure", sa.Text, nullable=True),
        sa.Column("collection_status", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_description", sa.Text, nullable=True),
        sa.Column("last_update_at", TZ, nullable=True),
        sa.Column("site_id", sa.String(64), nullable=True),
        sa.Column("first_seen", TZ, nullable=False),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("removed_at", TZ, nullable=True),
        sa.Column("unreachable_since", TZ, nullable=True),
        sa.Column("previous_hostname", sa.String(255), nullable=True),
    )
    op.create_index("ix_inventory_devices_mgmt_ip", "inventory_devices", ["mgmt_ip"])
    op.create_index("ix_inventory_devices_site_id", "inventory_devices", ["site_id"])

    op.create_table(
        "device_state_history",
        sa.Column("id", sa.Integer, primary_key=True),
        _source_fk(),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("old", sa.Text, nullable=True),
        sa.Column("new", sa.Text, nullable=True),
    )
    op.create_index("ix_device_state_history_dev", "device_state_history", ["source_id", "device_id", "ts"])
    op.create_index("ix_device_state_history_ts", "device_state_history", ["ts"])

    op.create_table(
        "site_baselines",
        sa.Column("id", sa.Integer, primary_key=True),
        _source_fk(),
        sa.Column("approved_at", TZ, nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=False),
        sa.Column("sites", sa.JSON, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
    )
    op.create_index("ix_site_baselines_source_id", "site_baselines", ["source_id"])

    op.create_table(
        "geoip_files",
        sa.Column("id", sa.Integer, primary_key=True),
        _source_fk(),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("uploaded_at", TZ, nullable=False),
        sa.Column("uploaded_by", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("meta", sa.JSON, nullable=False),
    )
    op.create_index("ix_geoip_files_source_id", "geoip_files", ["source_id"])

    op.create_table(
        "geoip_mappings",
        sa.Column("id", sa.Integer, primary_key=True),
        _source_fk(),
        sa.Column("location_key", sa.String(255), nullable=False),
        sa.Column("site_id", sa.String(64), nullable=False),
        sa.Column("site_hierarchy", sa.String(512), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
    )
    op.create_index("ux_geoip_mappings_key", "geoip_mappings", ["source_id", "location_key"], unique=True)

    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("hostname", sa.String(128), nullable=False),
        sa.Column("queues", sa.JSON, nullable=False),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("restarts", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    for t in ("worker_heartbeats", "geoip_mappings", "geoip_files", "site_baselines", "device_state_history",
              "inventory_devices", "inventory_snapshots"):
        op.drop_table(t)
