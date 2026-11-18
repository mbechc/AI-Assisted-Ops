"""Models learned from already-assigned devices: name convention, AP tags/location, subnets."""
import ipaddress
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from app.connectors.catalyst_center.models import Site

IGNORED_TAGS = {"", "default", "default-site-tag", "default-policy-tag", "default location", "default-location",
                "unknown", "none", "n/a"}


class SiteIndex:
    def __init__(self, sites: list[Site]):
        self.by_id = {s.id: s for s in sites}
        self.children: dict[str, list[Site]] = defaultdict(list)
        for s in sites:
            if s.parent_id:
                self.children[s.parent_id].append(s)

    def get(self, site_id: str | None) -> Site | None:
        return self.by_id.get(site_id) if site_id else None

    def label(self, site_id: str | None) -> str:
        s = self.get(site_id)
        return s.hierarchy if s else "unknown"

    def building_of(self, site_id: str | None) -> str | None:
        s = self.get(site_id)
        while s is not None:
            if s.type == "building":
                return s.id
            s = self.get(s.parent_id)
        return None

    def floors_of(self, building_id: str) -> list[Site]:
        out, stack = [], list(self.children.get(building_id, []))
        while stack:
            s = stack.pop()
            if s.type == "floor":
                out.append(s)
            stack.extend(self.children.get(s.id, []))
        return sorted(out, key=lambda s: s.hierarchy)

    def short(self, site_id: str | None, depth: int = 2) -> str:
        return "/".join(self.label(site_id).split("/")[-depth:])


@dataclass
class Prediction:
    site_id: str
    purity: float
    samples: int
    key: str


def name_keys(hostname: str) -> list[str]:
    """Progressively shorter name prefixes, e.g. dk-cph-b03-f2-ap017 -> dk-cph-b03-f2-ap, dk-cph-b03-f2, ..."""
    base = (hostname or "").lower().split(".")[0]
    tokens = [t for t in re.split(r"[-_\s]+", base) if t]
    if not tokens:
        return []
    last = re.sub(r"\d+$", "", tokens[-1])
    tokens = tokens[:-1] + ([last] if last else [])
    return ["-".join(tokens[:n]) for n in range(len(tokens), 0, -1)]


class NamePrefixModel:
    def __init__(self, index: SiteIndex, min_samples: int = 3, min_purity: float = 0.8):
        self.index = index
        self.min_samples = min_samples
        self.min_purity = min_purity
        self.site_counts: dict[str, Counter] = defaultdict(Counter)
        self.building_counts: dict[str, Counter] = defaultdict(Counter)

    def fit(self, pairs: list[tuple[str, str]]) -> "NamePrefixModel":
        for hostname, site_id in pairs:
            building = self.index.building_of(site_id)
            for key in name_keys(hostname):
                self.site_counts[key][site_id] += 1
                if building:
                    self.building_counts[key][building] += 1
        return self

    def predict(self, hostname: str) -> Prediction | None:
        for key in name_keys(hostname):
            sc = self.site_counts.get(key)
            if sc:
                n = sum(sc.values())
                site, c = sc.most_common(1)[0]
                if n >= self.min_samples and c / n >= self.min_purity:
                    return Prediction(site, c / n, n, key)
            bc = self.building_counts.get(key)
            if bc:
                n = sum(bc.values())
                b, c = bc.most_common(1)[0]
                if n >= self.min_samples and c / n >= self.min_purity:
                    return Prediction(b, c / n, n, key)
        return None


class KeyBuildingModel:
    """Maps a categorical key (AP site tag / location string / subnet) to a building."""

    def __init__(self, index: SiteIndex, min_samples: int = 3, min_purity: float = 0.7):
        self.index = index
        self.min_samples = min_samples
        self.min_purity = min_purity
        self.counts: dict[str, Counter] = defaultdict(Counter)

    def fit(self, pairs: list[tuple[str | None, str]]) -> "KeyBuildingModel":
        for key, site_id in pairs:
            b = self.index.building_of(site_id)
            if key and b:
                self.counts[key][b] += 1
        return self

    def predict(self, key: str | None) -> Prediction | None:
        c = self.counts.get(key) if key else None
        if not c:
            return None
        n = sum(c.values())
        b, k = c.most_common(1)[0]
        if n >= self.min_samples and k / n >= self.min_purity:
            return Prediction(b, k / n, n, key)
        return None


def normalize_tag(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    return None if v in IGNORED_TAGS else v


def subnet_key(ip: str | None, prefix: int = 24) -> str | None:
    try:
        return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False)) if ip else None
    except ValueError:
        return None


def direct_building_match(text: str | None, index: SiteIndex) -> Prediction | None:
    """A location string that names exactly one building (e.g. 'B03 Floor 2' or 'Copenhagen HQ')."""
    t = normalize_tag(text)
    if not t:
        return None
    hits = [s for s in index.by_id.values() if s.type == "building" and len(s.name) >= 3
            and re.search(rf"(^|[^a-z0-9]){re.escape(s.name.lower())}([^a-z0-9]|$)", t)]
    if len(hits) == 1:
        return Prediction(hits[0].id, 0.9, 1, f"location '{text}'")
    return None
