"""Release-independent models. Use cases only ever see these."""
from dataclasses import dataclass, field, asdict


@dataclass
class Site:
    id: str
    name: str
    parent_id: str | None
    hierarchy: str
    type: str  # global | area | building | floor

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Device:
    id: str
    hostname: str
    mgmt_ip: str | None
    mac: str | None
    family: str
    type: str | None = None
    platform: str | None = None
    serial: str | None = None
    reachability: str | None = None
    role: str | None = None
    wlc_ip: str | None = None
    software_version: str | None = None
    snmp_location: str | None = None
    eth_mac: str | None = None
    series: str | None = None
    collection_status: str | None = None
    reachability_failure: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    last_update_ms: int | None = None
    uptime: str | None = None

    @property
    def is_ap(self) -> bool:
        return (self.family or "").lower() == "unified ap"


@dataclass
class Link:
    a_id: str
    b_id: str
    a_port: str | None = None
    b_port: str | None = None


@dataclass
class Interface:
    device_id: str
    name: str
    speed_mbps: int | None = None
    duplex: str | None = None  # FullDuplex | HalfDuplex | AutoNegotiate
    status: str | None = None  # operational: up | down
    admin_status: str | None = None
    description: str | None = None


@dataclass
class PoePort:
    device_id: str
    name: str
    oper_status: str | None = None  # ON | OFF | FAULTY | POWER_DENY
    admin_status: str | None = None
    max_w: float | None = None
    allocated_w: float | None = None
    drawn_w: float | None = None
    pd_model: str | None = None
    pd_class: str | None = None


@dataclass
class ComplianceRow:
    device_id: str
    status: str  # COMPLIANT | NON_COMPLIANT | ...
    last_change_ms: int | None = None


@dataclass
class ApConfig:
    ap_id: str
    location: str | None = None
    site_tag: str | None = None
    policy_tag: str | None = None
    primary_wlc: str | None = None


@dataclass
class RfNeighbor:
    neighbor_id: str | None
    neighbor_name: str | None
    neighbor_mac: str | None
    rssi: float | None
    band: str | None = None


@dataclass
class TaskHandle:
    kind: str  # task | execution
    id: str
    url: str | None = None


@dataclass
class TaskResult:
    success: bool
    detail: str = ""
    raw: dict = field(default_factory=dict)
