# AIOps: AI-Assisted Operations for Cisco Catalyst Center

**Your network's night shift: an expert system that clears the repetitive Catalyst Center backlog, shows
its evidence, and only acts as autonomously as you allow. AI is optional, local or in your own Azure
tenant.**

## Why it's valuable

Every Catalyst Center estate collects the same low-priority debt: APs without a site, devices on the wrong
floor, APs back on factory names, roles that drifted, devices that have been down for months and
telemetry that silently stopped. Each fix is small; together they eat engineering days and degrade
assurance, maps and reports. AIOps works through that backlog continuously.

- **Evidence, not guesses.** Every proposal carries a confidence score and the signals behind it
  (CDP/LLDP neighbours, RF neighbours, naming, subnets, inventory history, optional GeoIP).
- **You set the autonomy.** L0 report only, L1 every change reviewed, L2 automatic above a confidence
  threshold, L3 full autonomy. Per-task caps, a global cap and a kill switch.
- **Private by design.** AI is off by default and no network data leaves the host. If you turn it on,
  choose a local LLM (Ollama, CPU only, nothing leaves the host) or Azure OpenAI in your own tenant.
- **Safe change path.** Every change goes through the Catalyst Center API. Nothing connects to WLCs, APs
  or switches directly (no SSH, NETCONF or Command Runner).
- **Accountable.** Every approval, change, setting and log view is in the audit log; a review queue
  dashboard shows the trend per category over 30 days.
- **Isolated workers.** Each domain runs in its own container and queue, so one runaway task cannot stall
  the others.

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

**GeoIP is optional.** Every task works without it. An uploaded GeoIP database adds one more vote
(default weight 0.15); a proposal backed only by GeoIP is capped at confidence 60 and never executed
automatically.

## AI is Optional

Every task is an **Expert Task**: rules and evidence from Catalyst Center make the proposals, and every
task works fully without AI. AI is an optional assistant on top. Choose one of three modes under
**Settings → AI assistance**:

| Mode | What runs | Data leaves the host | Default |
|---|---|---|---|
| **No AI** | Rules and evidence only | No | Yes |
| **Local LLM** | Ollama on the appliance (CPU), installed only when you select it | No | |
| **Azure OpenAI** | Your Azure OpenAI deployment (Microsoft's enterprise LLM service, the models behind Copilot) | Yes, to your Azure tenant | |

[How AI is implemented](docs/AI_USAGE.md)

## Getting started

### Operating system

A dedicated Linux VM or server, x86_64, with systemd:

- **Debian 12** (tested)
- **Ubuntu 22.04 / 24.04**
- **Red Hat Enterprise Linux 9**, and Rocky Linux / AlmaLinux 9

The installer installs Docker Engine and the compose plugin from Docker's repositories when missing.

### Machine

| | No AI or Azure OpenAI | Local LLM |
|---|---|---|
| CPU | 8 vCPU, x86_64 | 16 vCPU, x86_64 with AVX2 |
| Memory | 16 GB RAM | 64 GB RAM |
| Disk | 100 GB SSD under the Docker root (`/var/lib/docker`) | 200 GB SSD |

The local LLM runs on the CPU, so more cores means faster reviews; size the VM for Local LLM if you may
turn it on later. The sizes leave room for more use cases and Catalyst Centers, image versions kept for
rollback, database growth and 14 days of backups.

Network: HTTPS from the host to your Catalyst Center(s), to GitHub for installation and updates
(see [Updates](#updates)) and, with Azure OpenAI, to your Azure OpenAI endpoint.

### Install

The one-line install needs `curl` and `sudo`. Minimal Debian installs often have neither (Debian leaves
out `sudo` when a root password is set during installation), so install them first as root (`su -`):

```bash
apt-get update && apt-get install -y curl sudo   # Debian / Ubuntu
dnf install -y curl sudo                         # RHEL / Rocky / AlmaLinux
```

Then run one command on the host:

```bash
curl -fsSL https://raw.githubusercontent.com/mbechc/AI-Assisted-Ops/master/get-aiops.sh | sudo bash
```

It finds the newest Production release on GitHub, verifies its checksum, installs into `/opt/AIOps`,
pulls the prebuilt images from ghcr.io, initialises the database, creates the `admin` user and runs a
self-test. No LLM is downloaded: the appliance starts in No AI mode. At the end it prints the portal URL and the admin password (also stored
in `/opt/AIOps/initial-admin-password.txt`; delete it after the first login).

Options go after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/mbechc/AI-Assisted-Ops/master/get-aiops.sh \
  | sudo bash -s -- --hostname aiops.example.com --channel production
```

| Option | Meaning |
|---|---|
| `--dir DIR` | Installation folder (default `/opt/AIOps`) |
| `--channel production\|development` | Update track (default `production`) |
| `--version X.Y.Z` | Install a specific release |
| `--hostname NAME` | Name in the TLS certificate and portal URL |
| `--admin-password PW` | Set the initial admin password instead of generating one |
| `--https-port N` / `--http-port N` | Published ports (default 443/80) |
| `--with-mock` | Also start two simulated Catalyst Centers (2.3.5.6 and 3.1.3.0) for evaluation |

Behind a proxy: `export https_proxy=http://proxy:3128` and run `sudo -E bash` instead of `sudo bash`;
configure the Docker daemon proxy as well, so image pulls work.

### First configuration

1. Log in at `https://<host>/` as `admin` and change the password (user menu, bottom left).
2. **Data sources**: add the Catalyst Center (URL, a user with network-admin or an assign-device role),
   then **Test**, which detects the release and shows which API variant each operation uses.
3. **Expert Tasks**: press **Run** on a task (or open it to set its source and schedule), then review
   the proposals in **Review queue**. Schedules are cron expressions in UTC with presets on the task page.
4. As calibration builds up (approved vs. rejected per confidence band), raise the autonomy level. Every
   task has a cap; the global cap and the kill switch are under **Settings**. The global cap starts at
   L1 (human review) on a fresh install. Effective level = min(selected, task cap, global cap).
5. **Site hierarchy drift**: approve the current hierarchy as the baseline on the task page; later runs
   report changes against it.
6. **GeoIP (optional)**: add a data source of type GeoIP and upload a MaxMind-format `.mmdb` (GeoIP2/GeoLite2
   City or an internal database with a site/building field) under **Manage GeoIP**.
   Deleting a data source (admins) removes its inventory data and closes its open review items and queued
   runs; finished runs and decisions stay in the history. The simulated sources are added once, so a
   deleted one does not come back on upgrade.
7. Optional: replace `/opt/AIOps/certs/server.crt` and `server.key` with your own certificate and run
   `docker compose restart web` in `/opt/AIOps`.
8. **AI assistance (optional)**: see [Enabling AI](#enabling-ai).

### Enabling AI

Admins choose the mode under **Settings → AI assistance**. Switching is possible at any time; the next
task run uses the new mode.

- **Local LLM**: confirm, and the host updater starts the Ollama container and downloads the model
  (about 5 GB, from `registry.ollama.ai`). The section shows the progress and log; until the model is
  ready the tasks keep running without AI. **Remove model** stops the container and deletes the model;
  switching to another mode stops it and optionally deletes it. From the shell:
  `sudo /opt/AIOps/scripts/update.sh --llm enable|disable|remove`. Air-gapped: put an
  `ollama-models.tgz` (from `scripts/export-bundle.sh` on a host that has the model) in
  `/opt/AIOps/data/llm/` before enabling.
- **Azure OpenAI**: create an Azure OpenAI resource and a chat model deployment (for example
  `gpt-4o-mini`) in your tenant. Enter the endpoint (`https://<resource>.openai.azure.com`), the
  deployment name and an API key, or a Microsoft Entra app registration (tenant ID, client ID, secret)
  with the *Cognitive Services OpenAI User* role. **Test connection** sends a tiny prompt; secrets are
  stored encrypted and never shown again.

## Updates

**Settings → Updates** shows the installed version, the track and the newest release for that track.
Administrators switch track, refresh and install; operators and viewers see the same information read-only.

| Track | Offers |
|---|---|
| **Production** | Stable releases (`vX.Y.Z`) only |
| **Development** | Every release, including pre-releases (`vX.Y.Z-dev.N`) |

Switching back from Development to Production takes effect with the next Production release that is the
same version or newer; older versions are never offered.

Installing an update runs on the host: the release is downloaded and its checksum verified, a backup is
taken (database, `.env`, certificates), the changed image layers are pulled from ghcr.io (each service is
its own image, and unchanged layers are not downloaded again), migrations run and services restart. The
log is shown live in the portal.

Updates are online only. The host must reach:

| Host | Used for |
|---|---|
| `api.github.com` | Release lookup |
| `github.com` | Release download |
| `objects.githubusercontent.com`, `release-assets.githubusercontent.com` | Release download (redirect target) |
| `ghcr.io`, `pkg-containers.githubusercontent.com` | Application images |
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` | Base images (PostgreSQL, Redis; Ollama only with Local LLM), when a release changes them |
| `registry.ollama.ai` | LLM model: Local LLM only, when enabled or when a release changes the model |
| `<resource>.openai.azure.com` | Azure OpenAI mode only (not part of the update check) |
| `login.microsoftonline.com` | Azure OpenAI with Microsoft Entra sign-in only |

**Refresh** tests the GitHub endpoints from the Linux host itself (its DNS, proxy and firewall) and shows
each one as reachable or not, with the response time or the error. The same test runs every 15 minutes
(`AIOPS_CONNECTIVITY_INTERVAL_MIN` in `.env`) and drives the status on the **Dashboard**: **Connected**
when every endpoint answered, **AirGapped** when one or more did not. The appliance keeps working without
Internet; only updates need it.

How it works: the portal never gets root or the Docker socket. It drops a request file into
`/opt/AIOps/data/update/requests`; the systemd unit `aiops-updater.path` starts `scripts/update.sh` on the
host, which writes its results back to `data/update`. From the shell:

```bash
sudo /opt/AIOps/scripts/update.sh --check            # reachability and release lookup
sudo /opt/AIOps/scripts/update.sh --channel development
sudo /opt/AIOps/scripts/update.sh --version 0.3.0
```

**Rollback**: restore the backup taken right before the update without migrating it, then install the
previous release:

```bash
sudo /opt/AIOps/scripts/restore.sh --no-migrate /opt/AIOps/backups/db-<timestamp>.sql.gz
curl -fsSL https://raw.githubusercontent.com/mbechc/AI-Assisted-Ops/master/get-aiops.sh | sudo bash -s -- --version <previous>
```

## Operations

All scripts are in `/opt/AIOps`:

| Command | Purpose |
|---|---|
| `scripts/healthcheck.sh` | Container health, portal, API, database, Redis, AI provider check |
| `scripts/backup.sh` | Database dump + `.env`/certificates into `backups/` (keeps 14) |
| `scripts/restore.sh backups/db-....sql.gz` | Restore a database dump |
| `scripts/update.sh` | Host updater (see [Updates](#updates)) |
| `scripts/record-fixtures.sh <source-id>` | Record sanitized Catalyst Center responses for support |
| `scripts/acceptance-mock.sh` | End-to-end test of every task against the simulators (install with `--with-mock`) |
| `scripts/uninstall.sh [--purge]` | Remove containers (`--purge`: also data, model, images and the updater units) |

Keep a copy of `.env`: it contains the key that encrypts stored Catalyst Center credentials.

**Workers, Runners and logs.** Tasks run in separate worker containers (`worker-default`,
`worker-wireless`, `worker-inventory`, `worker-health`), each with its own queue, memory limit and
read-only filesystem. **Runners** shows each worker's health, active and queued runs, failures in the
last 24 hours, CPU/memory and the schedule for the next 24 hours. **Container logs** (admins only, every
view is audited) follows the live logs of the AIOps containers through `logproxy`, a socket proxy that
only allows reading container information and logs.

**Offline installation** (installation only; updates need GitHub): on a connected AIOps host run
`sudo /opt/AIOps/scripts/export-bundle.sh /tmp/aiops-offline.tar`, then on the target host (Docker
already installed): `tar -xf aiops-offline.tar && cd aiops-<version> && sudo ./install.sh --offline-bundle ../aiops-offline.tar --skip-docker-install`.

## Development

```bash
make test      # backend unit tests (against both simulated Catalyst Center releases) + frontend typecheck
make release   # dist/aiops-<version>.tar.gz
make deploy HOST=root@<host> INSTALL_ARGS="--yes --build --with-mock"   # builds the images on the host
```

### Cutting a release (maintainers)

1. Set the version in `VERSION` (and `frontend/package.json`, `backend/app/config.py`,
   the image defaults in `docker-compose.yml`).
2. Commit, then tag and push: `git tag v0.3.0 && git push origin v0.3.0`. Use `v0.3.0-dev.1` for a
   Development pre-release.
3. The `release` workflow runs the tests, pushes `ghcr.io/mbechc/aiops-backend` and `aiops-web` with that
   version, and publishes the GitHub Release with `aiops-<version>.tar.gz` and `SHA256SUMS`. Tags with a
   `-` are marked as pre-releases and only offered on the Development track.

The ghcr.io packages must be public (Package settings → Change visibility) so appliances can pull without
credentials.

## License

See [LICENSE](LICENSE).
