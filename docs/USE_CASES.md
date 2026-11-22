# Use cases: removing the repetitive 80%

AIOps exists to take the repetitive, boring 80% of Catalyst Center operations off operators, so they have
time for the complex 20%. This page collects the recurring work operators describe in the
[Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/bd-p/discussions-dna) and on Reddit
([r/networking](https://www.reddit.com/r/networking/), [r/Cisco](https://www.reddit.com/r/Cisco/)), and how
AIOps removes it.

Every use case follows the same rules:

- **Catalyst Center API only.** No SSH, NETCONF or Command Runner against WLCs, APs or switches. Changes go
  through Catalyst Center, so they are audited there too.
- **Expert Task first.** Rules and evidence from Catalyst Center make the proposal; AI is optional and only
  adjusts confidence or wording (see [How AI is implemented](AI_USAGE.md)).
- **Autonomy is earned.** New tasks start as L0 (report) or L1 (propose, operator approves). Higher levels
  are enabled per task once the calibration shows the proposals are right.

## Already in AIOps

| Expert Task | Repetitive work it removes |
|---|---|
| AP site assignment | Placing hundreds of new or unassigned APs in the right building and floor by hand |
| Wrong-site detection | Finding APs that sit on a floor in the wrong building (moved, swapped or mis-claimed) |
| AP hostname reset | Spotting APs that lost their name after a factory reset and restoring the naming convention |
| Site hierarchy drift | Checking that nobody renamed, deleted or recreated sites, areas and floors |
| Expected-down and flapping devices | Separating known-dead devices and flappers from real outages, so alerts mean something |
| Telemetry baseline | Finding devices with credential, SNMP or sync failures and writing the owner ticket |
| Device role correction | Fixing switch roles (access, distribution, core) that topology and Assurance depend on |
| AP uplink health | Checking every AP-facing switch port for half duplex or 100 Mbit (see below) |

## Backlog, ranked by value

| # | Use case | Level | Value | Effort |
|---|---|---|---|---|
| 1 | [AP uplink health](#1-ap-uplink-health) | L0 report | High | Low (built) |
| 2 | [AP power shortfall](#2-ap-power-shortfall) | L0 report | High | Medium |
| 3 | [Repeat err-disabled ports](#3-repeat-err-disabled-ports) | L0 report | High | Medium |
| 4 | [Alert-noise tuning](#4-alert-noise-tuning) | L1 propose | High | Medium |
| 5 | [Unsaved configuration](#5-unsaved-configuration) | L1 propose | Medium | Low |
| 6 | [AP floor-map hygiene](#6-ap-floor-map-hygiene) | L0 report | Medium | Medium |
| 7 | [Stale inventory](#7-stale-inventory) | L1 propose | Medium | Low |
| 8 | [Port and VLAN hygiene](#8-port-and-vlan-hygiene) | L0 report | Medium | Medium |

Out of scope: security advisories and end-of-life reporting. Cisco IQ covers them, so AIOps does not
duplicate them.

### 1. AP uplink health

**What operators say.** A WiFi 6 rollout with 9115 APs on Catalyst 9300 switches had "high latency, low
speed" everywhere although "everything that we set should be in place"; the cause was found only in the
switch log: `%CDP-4-DUPLEX_MISMATCH ... AP08 GigabitEthernet0 (half duplex)`
([Cisco Community](https://community.cisco.com/t5/wireless/high-latency-using-9115-ap/td-p/4037730)).
Another site hard-coded the switch port to full duplex while the AP auto-negotiated, which gives half
duplex: "it's pure luck if another device works"
([Cisco Community](https://community.cisco.com/t5/wireless/hi-in-my-access-point-i-am-unable-to-change-wired-0-duplex/td-p/4908923)).
Cisco's own wireless troubleshooting tool warns on "AP interface on half duplex" and "AP interface on
100 Mbps" ([DevNet](https://developer.cisco.com/docs/wireless-troubleshooting-tools/checks-available-in-tool/)).

**Why it is hard.** The port shows `connected`, the AP joins, Catalyst Center and the WLC look healthy;
users only report "slow WiFi" in one area. Finding it means checking every AP-facing port.

**How AIOps does it.** The topology gives each AP's switch and port; the interface API
(`GET /dna/intent/api/v1/interface/network-device/{id}`, fields `speed` and `duplex`) gives the port state.
Half duplex, 10 Mbps or 100 Mbps on an AP-facing port becomes a report item, grouped per switch, with the
fix: both sides on auto, or both hard-coded; then the cable and the AP. Read-only.

### 2. AP power shortfall

**What operators say.** A 9130AXI on 30 W without LLDP/CDP power negotiation "still sits in MIMO 1x1"
([Cisco Community](https://community.cisco.com/t5/wireless/power-for-a-cisco-9130axi/m-p/5553365)); weak
signal from 9130s turned out to be the PoE draw and the USB port
([Cisco Community](https://community.cisco.com/t5/wireless/is-this-device-issue-or-configuration-for-ap-signal-strength/td-p/4399414)).
Cisco TAC lists APs on too little PoE as a common issue: radios fall back or stay off
([Cisco](https://www.cisco.com/c/en/us/support/docs/wireless/catalyst-9100-access-points/215467-cisco-access-point-power-requirements-qu.html)).

**How AIOps does it.** Compare each AP model's full-power requirement with the power the switch port
allocates (PoE data in Catalyst Center Assurance, 2.3.6 and later). Report APs running derated, with the
options: UPOE port, disable the AP USB port, or an injector.

### 3. Repeat err-disabled ports

**What operators say.** "I get an alert when it happens, open a ticket with the network team, they reenable
the port and close the ticket, it stays up for a day or two and then err disables again ... it's been going
on for two weeks" ([r/networking](https://www.reddit.com/r/networking/comments/kurgei/esxi_host_network_err_disabled/));
the same loop for lab ports ([r/networking](https://www.reddit.com/r/networking/comments/1phcm92/switch_port_keeps_getting_errordisabled_whats_the/)).

**How AIOps does it.** Track interface state across snapshots; ports that go err-disabled repeatedly become
one item with the count, the cause (BPDU guard, port security, inline power) and the root-cause advice, instead of
one ticket per occurrence.

### 4. Alert-noise tuning

**What operators say.** "DNAC sent me about 1,000+ emails today about the hot router"
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-alerting-via-email-for-events/td-p/4454455));
APs in mobile MRI trailers alert every night
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/exclude-ap-s-from-certain-dna-center-alerts/td-p/5209243));
"Device Unreachable" alerts that clear within a minute
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/excessive-device-unreachable-notifications/td-p/5337376)).

**How AIOps does it.** Extends Expected-down and flapping devices: find issues that fire daily for the same
devices or sites, and propose an Assurance network profile or a priority change for those sites (Catalyst
Center supports both). The operator approves; nothing is silenced automatically.

### 5. Unsaved configuration

**What operators say.** Startup and running configuration out of sync, then "Run Compliance, wait about 10
minutes ... re-provision" ([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-compliance-error-startup-vs-running-configuratio/m-p/4283918)).

**How AIOps does it.** Devices non-compliant on "Startup vs Running" for more than N days, with who changed
what and when (Catalyst Center keeps the change history). Propose "write running config to startup"
through Catalyst Center; the operator approves.

### 6. AP floor-map hygiene

**What operators say.** "You must place the access points manually on the floorplan"
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-heatmap-query/td-p/5259681));
"upgraded DNA center and it wiped my maps"
([r/networking](https://www.reddit.com/r/networking/comments/1gebtt9/has_anyone_moved_away_from_cisco_dna_center_while/)).

**How AIOps does it.** APs assigned to a floor but not positioned on its map
(`GET /dna/intent/api/v2/floors/{floorId}/accessPointPositions`), and the placed count per floor over time,
so a drop after a Catalyst Center upgrade is caught the same day.

### 7. Stale inventory

**What operators say.** Unreachable devices that cannot be deleted, sometimes only with TAC
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dna-center-seeking-help-to-delete-a-device-that-is-not/td-p/3792173),
[Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-2-1-2-5-cannot-delete-a-device-from-inventory/td-p/4424016)).

**How AIOps does it.** Devices unreachable for more than N days and not on the expected-down list, proposed
for removal with the clean-up steps that work (clear port assignments, remove from fabric, delete).

### 8. Port and VLAN hygiene

**What operators say.** Daily work is still manual port and VLAN changes and audits
([r/networking](https://www.reddit.com/r/networking/comments/1e6k1uf/automation_tools_for_cisco_enterprise_networks/)).

**How AIOps does it.** Access ports whose VLAN or description does not match the site standard, for example
AP-facing ports outside the AP VLAN or ports without a description, from the interface API
(`vlanId`, `description`, `portMode`).
