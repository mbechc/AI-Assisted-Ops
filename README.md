# CiscoAIOps

Local, offline AI assistant for repetitive, low-priority network tasks. Recurring tasks analyse data
from Cisco Catalyst Center (2.x and 3.x), propose changes with a confidence score and evidence, and
either queue them for operator review or execute them autonomously within the autonomy level and caps
configured in the portal. A local LLM (Ollama) reviews uncertain proposals; nothing leaves the host.

Generation 1 data source: Catalyst Center. All changes go through the Catalyst Center API; nothing
connects to WLCs, APs or switches directly (no SSH, NETCONF or Command Runner).

| Task | Worker | Max level | What it does |
|---|---|---|---|
| Inventory snapshot | default | - | Collects devices, sites and topology once for all tasks; keeps a history of hostname, site, role and reachability changes |
| AP site assignment | wireless | L3 | Floor/building for APs without a site, from CDP/LLDP uplinks, RF neighbours, AP tags, naming, subnets and (optional) GeoIP |
| Wrong-site detection | inventory | L2 | Devices whose Catalyst Center building/floor disagrees with the evidence; proposes a reassignment |
| AP hostname reset | wireless | L2 | APs back on factory names (`APxxxx.xxxx.xxxx`): previous name from inventory history, otherwise the floor naming convention |
| Site hierarchy drift | inventory | L0 | Report of renamed/moved/added/removed sites against an approved baseline, plus hygiene (empty floors, buildings without floors, duplicates) |
| Expected-down devices | health | L2 | Long-down or flapping devices into maintenance mode (2.3.7+) or an exclusion tag; reverse check returns recovered devices |
| Telemetry baseline | health | L3 | Classifies missing telemetry (SNMP, credentials, collection failure, stale sync); resyncs stale devices, reports the rest to the owner |
| Device role correction | inventory | L2 | Access/distribution/core from the CDP/LLDP topology; corrects wrong roles |

**GeoIP is optional.** Every task works without it. When a GeoIP database is uploaded it adds one more
vote (default weight 0.15) on top of the existing signals; a proposal backed only by GeoIP is capped at
confidence 60 and is never executed automatically.

## Requirements

- Debian 12, Ubuntu 22.04/24.04 or RHEL/Rocky/Alma 9 (x86_64)
- 8-16 CPU cores, 32 GB RAM (minimum 4 cores / 16 GB), 40 GB free disk under the Docker root
- Internet access **during installation only** (or use an offline bundle, see below)
- HTTPS access from the host to the Catalyst Center(s)

## Install

```bash
tar -xzf ciscoaiops-<version>.tar.gz
cd ciscoaiops-<version>
sudo ./install.sh
```

The installer installs Docker Engine if missing, writes `/opt/CiscoAIOps`, generates secrets and a
self-signed TLS certificate, builds/pulls all images, downloads the LLM model, initialises the database,
creates the `admin` user and runs a self-test. The generated admin password is printed and stored in
`/opt/CiscoAIOps/initial-admin-password.txt` (delete it after the first login).

Common options (`./install.sh --help` for all):

| Option | Meaning |
|---|---|
| `--hostname NAME` | Name in the TLS certificate and portal URL |
| `--admin-password PW` | Set the initial admin password instead of generating one |
| `--https-port N` / `--http-port N` | Change published ports (default 443/80) |
| `--with-mock` | Also start two simulated Catalyst Centers (2.3.5.6 and 3.1.3.0) for evaluation |
| `--offline-bundle FILE` | Install without Internet from an offline bundle |
| `-y` | Non-interactive |

After installation the system needs no Internet access.

To use your own certificate, replace `/opt/CiscoAIOps/certs/server.crt` and `server.key` and run
`docker compose restart web` in `/opt/CiscoAIOps`.

## Offline installation

On a connected host with CiscoAIOps installed:

```bash
sudo /opt/CiscoAIOps/scripts/export-bundle.sh /tmp/ciscoaiops-offline.tar
```

On the air-gapped host (Docker Engine must already be installed):

```bash
tar -xf ciscoaiops-offline.tar
cd ciscoaiops-<version>
sudo ./install.sh --offline-bundle ../ciscoaiops-offline.tar --skip-docker-install
```

## Operations

All scripts are in `/opt/CiscoAIOps`:

| Command | Purpose |
|---|---|
| `scripts/healthcheck.sh` | Container health, portal, API, database, Redis, LLM self-test |
| `scripts/backup.sh` | Database dump + `.env`/certificates into `backups/` (keeps 14) |
| `scripts/restore.sh backups/db-....sql.gz` | Restore a database dump |
| `upgrade.sh` (from the new package) | Backup, then upgrade in place; data and settings are kept |
| `scripts/record-fixtures.sh <source-id>` | Record sanitized Catalyst Center responses for support |
| `scripts/acceptance-mock.sh` | End-to-end test of every task against the simulators (install with `--with-mock`) |
| `scripts/uninstall.sh [--purge]` | Remove containers (`--purge`: also data, model and images) |

Keep a copy of `.env`: it contains the key that encrypts stored Catalyst Center credentials.

## First steps in the portal

1. **Data sources**: add the Catalyst Center (URL, a user with network-admin or an assign-device role),
   then **Test**, which detects the release and shows which API variant is used for each operation.
2. **Tasks → AP site assignment**: select the source, run once, and review the proposals in **Review**.
3. As calibration builds up (approved vs. rejected per confidence band), raise the autonomy level.
   Every task has a cap; the global cap and the kill switch are under **Settings**.

Autonomy levels: L0 report only, L1 every proposal is reviewed, L2 proposals at or above the threshold
are executed automatically, L3 full autonomy. Effective level = min(selected, task cap, global cap).

4. **Site hierarchy drift**: approve the current hierarchy as the baseline on the task page; later runs
   report changes against it.
5. **GeoIP (optional)**: add a data source of type GeoIP, upload a MaxMind-format `.mmdb` (GeoIP2/GeoLite2
   City or an internal database with a site/building field) under **Manage GeoIP**. Uploaded versions are
   kept, so an earlier one can be re-activated. Location keys are learned from APs already in the right
   building; unmapped keys can be mapped to a building by hand.

## Workers, Runners and logs

Tasks run in separate worker containers (`worker-default`, `worker-wireless`, `worker-inventory`,
`worker-health`), each with its own queue, memory limit and read-only filesystem, so a failing or runaway
task cannot take the others down. **Runners** shows each worker's health, active and queued runs,
failures in the last 24 hours, container CPU/memory and the schedule for the next 24 hours.

**Container logs** (admins only, every view is audited) follows the live logs of the CiscoAIOps
containers; a run's drawer can follow just the lines of that run. Logs are read through `logproxy`, a
socket proxy that only allows reading container information and logs; the API never gets the Docker socket
and other containers on the host are not shown.

## Development

```bash
make test      # backend unit tests (against both simulated Catalyst Center releases) + frontend typecheck
make release   # dist/ciscoaiops-<version>.tar.gz
make deploy HOST=root@<host> INSTALL_ARGS="--yes --with-mock"
```
