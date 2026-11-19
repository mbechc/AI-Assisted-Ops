"""AP hostname reset: APs that came back with the factory-default name (APxxxx.xxxx.xxxx).

The name is restored through the Catalyst Center AP configuration API (never on the WLC). The best
evidence is the AP's own earlier name from the inventory history; otherwise a name is derived from
the naming convention used by the other APs on the same floor.
"""
import re
from collections import Counter, defaultdict

from app.connectors.catalyst_center.models import Device
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, history, record_change
from app.models import WorkItem
from app.usecases.ap_site.naming import learn_floor_templates
from app.usecases.common import chunks, epoch, fail_all, open_targets

DEFAULT_NAME_RE = r"^AP[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$"
NUMBERED = re.compile(r"^(.*?)(\d+)$")
VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
METHOD_LABELS = {"history": "Inventory history", "convention": "Naming convention",
                 "ai_template": "Naming template (AI-learned)"}


def _convention(names: list[str]) -> tuple[str, int, set[int]] | None:
    """Most common '<prefix><number>' pattern among names: (prefix, width, used numbers)."""
    parsed = [m.groups() for m in (NUMBERED.match(n) for n in names) if m]
    if len(parsed) < 3:
        return None
    prefixes = Counter(p for p, _ in parsed)
    prefix, n = prefixes.most_common(1)[0]
    if n / len(names) < 0.8:
        return None
    nums = [num for p, num in parsed if p == prefix]
    width = Counter(len(x) for x in nums).most_common(1)[0][0]
    return prefix, width, {int(x) for x in nums}


@register
class ApHostnameReset(TaskDefinition):
    key = "ap_hostname_reset"
    name = "AP hostname reset"
    description = ("Finds access points that have the factory-default name (APxxxx.xxxx.xxxx), typically after a "
                   "reset or RMA, and proposes the correct name: the AP's own earlier name from the inventory "
                   "history, or a name following the convention of the other APs on the same floor. Renames go "
                   "through the Catalyst Center AP configuration API.")
    category = "Wireless / Inventory hygiene"
    worker = "wireless"
    default_cron = "50 5 * * *"
    max_autonomy = 2
    default_autonomy_level = 1
    default_autonomy_cap = 1
    default_auto_threshold = 95
    default_max_auto_per_run = 25
    action_types = {"rename_ap": "Rename access point (AP name only, via Catalyst Center)"}
    ai_features = {
        "naming_convention": "Derives the floor's AP naming pattern when names do not follow a simple prefix + number",
    }
    params = [
        ParamSpec("default_name_regex", "Regex for factory-default AP names", "str", DEFAULT_NAME_RE),
        ParamSpec("convention_min_peers", "Minimum named APs on the floor to derive a name", "int", 3),
        ParamSpec("ai_templates_enabled", "AI: learn naming templates for floors without a simple convention "
                  "(only with AI on)", "bool", True),
        ParamSpec("rename_batch_size", "APs per rename call", "int", 20),
        ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 600),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        default_re = re.compile(p.get("default_name_regex") or DEFAULT_NAME_RE)
        aps = [d for d in inv.devices if d.is_ap]
        reset = [a for a in aps if default_re.match(a.hostname or "")]
        in_use = Counter((d.hostname or "").lower() for d in inv.devices)
        ctx.log(f"APs: {len(aps)}, with factory-default name: {len(reset)}")

        past = history(ctx.db, ctx.source.id, "hostname", epoch(), {a.id for a in reset})
        site_label = {s.id: s.hierarchy for s in inv.sites}
        by_site: dict[str, list[Device]] = defaultdict(list)
        for a in aps:
            if a.id in inv.assignments:
                by_site[inv.assignments[a.id]].append(a)

        proposals: list[Proposal] = []
        taken: set[str] = set()
        stats = Counter()
        pending_convention: dict[str, list[Device]] = defaultdict(list)
        for ap in reset:
            prev = self._previous_name(ap, inv.rows.get(ap.id), past.get(ap.id, []), default_re)
            if prev and in_use[prev.lower()] == 0 and prev.lower() not in taken:
                taken.add(prev.lower())
                stats["from_history"] += 1
                proposals.append(self._proposal(ap, prev, 95, "history", site_label.get(inv.assignments.get(ap.id)),
                                                [f"Inventory history: this AP was named {prev} before it reverted "
                                                 f"to {ap.hostname}."], []))
                continue
            site = inv.assignments.get(ap.id)
            if site is None:
                stats["no_site"] += 1
                continue
            pending_convention[site].append(ap)

        min_peers = int(p.get("convention_min_peers", 3))
        peers_by_site = {site: [a.hostname for a in by_site[site] if not default_re.match(a.hostname or "")]
                         for site in pending_convention}
        rule_conv = {site: _convention(peers) if len(peers) >= min_peers else None
                     for site, peers in peers_by_site.items()}
        ai_templates: dict = {}
        if p.get("ai_templates_enabled", True):
            unresolved = {site: peers for site, peers in peers_by_site.items()
                          if rule_conv[site] is None and len(peers) >= min_peers}
            ai_templates = learn_floor_templates(ctx, unresolved)
        for site, resets in pending_convention.items():
            peers = peers_by_site[site]
            conv = rule_conv[site]
            if conv is None and site in ai_templates:
                self._propose_from_template(ap_list=resets, ft=ai_templates[site], site=site, peers=peers,
                                            site_label=site_label, in_use=in_use, taken=taken,
                                            proposals=proposals, stats=stats)
                continue
            if conv is None:
                stats["no_convention"] += len(resets)
                continue
            prefix, width, used = conv
            taken_nums = used | {int(m.group(2)) for t in taken if (m := NUMBERED.match(t)) and m.group(1) == prefix.lower()}
            gaps = [n for n in range(1, max(used) + 1) if n not in taken_nums]
            extra = max(used) + 1
            resets = sorted(resets, key=lambda a: a.mac or a.id)
            unique = len(resets) == 1 and len(gaps) <= 1
            for ap in resets:
                if gaps:
                    num, why = gaps.pop(0), "fills the gap in the floor's numbering"
                else:
                    num, why = extra, "next free number"
                    extra += 1
                name = f"{prefix}{num:0{width}d}"
                while in_use[name.lower()] or name.lower() in taken:
                    num = extra
                    extra += 1
                    name = f"{prefix}{num:0{width}d}"
                taken.add(name.lower())
                conf = 80 if unique else 55
                concerns = [] if unique else [
                    f"{len(resets)} APs on this floor need a name; the numbers are assigned in MAC order and may be swapped."]
                stats["from_convention"] += 1
                proposals.append(self._proposal(
                    ap, name, conf, "convention", site_label.get(site),
                    [f"{len(peers)} APs on {site_label.get(site, site)} follow '{prefix}{'n' * width}'; "
                     f"{name} {why}."], concerns))

        current = {a.id: a.hostname for a in aps}
        proposed = {pr.target_key for pr in proposals}
        resolved = {t: {"actual_hostname": current[t]} for t in open_targets(ctx, self.key)
                    if t in current and t not in proposed}
        resolved.update({t: {"removed_from_inventory": True} for t in open_targets(ctx, self.key) if t not in current})
        ctx.log(f"Proposals: {stats['from_history']} from history, {stats['from_convention']} from naming convention, "
                f"{stats['from_ai_template']} from an AI-learned template; "
                f"{stats['no_site']} without site, {stats['no_convention']} without a usable convention")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "aps_total": len(aps), "aps_default_name": len(reset), "proposals": len(proposals),
            "cc_api_calls": ctx.cc.http.calls, **stats})

    def _propose_from_template(self, ap_list, ft, site, peers, site_label, in_use, taken, proposals, stats) -> None:
        nums = sorted(ft.used)
        gaps = [n for n in range(1, nums[-1] + 1) if n not in ft.used]
        extra = nums[-1] + 1
        def free(n: str) -> bool:
            return bool(VALID_NAME.match(n)) and not in_use[n.lower()] and n.lower() not in taken

        for ap in sorted(ap_list, key=lambda a: a.mac or a.id):
            name = None
            while gaps and name is None:
                cand = ft.render(gaps.pop(0))
                name = cand if free(cand) else None
            while name is None and extra <= nums[-1] + 1000:
                cand = ft.render(extra)
                extra += 1
                name = cand if free(cand) else None
            if name is None:
                stats["no_convention"] += 1
                continue
            taken.add(name.lower())
            stats["from_ai_template"] += 1
            proposals.append(self._proposal(
                ap, name, 60, "ai_template", site_label.get(site),
                [f"{ft.matched} of {len(peers)} APs on {site_label.get(site, site)} follow the AI-learned template "
                 f"'{ft.template}' (every matching name rebuilt exactly); {name} is the next free number."],
                ["Template learned by AI from the floor's AP names; check it matches the local convention."]))

    @staticmethod
    def _previous_name(ap: Device, row, past: list, default_re) -> str | None:
        candidates = [h.old for h in reversed(past) if h.old]
        if row is not None and row.previous_hostname:
            candidates.insert(0, row.previous_hostname)
        for c in candidates:
            if not default_re.match(c) and VALID_NAME.match(c):
                return c
        return None

    def _proposal(self, ap: Device, name: str, conf: int, method: str, site: str | None,
                  lines: list[str], concerns: list[str]) -> Proposal:
        return Proposal(
            target_key=ap.id,
            target_label=f"{ap.hostname} ({ap.mgmt_ip or 'no ip'})",
            action_type="rename_ap",
            payload={"device_id": ap.id, "hostname": ap.hostname, "mac": ap.mac, "eth_mac": ap.eth_mac,
                     "mgmt_ip": ap.mgmt_ip, "hostname_new": name, "method": method, "site": site,
                     "auto_eligible": method == "history"},
            rule_confidence=conf,
            evidence=[{"signal": method, "label": METHOD_LABELS[method],
                       "available": True, "weight": 1.0, "detail": " ".join(lines), "buildings": [], "floors": [],
                       "agrees": True}],
            rationale=f"Rename {ap.hostname} to {name}. " + " ".join(lines),
            concerns=concerns,
            group_label=(site or "No site").split("/")[-2] if site and "/" in site else (site or "No site"),
            fingerprint=f"{ap.hostname}->{name}",
        )

    def apply_override(self, ctx: RunContext, item: WorkItem, override: dict) -> dict:
        name = (override.get("hostname_new") or "").strip()
        if not VALID_NAME.match(name):
            raise ValueError("AP names: 1-32 characters, letters, digits, '.', '_' or '-'")
        if re.match(DEFAULT_NAME_RE, name):
            raise ValueError("that is a factory-default name")
        from app.models import InventoryDevice
        from sqlalchemy import func, select
        clash = ctx.db.scalars(select(InventoryDevice.device_id).where(
            func.lower(InventoryDevice.hostname) == name.lower(), InventoryDevice.removed_at.is_(None),
            InventoryDevice.device_id != item.target_key)).first()
        if clash:
            raise ValueError("another device already has this name")
        return {"hostname_new": name}

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        if observed.get("removed_from_inventory"):
            return {**observed, "matched": None}
        return {**observed, "proposed": (item.payload or {}).get("hostname_new"),
                "matched": observed.get("actual_hostname") == (item.payload or {}).get("hostname_new")}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        cc = ctx.cc
        timeout = int(ctx.params.get("task_timeout_seconds", 600))
        results: dict[int, ExecResult] = {}
        for batch in chunks(items, int(ctx.params.get("rename_batch_size", 20))):
            renames = []
            for it in batch:
                pl = self.effective_payload(it)
                renames.append((Device(id=pl["device_id"], hostname=pl["hostname"], mgmt_ip=pl.get("mgmt_ip"),
                                       mac=pl.get("mac"), family="Unified AP"), pl["hostname_new"]))
            ctx.log(f"Renaming {len(renames)} AP(s) via Catalyst Center")
            try:
                res = cc.wait_for(cc.rename_aps(renames), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                results.update(fail_all(batch, f"rename failed: {exc}"))
                continue
            if not res.success:
                results.update(fail_all(batch, f"Catalyst Center task failed: {res.detail}"))
                continue
            try:
                now = cc.get_devices_by_id([d.id for d, _ in renames])
            except Exception as exc:  # noqa: BLE001
                now = None
                ctx.log(f"verification read failed: {exc}")
            for it, (dev, new) in zip(batch, renames):
                actual = now.get(dev.id).hostname if now and dev.id in now else None
                verified = None if now is None else actual == new
                if verified:
                    record_change(ctx.db, ctx.source.id, dev.id, "hostname", new)
                results[it.id] = ExecResult(
                    success=verified is not False,
                    detail=(f"renamed to {new} and verified" if verified else
                            "renamed (verification unavailable)" if verified is None else
                            f"task succeeded but the AP is named {actual}"),
                    verified=verified, data={"from": dev.hostname, "to": new, "actual": actual})
        return results
