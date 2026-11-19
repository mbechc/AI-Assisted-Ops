"""Evidence signals for "which site does this device belong to", shared by every site-related task.

Built once per run from the inventory snapshot plus per-AP data fetched through Catalyst Center
(AP configuration, RF neighbours). GeoIP is an optional extra signal: when no GeoIP source is
configured the other signals are used exactly as before.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from app.connectors.catalyst_center import CapabilityUnavailable
from app.connectors.catalyst_center.models import Device
from app.framework.base import RunContext
from app.inventory import Inventory
from app.store import cache_get, cache_set
from app.usecases.ap_site.learners import (
    KeyBuildingModel, NamePrefixModel, Prediction, SiteIndex, direct_building_match, normalize_tag, subnet_key,
)
from app.usecases.ap_site.naming import learn_site_naming, short_name
from app.usecases.ap_site.scoring import SIGNALS, SIGNAL_LABELS, SignalVote, rssi_weight, share

AI_NAMING_WEIGHT = 0.10
AI_NAMING_MAX_WEIGHT = 0.15
DEFAULT_OPTIONAL_WEIGHTS = {"geoip": 0.15, "ai_naming": AI_NAMING_WEIGHT}


class SiteSignals:
    def __init__(self, ctx: RunContext, inv: Inventory, index: SiteIndex, params: dict,
                 targets: list[Device], name_learning: str = "aps"):
        self.ctx, self.inv, self.index, self.p = ctx, inv, index, params
        self.by_id = {d.id: d for d in inv.devices}
        self.assignments = inv.assignments
        self.available = {"uplink": True, "rf": bool(params.get("rf_enabled", True)), "name": True,
                          "tag": bool(params.get("ap_config_enabled", True)), "subnet": True, "geoip": False,
                          "ai_naming": False}

        self.neighbors: dict[str, set[str]] = defaultdict(set)
        self.ports: dict[tuple[str, str], str] = {}
        for ln in inv.links:
            self.neighbors[ln.a_id].add(ln.b_id)
            self.neighbors[ln.b_id].add(ln.a_id)
            self.ports[(ln.a_id, ln.b_id)] = ln.a_port or ""
            self.ports[(ln.b_id, ln.a_id)] = ln.b_port or ""
        if not inv.links:
            self.available["uplink"] = False
            ctx.log("Uplink signal unavailable: no topology links in the inventory snapshot")

        assigned = [d for d in inv.devices if d.id in self.assignments]
        assigned_aps = [d for d in assigned if d.is_ap]
        name_src = assigned_aps if name_learning == "aps" else assigned
        name_pairs = [(d.hostname, self.assignments[d.id]) for d in name_src]
        self.name_model = NamePrefixModel(index, int(params.get("name_min_samples", 3)),
                                          float(params.get("name_min_purity", 0.8))).fit(name_pairs)
        self.ai_naming = None
        if float(params.get("weight_ai_naming", AI_NAMING_WEIGHT)) > 0 and params.get("ai_naming_enabled", True):
            self.ai_naming = learn_site_naming(ctx, index, name_pairs, name_learning)
            self.available["ai_naming"] = self.ai_naming is not None
        self.subnet_model = KeyBuildingModel(index).fit(
            [(subnet_key(d.mgmt_ip), self.assignments[d.id]) for d in assigned])

        self.configs: dict[str, dict] = {}
        self.tag_model = KeyBuildingModel(index)
        target_aps = [d for d in targets if d.is_ap]
        if self.available["tag"]:
            sample = assigned_aps[: int(params.get("ap_config_learning_sample", 1500))]
            seen = {d.id for d in target_aps}
            fetch = target_aps + [a for a in sample if a.id not in seen]
            configs = self._ap_configs(fetch, int(params.get("ap_config_cache_hours", 168)))
            if configs is None:
                self.available["tag"] = False
            else:
                self.configs = configs
                self.tag_model.fit([(normalize_tag(configs.get(a.id, {}).get("site_tag"))
                                     or normalize_tag(configs.get(a.id, {}).get("location")), self.assignments[a.id])
                                    for a in sample])

        self.rf: dict[str, list[dict]] = {}
        if self.available["rf"] and target_aps:
            rf = self._rf_neighbors(target_aps, int(params.get("rf_cache_hours", 20)))
            if rf is None:
                self.available["rf"] = False
            else:
                self.rf = rf
        elif not target_aps:
            self.available["rf"] = False

        self.by_name = {d.hostname.lower(): d.id for d in inv.devices}
        self.by_mac: dict[str, str] = {}
        for d in inv.devices:
            for m in (d.mac, d.eth_mac):
                if m:
                    self.by_mac[m.lower()] = d.id

        self.geoip = None
        self.geo_model = KeyBuildingModel(index)
        if float(params.get("weight_geoip", 0.15)) > 0:
            from app.connectors.geoip import load_provider
            self.geoip = load_provider(ctx.db)
            if self.geoip is not None:
                self.available["geoip"] = True
                self.geo_model.fit([(self.geoip.location_key(d.mgmt_ip), self.assignments[d.id]) for d in assigned])
                ctx.log(f"GeoIP signal: {self.geoip.describe()}")
            else:
                ctx.log("GeoIP signal: not configured (optional)")

        self.weights = {s: float(params.get(f"weight_{s}", DEFAULT_OPTIONAL_WEIGHTS.get(s, 0.0))) for s in SIGNALS}
        self.weights["ai_naming"] = min(self.weights["ai_naming"], AI_NAMING_MAX_WEIGHT)

    def log_available(self) -> None:
        self.ctx.log("Signals available: " + ", ".join(f"{SIGNAL_LABELS[k]}={'yes' if v else 'no'}"
                                                       for k, v in self.available.items()))

    # ---------------------------------------------------------------- votes

    def votes(self, dev: Device, exclude_self: bool = False) -> dict[str, SignalVote]:
        v = {
            "uplink": self._uplink_vote(dev),
            "rf": self._rf_vote(self.rf.get(dev.id)) if dev.is_ap else None,
            "name": self._name_vote(dev),
            "tag": self._tag_vote(self.configs.get(dev.id)) if dev.is_ap else None,
            "subnet": self._subnet_vote(dev),
            "geoip": self._geoip_vote(dev) if self.geoip is not None else None,
            "ai_naming": self._ai_naming_vote(dev) if self.ai_naming is not None else None,
        }
        return {k: x for k, x in v.items() if x is not None}

    def _uplink_vote(self, dev: Device) -> SignalVote | None:
        b_votes: dict[str, float] = defaultdict(float)
        f_votes: dict[str, float] = defaultdict(float)
        details = []
        for nid in self.neighbors.get(dev.id, ()):
            nd = self.by_id.get(nid)
            if nd is None or nd.is_ap or nid not in self.assignments:
                continue
            site = self.assignments[nid]
            b = self.index.building_of(site)
            if not b:
                continue
            b_votes[b] += 1
            if self.index.get(site) and self.index.get(site).type == "floor":
                f_votes[site] += 0.5
            port = self.ports.get((nid, dev.id)) or ""
            details.append(f"{nd.hostname}{' ' + port if port else ''} in {self.index.short(site)}")
        if not b_votes:
            return None
        n = sum(b_votes.values())
        return SignalVote("uplink", share(b_votes), {f: x / n for f, x in f_votes.items()},
                          "Connected to " + "; ".join(details[:3]))

    def _rf_vote(self, nbrs) -> SignalVote | None:
        if not nbrs:
            return None
        b_votes: dict[str, float] = defaultdict(float)
        f_votes: dict[str, float] = defaultdict(float)
        known = 0
        for n in nbrs:
            nid = n.get("neighbor_id")
            if not nid and n.get("neighbor_name"):
                nid = self.by_name.get(n["neighbor_name"].lower())
            if not nid and n.get("neighbor_mac"):
                nid = self.by_mac.get(n["neighbor_mac"].lower())
            site = self.assignments.get(nid) if nid else None
            b = self.index.building_of(site)
            if not b:
                continue
            w = rssi_weight(n.get("rssi"))
            known += 1
            b_votes[b] += w
            if self.index.get(site).type == "floor":
                f_votes[site] += w
        if not b_votes:
            return None
        damp = 1.0 if known >= 2 else 0.6
        total_w = sum(b_votes.values())
        top = max(f_votes, key=f_votes.get) if f_votes else None
        detail = f"{known} of {len(nbrs)} RF neighbours have a site"
        if top:
            detail += f"; strongest floor {self.index.short(top)}"
        return SignalVote("rf", {b: x / total_w * damp for b, x in b_votes.items()},
                          {f: x / total_w * damp for f, x in f_votes.items()}, detail)

    def _name_vote(self, dev: Device) -> SignalVote | None:
        pred = self.name_model.predict(dev.hostname)
        if not pred:
            return None
        site = self.index.get(pred.site_id)
        b = self.index.building_of(pred.site_id)
        if not b:
            return None
        floors = {pred.site_id: pred.purity} if site and site.type == "floor" else {}
        return SignalVote("name", {b: pred.purity}, floors,
                          f"Prefix '{pred.key}' maps to {self.index.short(pred.site_id)} "
                          f"({round(pred.purity * pred.samples)} of {pred.samples} devices)")

    def _ai_naming_vote(self, dev: Device) -> SignalVote | None:
        pred = self.ai_naming.predict(dev.hostname)
        if not pred:
            return None
        b, floor = pred
        strength = self.ai_naming.accuracy
        floors = {floor: self.ai_naming.floor_accuracy or 0.0} if floor and self.ai_naming.floor_accuracy else {}
        where = self.index.short(floor or b)
        return SignalVote("ai_naming", {b: strength}, floors,
                          f"AI-learned convention maps '{short_name(dev.hostname)}' to {where} "
                          f"(validated on {self.ai_naming.matched} assigned devices, {strength:.0%} correct)")

    def _tag_vote(self, cfg: dict | None) -> SignalVote | None:
        if not cfg:
            return None
        for fld, label in (("site_tag", "site tag"), ("location", "AP location")):
            raw = cfg.get(fld)
            pred = self.tag_model.predict(normalize_tag(raw)) or (
                direct_building_match(raw, self.index) if fld == "location" else None)
            if pred:
                return SignalVote("tag", {pred.site_id: pred.purity}, {},
                                  f"{label} '{raw}' maps to {self.index.short(pred.site_id)}"
                                  + (f" ({round(pred.purity * pred.samples)} of {pred.samples} APs)"
                                     if pred.samples > 1 else ""))
        return None

    def _subnet_vote(self, dev: Device) -> SignalVote | None:
        key = subnet_key(dev.mgmt_ip)
        pred = self.subnet_model.predict(key)
        if not pred:
            return None
        return SignalVote("subnet", {pred.site_id: pred.purity}, {},
                          f"Subnet {key} is mostly {self.index.short(pred.site_id)} "
                          f"({round(pred.purity * pred.samples)} of {pred.samples} devices)")

    def _geoip_vote(self, dev: Device) -> SignalVote | None:
        hit = self.geoip.lookup(dev.mgmt_ip)
        if hit is None:
            return None
        mapped = self.geoip.mapping_for(hit.key)
        pred: Prediction | None = None
        how = ""
        if mapped:
            b = self.index.building_of(mapped)
            if b:
                pred, how = Prediction(b, 0.9, 1, hit.key), "operator mapping"
        if pred is None:
            pred = self.geo_model.predict(hit.key)
            how = f"learned from {pred.samples} assigned devices" if pred else ""
        if pred is None:
            pred = direct_building_match(hit.key, self.index)
            how = "name match" if pred else ""
        if pred is None:
            return None
        return SignalVote("geoip", {pred.site_id: pred.purity}, {},
                          f"GeoIP {hit.network} '{hit.key}' maps to {self.index.short(pred.site_id)} ({how})")

    # ---------------------------------------------------------------- per-AP data through Catalyst Center

    def _ap_configs(self, aps: list[Device], cache_hours: int) -> dict[str, dict] | None:
        ctx = self.ctx
        out: dict[str, dict] = {}
        fetched = 0
        for ap in aps:
            key = f"cc:apcfg:{ctx.source.id}:{ap.id}"
            cached = cache_get(ctx.db, key)
            if cached is not None:
                out[ap.id] = cached
                continue
            try:
                cfg = ctx.cc.get_ap_config(ap)
            except CapabilityUnavailable as exc:
                ctx.log(f"AP configuration signal unavailable: {exc}")
                return None
            val = asdict(cfg) if cfg else {}
            cache_set(ctx.db, key, val, cache_hours * 3600)
            out[ap.id] = val
            fetched += 1
            if fetched % 200 == 0:
                ctx.db.flush()
                ctx.log(f"AP configuration: fetched {fetched}")
        ctx.log(f"AP configuration: {fetched} fetched, {len(out) - fetched} from cache")
        return out

    def _rf_neighbors(self, aps: list[Device], cache_hours: int) -> dict[str, list[dict]] | None:
        ctx = self.ctx
        out: dict[str, list[dict]] = {}
        fetched = 0
        for ap in aps:
            key = f"cc:rf:{ctx.source.id}:{ap.id}"
            cached = cache_get(ctx.db, key)
            if cached is not None:
                out[ap.id] = cached
                continue
            try:
                nbrs = ctx.cc.get_ap_rf_neighbors(ap)
            except CapabilityUnavailable as exc:
                ctx.log(f"RF neighbour signal unavailable: {exc}")
                return None
            val = [asdict(n) for n in (nbrs or [])]
            cache_set(ctx.db, key, val, cache_hours * 3600)
            out[ap.id] = val
            fetched += 1
            if fetched % 200 == 0:
                ctx.db.flush()
        ctx.log(f"RF neighbours: {fetched} fetched, {len(out) - fetched} from cache")
        return out


def llm_evidence_lines(sc, index: SiteIndex) -> list[str]:
    lines = []
    for e in sc.evidence:
        if not e["available"]:
            lines.append(f"- {e['label']}: {e['detail']}")
            continue
        b = ", ".join(f"{x['site']} ({x['strength']})" for x in e["buildings"]) or "none"
        f = ", ".join(f"{x['site']} ({x['strength']})" for x in e["floors"]) or "none"
        lines.append(f"- {e['label']} [weight {e['weight']}]: {e['detail']}. Building votes: {b}. Floor votes: {f}.")
    return lines
