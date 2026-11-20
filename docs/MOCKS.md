# Simulated Catalyst Centers (mocks)

`--with-mock` starts two simulated Catalyst Centers next to AIOps, so you can evaluate every Expert Task
without a lab. This document explains how they work, what they produce, when findings appear and what
happens when you approve them.

## Summary

- Two mocks: **Catalyst Center 2.3.5.6** (`mockcc-23`) and **3.1.3.0** (`mockcc-31`). They are added as
  data sources automatically during installation.
- Each mock builds one synthetic estate of about **6,000 APs** when its container starts. The estate is
  deterministic: it is generated from a fixed seed, so it is the same every time.
- Both mocks produce findings for **every Expert Task**. Findings are not generated randomly over time;
  they appear as soon as a task runs (flapping devices need a few collections first).
- Approved changes are applied to the mock through the normal Catalyst Center API and **stay fixed** until
  the mock container restarts. After a restart the estate is rebuilt and the original problems are back.

## How they work

The mocks are a small web service (`backend/mockcc/`) that implements the Catalyst Center REST API calls
AIOps uses: authentication, release detection, sites, devices, site membership, topology, AP configuration,
RF neighbours, device health, site assignment, AP rename, role change, resync, maintenance schedules, tags
and task polling.

| | Mock 2.3.5.6 | Mock 3.1.3.0 |
|---|---|---|
| Service | `mockcc-23` | `mockcc-31` |
| Seed | 23 | 31 |
| Site APIs | Legacy `/site` and `/membership` | New `/sites` and `assignedToSite` |
| Site assignment | Legacy `site/{id}/device` with execution-status polling | `assignToSite/apply` with `/tasks` polling |
| Release detection | `/dnac-release` | Only `/dnac-packages` (`/dnac-release` returns 404) |
| RF neighbours | Not available | Available through the Assurance API |
| Device health | `/device-health` | `/assuranceNetworkDevices` |
| Expected-down exclusion | Exclusion tag | Maintenance schedule |

The two releases deliberately expose different APIs, so each task is exercised on both code paths. The
seeds differ, so the device names, IDs and counts differ while the structure is the same.

All mock state lives in memory. Nothing is written to disk.

### The estate

Each mock generates:

- **Sites**: Global / Denmark with the areas Copenhagen (12 buildings, `CPH-B01`...), Aarhus (7, `AAR-`)
  and Odense (5, `ODE-`). Each building has 2 to 5 floors (`Floor 1`...). An extra area **Offshore** with
  building `DGB-B01` represents a site that has been "going live" for years.
- **Wireless**: one Catalyst 9800 controller per city and about 6,000 Catalyst 9130 APs, evenly spread over
  the floors. APs are named `<building>-F<floor>-AP<nnn>` (for example `CPH-B03-F2-AP017`).
- **Switching**: one access switch per floor (`<building>-F<floor>-SW01`), one distribution switch per
  building (`<building>-DS01`) and two core switches per city (`CPH-CORE-01`...). Odense uses two
  Catalyst 9300s as core (`ODE-B01-SW91/92`). Two switches are stacks (8 and 4 members).
- **Evidence for the tasks**: CDP/LLDP links from floor switches to 95% of the APs, RF neighbours (6 on the
  same floor, 2 on adjacent floors), AP site tags and locations (partly default values, as in real
  estates) and management subnets per floor.

## What each Expert Task finds

| Expert Task | What the mock plants | Approximate count per mock |
|---|---|---|
| AP site assignment | 20% of APs have no site | about 1,200 |
| Wrong-site detection | 2% of the assigned APs sit on a floor in another building of the same city | about 100 |
| AP hostname reset | 8% of APs carry the factory name `APxxxx.xxxx.xxxx` | about 360 to 480 |
| Device role correction | 20% of distribution switches tagged ACCESS, 2% of floor switches tagged CORE, Odense core switches tagged ACCESS | about 10 to 15 |
| Expected-down and flapping devices | Offshore switch and 10 APs unreachable for 400 days; about 1.5% of switches (and a few APs) flap | 11 immediately, flapping devices after a few collections |
| Telemetry baseline | Of the switches: 3% credential failure, 3% SNMP failure, 4% stale sync (3 to 10 days); 0.5% briefly down (must be left alone) | about 10 to 15 |
| Site hierarchy drift | An unused floor `F9` and other empty floors (hygiene). Real drift only on request, see below | "No approved baseline" and hygiene findings |

The ground truth (the real floor of every AP, the intended hostnames, the real roles and so on) is kept in
the mock and never exposed through the Catalyst Center API. The acceptance test reads it from
`/mock/truth` to measure accuracy.

## When findings appear

- **The estate exists from the moment the mock starts.** Findings appear when a task runs. The installer
  does not start any runs; tasks run on their schedule or when you press **Run now**:

  | Task | Default schedule (UTC) |
  |---|---|
  | Inventory snapshot (collector) | every 2 hours at :15 |
  | Expected-down and flapping devices | every 4 hours at :20 |
  | Telemetry baseline | every 6 hours at :40 |
  | AP site assignment | daily 05:30 |
  | Wrong-site detection | daily 05:45 |
  | AP hostname reset | daily 05:50 |
  | Site hierarchy drift | daily 06:10 |
  | Device role correction | daily 06:25 |

- **Flapping needs history.** The mock flips flapping devices between reachable and unreachable on every
  full inventory read. The task needs at least 4 changes within 48 hours, so flapping devices appear after
  about 4 collections (roughly 8 hours with the default collector schedule).
- **Site hierarchy drift is report only (L0).** Its findings have the status *reported*, not *proposed*;
  filter the **Inbox** on *Reported* or open the task page to see them. Approve the baseline on the task
  page first; later runs report changes against it.
- **One Catalyst Center per run.** A task without a selected source runs against the first enabled
  Catalyst Center (the 2.3.5.6 mock). To test the 3.1.3.0 mock, select it as the task's source and press
  **Run now**. The inventory collector always collects from both.

### Test-only events

Some situations only happen when something asks for them. The acceptance test
(`scripts/acceptance-mock.sh`) uses these endpoints; you can call them yourself from `/opt/AIOps` on the
host, for example:

```bash
docker compose exec api python -c "import httpx; print(httpx.post('http://mockcc-23:8080/mock/drift').text)"
```

Use `mockcc-31` for the 3.1.3.0 mock.

| Endpoint | Effect |
|---|---|
| `POST /mock/drift` | Renames a building (`-RENAMED`), moves a building to another city, adds building `NEW-B99` and removes floor `F9` |
| `DELETE /mock/drift` | Undoes the drift |
| `POST /mock/reset_names` | Factory-resets the names of about 1% more APs (new hostname-reset findings) |
| `GET /mock/geoip.mmdb` | A GeoIP database matching the estate (one location per building subnet, 10% of buildings missing) |
| `GET /mock/truth` | The ground truth used to score the results |

Apart from flapping devices and these endpoints, the mocks never create new problems by themselves.

## What happens when you approve

AIOps executes approved items through the same API calls it uses against a real Catalyst Center, and the
mock applies them. The next run verifies the result:

| Change | Effect in the mock | Next run |
|---|---|---|
| Site assignment / wrong-site reassignment | The device moves to the chosen floor | Finding closed |
| AP hostname reset | The AP is renamed (also in its AP configuration) | Finding closed |
| Device role correction | The role is changed | Finding closed |
| Telemetry resync of a stale device | The inventory timestamp is refreshed | Finding closed |
| Telemetry credential or SNMP failure | Report only: a resync cannot fix it | Stays open (by design) |
| Expected-down exclusion | Maintenance schedule (3.1) or exclusion tag (2.3) | Device excluded; no new proposal |

The mock applies what you approve, also when it is wrong: approving a wrong floor leaves the AP on that
floor, and Wrong-site detection may later propose moving it back.

Asynchronous operations answer *in progress* on the first poll and finish on the next one, and about every
250th request returns HTTP 429 (*Too many requests*), so retry and polling logic is exercised as well.

### Do findings come back?

- **Not while the mock keeps running.** Fixed items stay fixed, and declined items stay as they are in
  **Activity**.
- **Yes after the mock container restarts** (an update, a host reboot, `docker compose restart`). The mock
  rebuilds the same estate from its seed in memory: assignments, names, roles, maintenance schedules and
  tags you changed are gone, and the next runs propose the same fixes again. This makes it easy to repeat
  a demo.

## Configuration

The mocks run in the `mock` compose profile (`COMPOSE_PROFILES=mock` in `.env`). Settings per service in
`docker-compose.yml`:

| Variable | Meaning | Default |
|---|---|---|
| `MOCK_CC_RELEASE` | Catalyst Center release and API surface to simulate | `2.3.5.6` / `3.1.3.0` |
| `MOCK_CC_SEED` | Seed of the estate; another seed gives another estate with the same structure | `23` / `31` |
| `MOCK_CC_APS` | Number of APs | `6000` |

Each mock uses up to 768 MB of memory. Deleting a mock data source in AIOps removes its inventory data;
it is not added again on upgrade. Remove the mocks entirely by taking `mock` out of `COMPOSE_PROFILES` and
running `docker compose up -d --remove-orphans` in `/opt/AIOps`.

## Acceptance test

`sudo /opt/AIOps/scripts/acceptance-mock.sh` runs every task against both mocks, scores the results
against the ground truth (for example: at most 2% wrong at confidence 95 or higher), approves a sample,
verifies the changes in the mock and triggers the drift and name-reset events. Add `--ai-mode none`,
`local` or `cloud` to also check that the selected AI mode is in effect.

## Limits

- The mocks simulate the API responses AIOps needs, not a complete Catalyst Center.
- There is no web UI, no real device behind them, and nothing is persisted.
- Site renames by deleting and re-creating a site are not simulated, so the AI-assisted rename detection
  only sees plain renames in the mock.
