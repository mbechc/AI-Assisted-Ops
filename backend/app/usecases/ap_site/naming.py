"""AI-learned naming conventions (optional; the rule-based NamePrefixModel always runs).

The AI sees a sample of already-assigned hostnames with their sites and returns a structured mapping
(regex with named groups + token aliases). The mapping is never trusted as-is: it is replayed against
every assigned device and only used when it predicts the actual building for at least
MIN_ACCURACY of the devices it matches. It then becomes one extra, optional evidence signal whose
weight is capped and which can never carry a proposal on its own (see scoring.CAPPED_SIGNALS).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.framework.base import RunContext
from app.llm.base import LLMUnavailable
from app.store import cache_get, cache_set
from app.usecases.ap_site.learners import SiteIndex

MIN_ACCURACY = 0.95
MIN_MATCHED = 10
SAMPLE_PER_BUILDING = 6
SAMPLE_MAX = 160
CACHE_TTL = 30 * 86400
MAX_PATTERN_LEN = 200
# Nested quantifiers such as (a+)+ can backtrack catastrophically; AI patterns with them are rejected.
_NESTED_QUANT = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)[+*{]")

SITE_SYSTEM = (
    "You analyse the hostname convention of network devices in Cisco Catalyst Center. You receive example "
    "hostnames with the building and floor each device is assigned to, and the list of building names. "
    "Find how the hostname encodes the building and (if present) the floor. Return a Python regular expression "
    "applied to the lower-case short hostname (text before the first dot), with a named group 'building' and, "
    "if the floor is encoded, a named group 'floor'. Use only simple constructs (literals, character classes, "
    "single quantifiers); no nested quantifiers. Return building_aliases mapping each building token seen in "
    "hostnames to the exact building name from the list, and floor_aliases mapping floor tokens to floor names "
    "when they differ. If the names do not encode the site, return an empty pattern. Never invent buildings."
)
SITE_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string"},
        "building_aliases": {"type": "array", "items": {
            "type": "object", "properties": {"token": {"type": "string"}, "building": {"type": "string"}},
            "required": ["token", "building"]}},
        "floor_aliases": {"type": "array", "items": {
            "type": "object", "properties": {"token": {"type": "string"}, "floor": {"type": "string"}},
            "required": ["token", "floor"]}},
        "explanation": {"type": "string"},
    },
    "required": ["pattern", "building_aliases", "floor_aliases", "explanation"],
}

FLOOR_SYSTEM = (
    "You derive access point naming templates. For each floor you receive the names of the APs already on it. "
    "Return, per floor, a Python regular expression with a named group 'num' (the AP's running number) that "
    "matches the names, and a template that rebuilds a name from a number, using {num} with an optional width, "
    "e.g. 'cph-b2-f3-ap{num:02d}-lobby'. Use simple constructs only. If no consistent convention exists for a "
    "floor, return an empty pattern for it."
)
FLOOR_SCHEMA = {
    "type": "object",
    "properties": {"floors": {"type": "array", "items": {
        "type": "object",
        "properties": {"floor": {"type": "string"}, "pattern": {"type": "string"}, "template": {"type": "string"}},
        "required": ["floor", "pattern", "template"]}}},
    "required": ["floors"],
}


def short_name(hostname: str | None) -> str:
    return (hostname or "").lower().split(".")[0]


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def digits(text: str | None) -> str | None:
    m = re.search(r"\d+", text or "")
    return str(int(m.group())) if m else None


def safe_regex(pattern: str, groups: tuple[str, ...]) -> re.Pattern | None:
    if not pattern or len(pattern) > MAX_PATTERN_LEN or _NESTED_QUANT.search(pattern):
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    return rx if all(g in rx.groupindex for g in groups) else None


@dataclass
class SiteNaming:
    """A validated AI mapping: hostname -> (building_id, floor_id | None)."""
    rx: re.Pattern
    building_alias: dict[str, str]  # token -> building_id
    floor_alias: dict[str, str]  # token -> normalised floor name
    index: SiteIndex
    accuracy: float = 0.0
    matched: int = 0
    total: int = 0
    explanation: str = ""
    floor_accuracy: float | None = None

    def predict(self, hostname: str) -> tuple[str, str | None] | None:
        m = self.rx.search(short_name(hostname))
        if not m:
            return None
        token = norm(m.group("building"))
        b = self.building_alias.get(token)
        if b is None:
            return None
        floor_id = None
        if "floor" in self.rx.groupindex and m.group("floor"):
            floor_id = self._floor(b, m.group("floor"))
        return b, floor_id

    def _floor(self, building_id: str, token: str) -> str | None:
        t = norm(token)
        want = self.floor_alias.get(t, t)
        floors = self.index.floors_of(building_id)
        hits = [f for f in floors if norm(f.name) == want]
        if not hits and digits(token) is not None:
            hits = [f for f in floors if digits(f.name) == digits(token)]
        return hits[0].id if len(hits) == 1 else None

    def describe(self) -> str:
        fl = "" if self.floor_accuracy is None else f", floor {self.floor_accuracy:.0%}"
        return (f"pattern {self.rx.pattern!r}: building correct for {self.accuracy:.0%} of {self.matched} matching "
                f"devices{fl} ({self.total} assigned devices checked)")


def _sample(pairs: list[tuple[str, str]], index: SiteIndex) -> list[tuple[str, str]]:
    by_b: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for h, s in sorted(pairs):
        b = index.building_of(s)
        if b and h:
            by_b[b].append((h, s))
    out: list[tuple[str, str]] = []
    for items in by_b.values():
        step = max(1, len(items) // SAMPLE_PER_BUILDING)
        out.extend(items[::step][:SAMPLE_PER_BUILDING])
    return out[:SAMPLE_MAX]


def _site_prompt(sample: list[tuple[str, str]], index: SiteIndex) -> str:
    buildings = sorted({index.building_of(s) for _, s in sample} - {None})
    lines = ["Buildings (exact names):"] + [f"- {index.get(b).name} ({index.label(b)})" for b in buildings[:200]]
    lines.append("Examples (hostname -> building / floor):")
    for h, s in sample:
        site = index.get(s)
        b = index.get(index.building_of(s))
        floor = site.name if site and site.type == "floor" else "-"
        lines.append(f"- {short_name(h)} -> {b.name if b else '?'} / {floor}")
    return "\n".join(lines)


def build_site_naming(raw: dict, index: SiteIndex) -> SiteNaming | None:
    rx = safe_regex(str(raw.get("pattern") or ""), ("building",))
    if rx is None:
        return None
    by_name: dict[str, list[str]] = defaultdict(list)
    for s in index.by_id.values():
        if s.type == "building":
            by_name[norm(s.name)].append(s.id)
    alias: dict[str, str] = {}
    for a in raw.get("building_aliases") or []:
        ids = by_name.get(norm(a.get("building")), [])
        if len(ids) == 1 and norm(a.get("token")):
            alias[norm(a["token"])] = ids[0]
    for name, ids in by_name.items():
        if len(ids) == 1:
            alias.setdefault(name, ids[0])
    floor_alias = {norm(a.get("token")): norm(a.get("floor")) for a in raw.get("floor_aliases") or []
                   if norm(a.get("token")) and norm(a.get("floor"))}
    return SiteNaming(rx, alias, floor_alias, index, explanation=str(raw.get("explanation") or "")[:500])


def validate(naming: SiteNaming, pairs: list[tuple[str, str]]) -> SiteNaming:
    ok = matched = f_ok = f_n = 0
    for h, s in pairs:
        pred = naming.predict(h)
        if pred is None:
            continue
        matched += 1
        actual_b = naming.index.building_of(s)
        ok += pred[0] == actual_b
        site = naming.index.get(s)
        if pred[1] is not None and site is not None and site.type == "floor":
            f_n += 1
            f_ok += pred[1] == s
    naming.matched, naming.total = matched, len(pairs)
    naming.accuracy = ok / matched if matched else 0.0
    naming.floor_accuracy = f_ok / f_n if f_n else None
    return naming


def learn_site_naming(ctx: RunContext, index: SiteIndex, pairs: list[tuple[str, str]], scope: str) -> SiteNaming | None:
    """One AI call per inventory change; returns a mapping only when it passes validation."""
    if ctx.llm is None or ctx.source is None or len(pairs) < MIN_MATCHED:
        return None
    sample = _sample(pairs, index)
    sig = hashlib.sha256(json.dumps(sorted(pairs)).encode()).hexdigest()
    key = f"ai:naming:{ctx.source.id}:{scope}:{ctx.llm.name}"
    cached = cache_get(ctx.db, key)
    if cached and cached.get("sig") == sig:
        raw = cached.get("raw")
        if not raw:
            ctx.log("AI naming convention: none found (cached until the inventory changes)")
            return None
    else:
        try:
            raw = ctx.llm.chat_json(SITE_SYSTEM, _site_prompt(sample, index), SITE_SCHEMA, num_ctx=8192)
        except LLMUnavailable as exc:
            ctx.log(f"AI naming convention skipped: {ctx.llm.label} unavailable ({exc})")
            return None
        except Exception as exc:  # noqa: BLE001 - a bad AI answer must not fail the run
            ctx.log(f"AI naming convention skipped: unusable answer ({exc})")
            return None
    naming = build_site_naming(raw or {}, index)
    if naming is not None:
        validate(naming, pairs)
    passed = naming is not None and naming.matched >= MIN_MATCHED and naming.accuracy >= MIN_ACCURACY
    cache_set(ctx.db, key, {"sig": sig, "raw": raw if passed else None}, CACHE_TTL)
    if naming is None:
        ctx.log("AI naming convention: no usable pattern returned; rule-based name model only")
        return None
    if not passed:
        ctx.log(f"AI naming convention rejected by validation ({naming.describe()}); rule-based name model only")
        return None
    ctx.log(f"AI naming convention accepted: {naming.describe()}")
    return naming


# ------------------------------------------------------------------ AP name templates per floor

@dataclass
class FloorTemplate:
    rx: re.Pattern
    template: str
    width: int
    used: set[int] = field(default_factory=set)
    matched: int = 0

    def render(self, num: int) -> str:
        return self.template.replace("{num}", str(num)).replace(f"{{num:0{self.width}d}}", f"{num:0{self.width}d}")


_TEMPLATE = re.compile(r"^[A-Za-z0-9._-]*\{num(?::0(\d)d)?\}[A-Za-z0-9._-]*$")


def build_floor_template(pattern: str, template: str, names: list[str]) -> FloorTemplate | None:
    """Accept only when the regex matches most names and the template rebuilds every matched name exactly."""
    rx = safe_regex(pattern, ("num",))
    t = _TEMPLATE.match(template or "")
    if rx is None or t is None:
        return None
    width = int(t.group(1) or 0)
    ft = FloorTemplate(rx, template, width)
    lowered = [n.lower() for n in names]
    for n in lowered:
        m = rx.fullmatch(n)
        if not m:
            continue
        try:
            num = int(m.group("num"))
        except (TypeError, ValueError):
            return None
        if ft.render(num).lower() != n:
            return None
        ft.used.add(num)
        ft.matched += 1
    if ft.matched < 3 or ft.matched / max(1, len(lowered)) < 0.6:
        return None
    return ft


def learn_floor_templates(ctx: RunContext, floors: dict[str, list[str]]) -> dict[str, FloorTemplate]:
    """One AI call for every floor where the rule-based '<prefix><number>' convention found nothing."""
    if ctx.llm is None or not floors:
        return {}
    items = list(floors.items())[:25]
    prompt = "\n".join(f"Floor {label}: " + ", ".join(sorted(names)[:30]) for label, names in items)
    try:
        raw = ctx.llm.chat_json(FLOOR_SYSTEM, prompt, FLOOR_SCHEMA, num_ctx=8192)
    except LLMUnavailable as exc:
        ctx.log(f"AI naming templates skipped: {ctx.llm.label} unavailable ({exc})")
        return {}
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"AI naming templates skipped: unusable answer ({exc})")
        return {}
    out: dict[str, FloorTemplate] = {}
    for f in (raw or {}).get("floors") or []:
        label = f.get("floor")
        if label in floors:
            ft = build_floor_template(str(f.get("pattern") or ""), str(f.get("template") or ""), floors[label])
            if ft:
                out[label] = ft
    ctx.log(f"AI naming templates: {len(out)} of {len(items)} floors validated")
    return out
