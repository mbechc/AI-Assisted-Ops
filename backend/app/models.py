from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


OPEN_ITEM_STATUSES = ("proposed", "approved", "executing")
DECIDED_ITEM_STATUSES = ("approved", "rejected", "executing", "done", "failed")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer | operator | admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    theme: Mapped[str] = mapped_column(String(16), default="dark", server_default="dark")  # dark | light


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32), default="catalyst_center")
    base_url: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(128))
    secret_enc: Mapped[str] = mapped_column(Text)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=False)
    ca_bundle: Mapped[str | None] = mapped_column(Text, nullable=True)
    rate_limit_rps: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    detected_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskConfig(Base):
    """Operator-editable configuration for a registered TaskDefinition."""

    __tablename__ = "task_configs"

    task_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cron: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True)
    autonomy_level: Mapped[int] = mapped_column(Integer, default=1)
    autonomy_cap: Mapped[int] = mapped_column(Integer, default=1)
    auto_threshold: Mapped[int] = mapped_column(Integer, default=95)
    max_auto_per_run: Mapped[int] = mapped_column(Integer, default=50)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_key: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")
    triggered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|success|failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_items_task_status", "task_key", "status"),
        Index("ix_work_items_task_target", "task_key", "target_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_key: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True)
    first_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_key: Mapped[str] = mapped_column(String(255))
    target_label: Mapped[str] = mapped_column(String(255))
    group_label: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[int] = mapped_column(Integer, default=0, index=True)
    rule_confidence: Mapped[int] = mapped_column(Integer, default=0)
    llm_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_status: Mapped[str] = mapped_column(String(16), default="skipped")  # skipped|ok|error|unavailable
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    concerns: Mapped[list] = mapped_column(JSON, default=list)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    # proposed|reported|approved|rejected|executing|done|failed|superseded
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exec_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verify_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ok|failed
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict | list] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ------------------------------------------------------------------ inventory snapshot (0002)

class InventorySnapshot(Base):
    """One collection pass of the shared collector; also holds the site hierarchy as it was."""

    __tablename__ = "inventory_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_count: Mapped[int] = mapped_column(Integer, default=0)
    ap_count: Mapped[int] = mapped_column(Integer, default=0)
    sites: Mapped[list] = mapped_column(JSON, default=list)
    links: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class InventoryDevice(Base):
    """Latest known state of every device per source (upserted by the collector)."""

    __tablename__ = "inventory_devices"

    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    mgmt_ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mac: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eth_mac: Mapped[str | None] = mapped_column(String(32), nullable=True)
    family: Mapped[str] = mapped_column(String(64), default="")
    platform: Mapped[str | None] = mapped_column(String(128), nullable=True)
    series: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    software_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wlc_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reachability: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reachability_failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    collection_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    site_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unreachable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)


class DeviceStateHistory(Base):
    """Appended only when a tracked field changes, so history stays compact."""

    __tablename__ = "device_state_history"
    __table_args__ = (Index("ix_device_state_history_dev", "source_id", "device_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    device_id: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    field: Mapped[str] = mapped_column(String(32))
    old: Mapped[str | None] = mapped_column(Text, nullable=True)
    new: Mapped[str | None] = mapped_column(Text, nullable=True)


class SiteBaseline(Base):
    """Operator-approved site hierarchy that drift is measured against."""

    __tablename__ = "site_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str] = mapped_column(String(64))
    sites: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ------------------------------------------------------------------ optional GeoIP source (0002)

class GeoipFile(Base):
    __tablename__ = "geoip_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class GeoipMapping(Base):
    """GeoIP location key -> Catalyst Center building/floor, set by an operator."""

    __tablename__ = "geoip_mappings"
    __table_args__ = (Index("ux_geoip_mappings_key", "source_id", "location_key", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    location_key: Mapped[str] = mapped_column(String(255))
    site_id: Mapped[str] = mapped_column(String(64))
    site_hierarchy: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerHeartbeat(Base):
    """Last time each specialist worker was seen; survives Redis restarts for the Runners page."""

    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(128), default="")
    queues: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
