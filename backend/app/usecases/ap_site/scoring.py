"""Pure scoring: weighted evidence -> proposed site, candidates and rule confidence."""
import math
from collections import defaultdict
from dataclasses import dataclass, field

from app.usecases.ap_site.learners import SiteIndex

SIGNALS = ("uplink", "rf", "name", "tag", "subnet", "geoip")
# Optional signals add evidence when configured but never lower coverage when absent.
OPTIONAL_SIGNALS = {"geoip"}
GEOIP_ONLY_MAX_CONFIDENCE = 60
SIGNAL_LABELS = {
    "uplink": "Wired uplink (CDP/LLDP)",
    "rf": "RF neighbours",
    "name": "Name convention",
    "tag": "AP site tag / location",
    "subnet": "Management subnet",
    "geoip": "GeoIP (subnet location)",
}


@dataclass
class SignalVote:
    signal: str
    buildings: dict[str, float] = field(default_factory=dict)  # building_id -> strength 0..1
    floors: dict[str, float] = field(default_factory=dict)  # floor_id -> strength 0..1
    detail: str = ""


@dataclass
class Score:
    building_id: str | None
    site_id: str | None  # floor when determined, else building
    floor_determined: bool
    confidence: int
    building_scores: dict[str, float]
    floor_scores: dict[str, float]
    candidate_floors: list[dict]
    evidence: list[dict]
    concerns: list[str]
    auto_eligible: bool = True
    margin: float = 0.0
    coverage: float = 1.0


def rssi_weight(rssi: float | None) -> float:
    if rssi is None:
        return 0.5
    return max(0.1, min(1.0, (rssi + 95.0) / 45.0))


def share(votes: dict[str, float]) -> dict[str, float]:
    total = sum(votes.values())
    return {k: v / total for k, v in votes.items()} if total > 0 else {}


def _top2(scores: dict[str, float]) -> tuple[tuple[str | None, float], float]:
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    first = ranked[0] if ranked else (None, 0.0)
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return first, second


def score(votes: dict[str, SignalVote], available: dict[str, bool], weights: dict[str, float],
          index: SiteIndex, require_floor: bool = True) -> Score:
    w_avail = {s: weights.get(s, 0.0) for s in SIGNALS if available.get(s)}
    w_total = sum(w_avail.values()) or 1.0
    w_norm = {s: w / w_total for s, w in w_avail.items()}
    core = [s for s in SIGNALS if s not in OPTIONAL_SIGNALS]
    coverage = (sum(weights.get(s, 0.0) for s in core if available.get(s))
                / (sum(weights.get(s, 0.0) for s in core) or 1.0))

    building_scores: dict[str, float] = defaultdict(float)
    for s, w in w_norm.items():
        v = votes.get(s)
        if v:
            for b, strength in v.buildings.items():
                building_scores[b] += w * strength

    (b1, s1), s2 = _top2(building_scores)
    concerns: list[str] = []
    evidence = _evidence(votes, available, w_norm, index, b1)

    if b1 is None or s1 <= 0:
        return Score(None, None, False, 0, {}, {}, [], evidence,
                     ["No usable evidence: no uplink, RF, naming, tag or subnet signal points to a site."])

    margin = (s1 - s2) / s1
    conf_b = math.sqrt(min(1.0, s1)) * (0.5 + 0.5 * margin) * (0.85 + 0.15 * coverage)

    agreeing = [s for s in w_norm if votes.get(s) and votes[s].buildings.get(b1, 0) >= 0.5]
    conflicting = [s for s in w_norm if votes.get(s) and votes[s].buildings
                   and max(votes[s].buildings, key=votes[s].buildings.get) != b1
                   and max(votes[s].buildings.values()) >= 0.5]
    if len(agreeing) <= 1:
        concerns.append(f"Building supported by a single signal ({SIGNAL_LABELS[agreeing[0]] if agreeing else 'weak evidence'}).")
    for s in conflicting:
        alt = max(votes[s].buildings, key=votes[s].buildings.get)
        concerns.append(f"{SIGNAL_LABELS[s]} points to a different building: {index.short(alt)}.")
    if coverage < 0.999:
        missing = [SIGNAL_LABELS[s] for s in core if not available.get(s)]
        concerns.append("Signals not available on this Catalyst Center release/data: " + ", ".join(missing) + ".")

    floors = index.floors_of(b1)
    floor_ids = {f.id for f in floors}
    floor_scores: dict[str, float] = defaultdict(float)
    floor_w = 0.0
    for s, w in w_norm.items():
        v = votes.get(s)
        fv = {f: st for f, st in (v.floors.items() if v else []) if f in floor_ids}
        if fv:
            floor_w += w
            for f, st in fv.items():
                floor_scores[f] += w * st
    if floor_w > 0:
        floor_scores = defaultdict(float, {f: sc / floor_w for f, sc in floor_scores.items()})

    candidates = [{"site_id": f.id, "hierarchy": f.hierarchy, "score": round(floor_scores.get(f.id, 0.0), 3)}
                  for f in floors]
    candidates.sort(key=lambda c: (-c["score"], c["hierarchy"]))

    site_id, floor_determined = b1, False
    if len(floors) == 1:
        site_id, floor_determined, floor_factor = floors[0].id, True, 1.0
    elif floors:
        (f1, fs1), fs2 = _top2(floor_scores)
        f_margin = (fs1 - fs2) / fs1 if fs1 > 0 else 0.0
        if f1 and fs1 >= 0.5 and f_margin >= 0.4:
            site_id, floor_determined = f1, True
            # Agreeing floor evidence saturates at 0.6; the uplink alone only ever casts a half vote.
            floor_factor = 0.6 + 0.4 * min(1.0, fs1 / 0.6) * f_margin
            floor_sources = [s for s in w_norm if votes.get(s) and votes[s].floors.get(f1)]
            if floor_sources == ["uplink"]:
                floor_factor *= 0.9
                concerns.append("Floor inferred from the uplink switch location only.")
        else:
            floor_factor = 0.75
            concerns.append("Building identified but the floor is ambiguous; choose a floor.")
    else:
        floor_factor = 0.9 if not require_floor else 0.75
        if require_floor:
            concerns.append("Building has no floors in Catalyst Center; APs need a floor.")

    confidence = int(round(100 * conf_b * floor_factor))
    auto_eligible = True
    if agreeing == ["geoip"]:
        confidence = min(confidence, GEOIP_ONLY_MAX_CONFIDENCE)
        auto_eligible = False
        concerns.append(f"Only GeoIP supports this site: confidence capped at {GEOIP_ONLY_MAX_CONFIDENCE} "
                        "and never executed automatically.")
    return Score(b1, site_id, floor_determined, max(0, min(100, confidence)), dict(building_scores),
                 dict(floor_scores), candidates[:8], evidence, concerns, auto_eligible, margin, coverage)


def _evidence(votes: dict[str, SignalVote], available: dict[str, bool], w_norm: dict[str, float],
              index: SiteIndex, best: str | None) -> list[dict]:
    out = []
    for s in SIGNALS:
        v = votes.get(s)
        top = sorted((v.buildings.items() if v else []), key=lambda kv: -kv[1])[:3]
        floors = sorted((v.floors.items() if v else []), key=lambda kv: -kv[1])[:3]
        out.append({
            "signal": s,
            "label": SIGNAL_LABELS[s],
            "available": bool(available.get(s)),
            "weight": round(w_norm.get(s, 0.0), 3),
            "detail": v.detail if v else (("not configured" if s in OPTIONAL_SIGNALS else "not available")
                                          if not available.get(s) else "no data for this device"),
            "buildings": [{"site_id": b, "site": index.short(b), "strength": round(st, 3)} for b, st in top],
            "floors": [{"site_id": f, "site": index.short(f), "strength": round(st, 3)} for f, st in floors],
            "agrees": bool(v and best and v.buildings.get(best, 0) >= 0.5),
        })
    return out
