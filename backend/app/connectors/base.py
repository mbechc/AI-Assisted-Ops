"""Data-source connector registry.

Generation 1 implements Catalyst Center. The other kinds are registered so the
UI and task framework can reference them, but they cannot be enabled yet.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorKind:
    kind: str
    label: str
    implemented: bool
    description: str


CONNECTOR_KINDS: dict[str, ConnectorKind] = {
    k.kind: k
    for k in [
        ConnectorKind("catalyst_center", "Cisco Catalyst Center", True,
                      "Inventory, sites, topology, wireless and assurance via the Catalyst Center REST API (2.3.x and 3.x)."),
        ConnectorKind("netbox", "NetBox", False, "Source of truth for sites, racks, IPAM (planned)."),
        ConnectorKind("sdwan", "Cisco SD-WAN Manager", False, "WAN edge inventory and health (planned)."),
        ConnectorKind("cisco_iq", "Cisco IQ", False, "Advisories and lifecycle insights (planned)."),
        ConnectorKind("geoip", "Custom GeoIP (optional)", True,
                      "Uploaded MaxMind-format (.mmdb) database. Adds an extra location signal to the site tasks; "
                      "everything works without it."),
        ConnectorKind("devices", "Standalone Cisco devices", False, "Direct read-only device access for non-managed devices (planned)."),
    ]
}
