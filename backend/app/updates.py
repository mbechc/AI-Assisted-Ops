"""Appliance updates. The backend only reads the host updater's result files and queues requests;
scripts/update.sh on the host does every network test, download and install (see README, Updates)."""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
CHANNELS = ("production", "development")
RELEASE_CHECK_HOURS = 24
# A queued request the host has not picked up after this long means the updater is not running.
UPDATER_STALE_SECONDS = 120


def update_dir() -> Path:
    return Path(get_settings().update_dir)


def _read(name: str):
    try:
        return json.loads((update_dir() / name).read_text())
    except (OSError, ValueError):
        return None


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def version_key(v: str) -> tuple:
    """Order: 2026.11.0 > 2026.11.0-dev.3 > 2026.11.0-dev.2 > 0.2.5 (calendar versions; 0.x.y came before)."""
    main, _, pre = v.partition("-")
    nums = tuple(int(p) for p in main.split("."))
    if not pre:
        return nums + (1,)
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return nums + (0,) + parts


def version_gt(a: str, b: str) -> bool:
    return version_key(a) > version_key(b)


def releases_for(channel: str, raw) -> list[dict]:
    """Releases offered on a track, newest first. Production never sees pre-releases."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        tag = str(r.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
        if r.get("draft") or not VERSION_RE.match(version):
            continue
        pre = bool(r.get("prerelease"))
        if channel == "production" and pre:
            continue
        out.append({"version": version, "tag": tag, "prerelease": pre, "name": r.get("name") or tag,
                    "published_at": r.get("published_at"), "notes": (r.get("body") or "")[:20000],
                    "url": r.get("html_url")})
    out.sort(key=lambda x: version_key(x["version"]), reverse=True)
    return out


def available_update(current: str, channel: str, raw) -> dict | None:
    offered = releases_for(channel, raw)
    if not offered or not VERSION_RE.match(current):
        return None
    return offered[0] if version_gt(offered[0]["version"], current) else None


def pending() -> list[dict]:
    d = update_dir() / "requests"
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    now = time.time_ns()
    for n in names:
        stamp, _, action = n.partition("-")
        if stamp.isdigit():
            out.append({"action": action, "age_seconds": max(0, (now - int(stamp)) // 1_000_000_000)})
    return out


LLM_ACTIONS = ("llm-enable", "llm-disable", "llm-remove")


def queue(action: str, arg: str | None = None) -> None:
    """Drop a request for the host updater. The file name is the whole request; contents are ignored."""
    if action not in ("check", "connectivity", "channel", "install") + LLM_ACTIONS:
        raise ValueError(action)
    if action in LLM_ACTIONS and arg:
        raise ValueError(arg)
    if action == "channel" and arg not in CHANNELS:
        raise ValueError(arg)
    if action == "install" and not (arg and VERSION_RE.match(arg)):
        raise ValueError(arg)
    d = update_dir() / "requests"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{action}" + (f"-{arg}" if arg else "")
    fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)


def llm_state() -> dict:
    """Local LLM container/model state as reported by the host updater (data/update/llm.json).

    state: absent (never enabled) | queued | installing | pulling | ready | failed | disabled | removed
    """
    raw = _read("llm.json") or {}
    queued = [p for p in pending() if p["action"] in LLM_ACTIONS]
    state = raw.get("state") or "absent"
    if queued:
        state = "queued"
    return {
        "state": state,
        "model": raw.get("model") or get_settings().ollama_model,
        "updated_at": raw.get("updated_at"),
        "error": raw.get("error"),
        "pending": [p["action"] for p in queued],
        "updater_stale": any(p["age_seconds"] > UPDATER_STALE_SECONDS for p in queued),
    }


def llm_log_tail(lines: int = 200) -> str:
    try:
        text = (update_dir() / "llm.log").read_text(errors="replace")
    except OSError:
        return ""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return "\n".join(text.splitlines()[-lines:])


def connectivity(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    interval = max(1, get_settings().steward_connectivity_interval_min)
    stale_after = timedelta(minutes=max(30, 2 * interval))
    c = _read("connectivity.json") or {}
    checked = _ts(c.get("checked_at"))
    endpoints = c.get("endpoints") or []
    failed = [e.get("host") for e in endpoints if not e.get("ok")]
    if checked is None:
        status, reason = "unknown", "The host updater has not reported yet (is steward-updater.path installed?)."
    elif now - checked > stale_after:
        status, reason = "unknown", f"No result from the host updater for {int((now - checked).total_seconds() // 60)} minutes."
    else:
        status = "connected" if c.get("status") == "connected" and not failed else "airgapped"
        reason = None
    return {"status": status, "checked_at": c.get("checked_at"), "endpoints": endpoints, "failed": failed,
            "reason": reason, "interval_min": interval}


def state() -> dict:
    s = get_settings()
    host = _read("status.json") or {}
    meta = _read("releases-meta.json") or {}
    install = _read("install.json")
    current = s.steward_version
    channel = host.get("channel") if host.get("channel") in CHANNELS else s.steward_update_channel
    queued = pending()
    channel_pending = next((p["action"].split("-", 1)[1] for p in reversed(queued)
                            if p["action"].startswith("channel-")), None)
    available = available_update(current, channel, _read("releases.json"))
    install_running = bool(install and install.get("state") == "running") or any(
        p["action"].startswith("install-") for p in queued)
    return {
        "current_version": current,
        "channel": channel,
        "channel_pending": channel_pending,
        "repo": host.get("repo") or s.steward_update_repo,
        "connectivity": connectivity(),
        "releases_checked_at": meta.get("checked_at"),
        "releases_error": meta.get("error"),
        "available": available,
        "install": install,
        "install_running": install_running,
        "pending": queued,
        "updater_stale": any(p["age_seconds"] > UPDATER_STALE_SECONDS for p in queued),
        "updater_installed": host.get("updater_version") is not None,
    }


def schedule_host_checks(now: datetime | None = None) -> list[str]:
    """Called by the scheduler: keep the connectivity result fresh and look for releases once a day."""
    if not update_dir().is_dir():
        return []
    now = now or datetime.now(timezone.utc)
    queued = {p["action"] for p in pending()}
    if queued & {"check", "connectivity"}:
        return []
    meta = _ts((_read("releases-meta.json") or {}).get("checked_at"))
    if meta is None or now - meta >= timedelta(hours=RELEASE_CHECK_HOURS):
        queue("check")
        return ["check"]
    checked = _ts((_read("connectivity.json") or {}).get("checked_at"))
    if checked is None or now - checked >= timedelta(minutes=max(1, get_settings().steward_connectivity_interval_min)):
        queue("connectivity")
        return ["connectivity"]
    return []


def log_tail(lines: int = 400) -> str:
    try:
        text = (update_dir() / "update.log").read_text(errors="replace")
    except OSError:
        return ""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return "\n".join(text.splitlines()[-lines:])
