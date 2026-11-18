"""Record sanitized Catalyst Center responses for every capability candidate.

Run against each lab release; the output directory (data/fixtures/<release>/)
documents exactly which endpoints and response shapes each release exposes and
feeds the contract tests. Credentials and tokens are never written.
"""
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

from app.connectors.catalyst_center import CapabilityUnavailable
from app.db import session_scope
from app.models import DataSource
from app.sources import build_cc_client, save_capabilities

SENSITIVE_KEYS = re.compile(r"(password|secret|token|community|snmp.*(auth|priv)|key)$", re.I)


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SENSITIVE_KEYS.search(k) else _sanitize(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj[:200]]
    return obj


def record(source_id: int, out_root: str, max_objects: int = 25) -> int:
    with session_scope() as db:
        source = db.get(DataSource, source_id)
        if source is None:
            print(f"source {source_id} not found")
            return 1
        seen = Counter()
        records: list[dict] = []

        def recorder(method, path, params, body, status, response):
            norm = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "{id}", path)
            seen[norm] += 1
            if seen[norm] > 3:
                return
            records.append({"method": method, "path": path, "template": norm, "params": params,
                            "request": _sanitize(body), "status": status, "response": _sanitize(response)})

        client = build_cc_client(source, recorder=recorder)
        client._ops = {}
        version = client.connect(redetect=True)
        release = ".".join(map(str, version)) if version else "unknown"
        out_dir = os.path.join(out_root, release)
        os.makedirs(out_dir, exist_ok=True)
        print(f"Recording Catalyst Center {release} from {source.base_url} into {out_dir}")

        def attempt(name, fn):
            try:
                res = fn()
                print(f"  {name}: ok ({len(res) if hasattr(res, '__len__') else 'value'})")
                return res
            except CapabilityUnavailable as exc:
                print(f"  {name}: unavailable ({exc})")
            except Exception as exc:  # noqa: BLE001
                print(f"  {name}: error {exc}")
            return None

        sites = attempt("get_sites", client.get_sites) or []
        devices = attempt("get_devices", client.get_devices) or []
        attempt("get_site_assignments", lambda: client.get_site_assignments(
            [s for s in sites if s.type in ("building", "floor")][:max_objects]))
        attempt("get_links", client.get_links)
        aps = [d for d in devices if d.is_ap][:max_objects]
        attempt("get_ap_config", lambda: [client.get_ap_config(a) for a in aps])
        attempt("get_ap_rf_neighbors", lambda: [client.get_ap_rf_neighbors(a) for a in aps])

        meta = {"release": release, "version_source": client.version_source, "base_url": source.base_url,
                "recorded_at": datetime.now(timezone.utc).isoformat(), "capabilities": client.capability_report(),
                "api_calls": client.http.calls}
        with open(os.path.join(out_dir, "meta.json"), "w") as fh:
            json.dump(meta, fh, indent=2, default=str)
        with open(os.path.join(out_dir, "responses.json"), "w") as fh:
            json.dump(records, fh, indent=1, default=str)
        save_capabilities(db, source, client)
        client.close()
        print(f"Wrote {len(records)} responses; capability summary in meta.json")
    return 0
