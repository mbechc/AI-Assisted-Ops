# Use cases: removing the repetitive 80%

Steward exists to take the repetitive, boring 80% of Catalyst Center operations off operators, so they have
time for the complex 20%. This page collects the recurring work operators describe in the
[Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/bd-p/discussions-dna) and on Reddit
([r/networking](https://www.reddit.com/r/networking/), [r/Cisco](https://www.reddit.com/r/Cisco/)), and how
Steward removes it.

Every use case follows the same rules:

- **Catalyst Center API only.** No SSH, NETCONF or Command Runner against WLCs, APs or switches. Changes go
  through Catalyst Center, so they are audited there too.
- **Expert Task first.** Rules and evidence from Catalyst Center make the proposal; AI is optional and only
  adjusts confidence or wording (see [How AI is implemented](AI_USAGE.md)).
- **Autonomy is earned.** New tasks start as L0 (report) or L1 (propose, operator approves). Higher levels
  are enabled per task once the calibration shows the proposals are right.

## Already in Steward

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
| AP power shortfall | Finding APs that run with radios off because their switch port gives too little PoE |
| Repeat err-disabled ports | The re-enable, close ticket, err-disabled again loop on ports to APs and switches |
| Unsaved configuration | Chasing devices where someone never saved the running configuration |
| Stale inventory | Cleaning out devices that have been unreachable for months |

## Backlog, ranked by value

| # | Use case | Level | Value | Effort |
|---|---|---|---|---|
| 1 | [AP uplink health](#1-ap-uplink-health) | L0 report | High | Low (built) |
| 2 | [AP power shortfall](#2-ap-power-shortfall) | L0 report | High | Medium (built) |
| 3 | [Repeat err-disabled ports](#3-repeat-err-disabled-ports) | L0 report | High | Medium (built) |
| 4 | [Alert-noise tuning](#4-alert-noise-tuning) | L1 propose | High | Medium |
| 5 | [Unsaved configuration](#5-unsaved-configuration) | L1 propose | Medium | Low (built) |
| 6 | [AP floor-map hygiene](#6-ap-floor-map-hygiene) | L0 report | Medium | Medium |
| 7 | [Stale inventory](#7-stale-inventory) | L1 propose | Medium | Low (built) |
| 8 | [Port and VLAN hygiene](#8-port-and-vlan-hygiene) | L0 report | Medium | Medium |

Next up: alert-noise tuning, then AP floor-map hygiene and port and VLAN hygiene.

Out of scope: security advisories and end-of-life reporting. Cisco IQ covers them, so Steward does not
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

**How Steward does it.** The topology gives each AP's switch and port; the interface API
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

**How Steward does it.** The topology gives each AP's switch and port; the PoE API
(`GET /dna/intent/api/v1/network-device/{id}/interface/poe-detail`, field `allocatedPower`) gives the power the
port allocates. Steward compares it with the power the AP model needs for full function, from a built-in table
per model prefix (802.3af 15.4 W, 802.3at 30 W, 802.3bt 45 or 60 W). Check the table against the data sheets of
the models you run and override entries in the task parameters (for example `C9130=30`). Ports where the switch
denies power are reported too. The report, grouped per switch, gives the options: UPOE/802.3bt port, LLDP on the
port, an injector, or disabling the AP USB port. Read-only.

### 3. Repeat err-disabled ports

**What operators say.** "I get an alert when it happens, open a ticket with the network team, they reenable
the port and close the ticket, it stays up for a day or two and then err disables again ... it's been going
on for two weeks" ([r/networking](https://www.reddit.com/r/networking/comments/kurgei/esxi_host_network_err_disabled/));
the same loop for lab ports ([r/networking](https://www.reddit.com/r/networking/comments/1phcm92/switch_port_keeps_getting_errordisabled_whats_the/)).

**How Steward does it.** Every run (hourly by default) reads the interfaces of switch ports that have a
neighbour in the topology: an AP, switch or controller, so the port should always be up. Each time such a port
goes down while administratively up is recorded; a port down 3 or more times in 7 days becomes one report item
with the count, the times, the neighbour and the root-cause advice, instead of one ticket per occurrence. Ports
without a known neighbour (desks, printers) are left out, because they go down whenever someone unplugs.
Catalyst Center does not return the err-disable reason through its API, so the item lists the likely causes
(BPDU guard, port security, inline power, UDLD, a flapping link); a port Catalyst Center reports as
err-disabled is marked as such. Read-only.

### 4. Alert-noise tuning

**What operators say.** "DNAC sent me about 1,000+ emails today about the hot router"
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-alerting-via-email-for-events/td-p/4454455));
APs in mobile MRI trailers alert every night
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/exclude-ap-s-from-certain-dna-center-alerts/td-p/5209243));
"Device Unreachable" alerts that clear within a minute
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/excessive-device-unreachable-notifications/td-p/5337376)).

**How Steward does it.** Extends Expected-down and flapping devices: find issues that fire daily for the same
devices or sites, and propose an Assurance network profile or a priority change for those sites (Catalyst
Center supports both). The operator approves; nothing is silenced automatically.

### 5. Unsaved configuration

**What operators say.** Startup and running configuration out of sync, then "Run Compliance, wait about 10
minutes ... re-provision" ([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-compliance-error-startup-vs-running-configuratio/m-p/4283918)).

**How Steward does it.** Devices non-compliant on "Startup vs Running" (compliance type `RUNNING_CONFIG`) for
3 days or more (adjustable); recent changes are left alone, because someone may still be working on them.
Steward proposes saving the running configuration through Catalyst Center
(`POST /dna/intent/api/v1/network-device-config/write-memory`, 2.3.7 and later), re-runs the compliance check and
verifies the device is compliant again. On older releases the findings are an owner report. The operator
approves; L2 can save automatically above the confidence threshold.

### 6. AP floor-map hygiene

**What operators say.** "You must place the access points manually on the floorplan"
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-heatmap-query/td-p/5259681));
"upgraded DNA center and it wiped my maps"
([r/networking](https://www.reddit.com/r/networking/comments/1gebtt9/has_anyone_moved_away_from_cisco_dna_center_while/)).

**How Steward does it.** APs assigned to a floor but not positioned on its map
(`GET /dna/intent/api/v2/floors/{floorId}/accessPointPositions`), and the placed count per floor over time,
so a drop after a Catalyst Center upgrade is caught the same day.

### 7. Stale inventory

**What operators say.** Unreachable devices that cannot be deleted, sometimes only with TAC
([Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dna-center-seeking-help-to-delete-a-device-that-is-not/td-p/3792173),
[Cisco Community](https://community.cisco.com/t5/cisco-catalyst-center/dnac-2-1-2-5-cannot-delete-a-device-from-inventory/td-p/4424016)).

**How Steward does it.** Devices unreachable for 90 days or more (adjustable) are proposed for deletion from
the Catalyst Center inventory (`DELETE /dna/intent/api/v1/network-device/{id}`), with the reminder to remove
SD-Access fabric and port assignments first. Devices excluded on purpose are kept: maintenance mode or the
exclusion tag of Expected-down and flapping devices, and its expected-down sites and hostname prefixes. Deleting
is never automatic (maximum L1); Steward verifies the device is gone.

### 8. Port and VLAN hygiene

**What operators say.** Daily work is still manual port and VLAN changes and audits
([r/networking](https://www.reddit.com/r/networking/comments/1e6k1uf/automation_tools_for_cisco_enterprise_networks/)).

**How Steward does it.** Access ports whose VLAN or description does not match the site standard, for example
AP-facing ports outside the AP VLAN or ports without a description, from the interface API
(`vlanId`, `description`, `portMode`).
