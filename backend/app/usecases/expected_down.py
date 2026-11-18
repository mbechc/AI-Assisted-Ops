"""Expected-down and flapping devices: put them in maintenance mode (or tag them) so they stop raising noise.

* Long-down devices (e.g. a decommissioned or seasonal offshore site) and devices matching a configured
  site path or hostname prefix are excluded with a maintenance window (Catalyst Center 2.3.7.6+), falling
  back to the AIOPS-EXCLUDED tag on older releases.
* Flapping devices (repeated reachability changes in the inventory history) are excluded the same way.
* Reverse check: excluded devices that are reachable and stable again are proposed for inclusion.
Briefly-down devices are left alone: that is a real outage, not noise.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, history, is_unreachable
from app.models import WorkItem
from app.usecases.common import fail_all, open_targets


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _split(v) -> list[str]:
    if isinstance(v, list):
        return [x.strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


@register
class ExpectedDown(TaskDefinition):
    key = "expected_down_devices"
    name = "Expected-down and flapping devices"
    description = ("Finds devices that have been unreachable for a long time, devices on sites or with hostname "
                   "prefixes you mark as expected-down, and devices that keep flapping. Proposes maintenance mode "
                   "(or the AIOPS-EXCLUDED tag on older releases) via Catalyst Center, and proposes taking devices "
                   "out again once they are reachable and stable.")
    category = "Health / Noise reduction"
    worker = "health"
    default_cron = "20 */4 * * *"
    max_autonomy = 2
    default_autonomy_level = 1
    default_autonomy_cap = 1
    default_auto_threshold = 95
    default_max_auto_per_run = 20
    action_types = {"exclude_device": "Put device in maintenance mode / exclusion tag",
                    "include_device": "End maintenance mode / remove exclusion tag"}
    params = [
        ParamSpec("down_days", "Unreachable for at least (days) to propose exclusion", "int", 7),
        ParamSpec("expected_down_site_paths", "Site paths that are expected down (comma separated)", "str", ""),
        ParamSpec("expected_down_hostname_prefixes", "Hostname prefixes that are expected down (comma separated)",
                  "str", ""),
        ParamSpec("flap_window_hours", "Flapping: look-back window (hours)", "int", 48),
        ParamSpec("flap_min_changes", "Flapping: minimum reachability changes in the window", "int", 4),
        ParamSpec("stable_hours", "Reverse check: reachable and stable for (hours)", "int", 24),
        ParamSpec("maintenance_days", "Maintenance window length (days)", "int", 90),
        ParamSpec("exclusion_tag", "Tag used when maintenance mode is not available", "str", "AIOPS-EXCLUDED"),
        ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 600),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        now = datetime.now(timezone.utc)
        site_label = {s.id: s.hierarchy for s in inv.sites}
        tag = p.get("exclusion_tag") or "AIOPS-EXCLUDED"
        try:
            excluded = ctx.cc.list_excluded(tag)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"Could not read current exclusions: {exc}")
            excluded = {}
        ctx.log(f"Currently excluded: {len(excluded)} device(s)")

        site_paths = [x.lower() for x in _split(p.get("expected_down_site_paths"))]
        prefixes = [x.lower() for x in _split(p.get("expected_down_hostname_prefixes"))]
        flap_since = now - timedelta(hours=int(p.get("flap_window_hours", 48)))
        flaps = history(ctx.db, ctx.source.id, "reachability", flap_since)
        stable_since = now - timedelta(hours=int(p.get("stable_hours", 24)))
        recent = history(ctx.db, ctx.source.id, "reachability", stable_since)
        down_days = int(p.get("down_days", 7))
        flap_min = int(p.get("flap_min_changes", 4))

        proposals: list[Proposal] = []
        stats = Counter()
        for d in inv.devices:
            row = inv.rows.get(d.id)
            site = site_label.get(inv.assignments.get(d.id), "")
            down = is_unreachable(d.reachability)
            changes = len(flaps.get(d.id, []))
            if d.id in excluded:
                if not down and not recent.get(d.id):
                    stats["include"] += 1
                    proposals.append(self._include(d, site, excluded[d.id], int(p.get("stable_hours", 24))))
                else:
                    stats["already_excluded"] += 1
                continue
            reasons, conf = [], 0
            if down:
                since = [x for x in (_aware(row.unreachable_since) if row else None,
                                     _aware(row.last_update_at) if row else None) if x]
                days = (now - min(since)).total_seconds() / 86400 if since else 0
                if days >= down_days:
                    reasons.append(f"unreachable for {days:.0f} days")
                    conf = max(conf, 95 if days >= 30 else 80)
                if site_paths and any(site.lower().startswith(sp) for sp in site_paths):
                    reasons.append("site marked as expected-down")
                    conf = max(conf, 95)
                if prefixes and any((d.hostname or "").lower().startswith(px) for px in prefixes):
                    reasons.append("hostname prefix marked as expected-down")
                    conf = max(conf, 95)
                if not reasons:
                    stats["down_recent"] += 1
            if changes >= flap_min:
                reasons.append(f"{changes} reachability changes in {int(p.get('flap_window_hours', 48))} h")
                conf = max(conf, min(95, 60 + 5 * changes))
                stats["flapping"] += 1
            if reasons:
                stats["exclude"] += 1
                proposals.append(self._exclude(d, site, reasons, conf, row, int(p.get("maintenance_days", 90))))

        keys = {pr.target_key for pr in proposals}
        resolved = {}
        for t in open_targets(ctx, self.key):
            if t not in keys:
                resolved[t] = {"excluded_now": t in excluded,
                               "reachability": next((d.reachability for d in inv.devices if d.id == t), None)}
        ctx.log(f"Proposals: {stats['exclude']} exclude ({stats['flapping']} flapping), {stats['include']} include; "
                f"{stats['down_recent']} recently down left alone, {stats['already_excluded']} already excluded")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "devices": len(inv.devices), "excluded": len(excluded), "cc_api_calls": ctx.cc.http.calls, **stats})

    def _exclude(self, d, site: str, reasons: list[str], conf: int, row, days: int) -> Proposal:
        text = f"{d.hostname}: " + "; ".join(reasons) + "."
        if row is not None and row.reachability_failure:
            text += f" Last failure: {row.reachability_failure}."
        return Proposal(
            target_key=d.id, target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})", action_type="exclude_device",
            payload={"device_id": d.id, "hostname": d.hostname, "site": site, "reasons": reasons, "days": days,
                     "family": d.family, "reachability": d.reachability},
            rule_confidence=conf,
            evidence=[{"signal": "reachability", "label": "Reachability history", "available": True, "weight": 1.0,
                       "detail": text, "buildings": [], "floors": [], "agrees": True}],
            rationale=f"Exclude for {days} days: " + "; ".join(reasons) + ".",
            concerns=[] if conf >= 90 else ["Check that this is not a real outage someone should fix."],
            group_label=site.split("/")[-2] if site.count("/") >= 2 else (site or "No site"),
            fingerprint=f"exclude:{d.id}:{','.join(sorted(r.split(' ')[0] for r in reasons))}",
        )

    def _include(self, d, site: str, exc: dict, hours: int) -> Proposal:
        return Proposal(
            target_key=d.id, target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})", action_type="include_device",
            payload={"device_id": d.id, "hostname": d.hostname, "site": site, "mechanism": exc.get("mechanism"),
                     "ref": exc.get("ref"), "family": d.family},
            rule_confidence=90,
            evidence=[{"signal": "reachability", "label": "Reachability history", "available": True, "weight": 1.0,
                       "detail": f"Reachable with no reachability change for {hours} h while excluded "
                                 f"({exc.get('mechanism')}).", "buildings": [], "floors": [], "agrees": True}],
            rationale=f"{d.hostname} is reachable and stable again; end the exclusion so it is monitored.",
            group_label="Back to normal", fingerprint=f"include:{d.id}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        want = item.action_type == "exclude_device"
        return {**observed, "matched": observed.get("excluded_now") == want}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        cc = ctx.cc
        tag = ctx.params.get("exclusion_tag") or "AIOPS-EXCLUDED"
        timeout = int(ctx.params.get("task_timeout_seconds", 600))
        results: dict[int, ExecResult] = {}
        excl = [i for i in items if i.action_type == "exclude_device"]
        incl = [i for i in items if i.action_type == "include_device"]
        if excl:
            days = int(ctx.params.get("maintenance_days", 90))
            ids = [i.payload["device_id"] for i in excl]
            try:
                res = cc.wait_all(cc.exclude_devices(ids, days, f"AIOps: expected-down/flapping ({len(ids)})", tag),
                                  timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                results.update(fail_all(excl, f"exclude failed: {exc}"))
            else:
                if not res.success:
                    results.update(fail_all(excl, f"Catalyst Center task failed: {res.detail}"))
                else:
                    results.update(self._verify(ctx, excl, tag, expect=True))
        if incl:
            try:
                current = cc.list_excluded(tag)
                subset = {i.payload["device_id"]: current[i.payload["device_id"]] for i in incl
                          if i.payload["device_id"] in current}
                res = cc.wait_all(cc.include_devices(subset, tag), timeout=timeout) if subset else None
            except Exception as exc:  # noqa: BLE001
                results.update(fail_all(incl, f"include failed: {exc}"))
            else:
                if res is not None and not res.success:
                    results.update(fail_all(incl, f"Catalyst Center task failed: {res.detail}"))
                else:
                    results.update(self._verify(ctx, incl, tag, expect=False))
        return results

    @staticmethod
    def _verify(ctx: RunContext, items: list[WorkItem], tag: str, expect: bool) -> dict[int, ExecResult]:
        try:
            now = ctx.cc.list_excluded(tag)
        except Exception as exc:  # noqa: BLE001
            return {i.id: ExecResult(True, f"done (verification unavailable: {exc})", verified=None) for i in items}
        out = {}
        for i in items:
            dev = i.payload["device_id"]
            ok = (dev in now) == expect
            mech = (now.get(dev) or {}).get("mechanism")
            out[i.id] = ExecResult(ok, ("excluded via " + str(mech) if expect else "exclusion removed") if ok
                                   else "Catalyst Center does not show the expected state", verified=ok,
                                   data={"mechanism": mech})
        return out
