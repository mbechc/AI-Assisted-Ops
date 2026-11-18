"""Read-only Docker API access through the logproxy (tecnativa/docker-socket-proxy, CONTAINERS=1 only).

Only containers of this compose project are ever listed or read, so the Runners and Logs pages cannot
be used to look at other services on the host.
"""
from __future__ import annotations

import json
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

import httpx

from app.config import get_settings

LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")
_stats_cache: dict[str, tuple[float, dict]] = {}


class DockerUnavailable(Exception):
    pass


def _client(timeout: float | None = 5.0) -> httpx.Client:
    return httpx.Client(base_url=get_settings().logproxy_url, timeout=timeout)


def _project_filter() -> str:
    return json.dumps({"label": [f"com.docker.compose.project={get_settings().compose_project}"]})


def list_containers() -> list[dict]:
    """Project containers with service name, state, health, restart count and start time."""
    try:
        with _client() as c:
            rows = c.get("/containers/json", params={"all": "1", "filters": _project_filter()})
            rows.raise_for_status()
            out = []
            for r in rows.json():
                labels = r.get("Labels") or {}
                info = c.get(f"/containers/{r['Id']}/json").json()
                state = info.get("State") or {}
                out.append({
                    "id": r["Id"][:12], "full_id": r["Id"], "name": (r.get("Names") or ["?"])[0].lstrip("/"),
                    "service": labels.get("com.docker.compose.service", ""), "image": r.get("Image"),
                    "state": state.get("Status"), "health": (state.get("Health") or {}).get("Status"),
                    "restarts": info.get("RestartCount", 0), "started_at": state.get("StartedAt"),
                    "oom_killed": state.get("OOMKilled", False), "exit_code": state.get("ExitCode"),
                    "mem_limit": (info.get("HostConfig") or {}).get("Memory") or None,
                })
            return sorted(out, key=lambda x: x["service"])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise DockerUnavailable(str(exc)) from exc


def _stats_one(cid: str) -> dict:
    cached = _stats_cache.get(cid)
    if cached and time.time() - cached[0] < 10:
        return cached[1]
    with _client(timeout=8) as c:
        s = c.get(f"/containers/{cid}/stats", params={"stream": "false"}).json()
    cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
    sys_delta = (cpu.get("system_cpu_usage") or 0) - (pre.get("system_cpu_usage") or 0)
    ncpu = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    mem = s.get("memory_stats") or {}
    usage = mem.get("usage") or 0
    cache = (mem.get("stats") or {}).get("inactive_file", 0)
    out = {"cpu_percent": round(cpu_delta / sys_delta * ncpu * 100, 1) if sys_delta > 0 and cpu_delta >= 0 else 0.0,
           "mem_bytes": max(0, usage - cache), "mem_limit": mem.get("limit") or None}
    _stats_cache[cid] = (time.time(), out)
    return out


def container_stats(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
        results = list(ex.map(lambda i: (i, _safe_stats(i)), ids))
    return dict(results)


def _safe_stats(cid: str) -> dict | None:
    try:
        return _stats_one(cid)
    except (httpx.HTTPError, ValueError):
        return None


def project_container(service: str) -> dict | None:
    return next((c for c in list_containers() if c["service"] == service), None)


def _frames(stream: Iterator[bytes]) -> Iterator[tuple[int, bytes]]:
    """Demultiplex the Docker log stream (8-byte header per frame) for non-TTY containers."""
    buf = b""
    for chunk in stream:
        buf += chunk
        while len(buf) >= 8:
            kind, size = buf[0], struct.unpack(">I", buf[4:8])[0]
            if kind not in (0, 1, 2) or buf[1:4] != b"\x00\x00\x00":
                # TTY container: raw stream without headers.
                yield 1, buf
                buf = b""
                break
            if len(buf) < 8 + size:
                break
            yield kind, buf[8:8 + size]
            buf = buf[8 + size:]


def stream_client() -> httpx.Client:
    return _client(timeout=httpx.Timeout(10.0, read=None))


def follow_logs(container_id: str, tail: int = 200, follow: bool = True, since: int | None = None,
                client: httpx.Client | None = None) -> Iterator[str]:
    """Yield log lines ('<timestamp> <message>') from one container. Closing `client` ends the stream."""
    params = {"stdout": "1", "stderr": "1", "timestamps": "1", "tail": str(max(0, min(tail, 5000))),
              "follow": "1" if follow else "0"}
    if since:
        params["since"] = str(since)
    c = client or stream_client()
    try:
        with c.stream("GET", f"/containers/{container_id}/logs", params=params) as resp:
            resp.raise_for_status()
            partial = ""
            for _, data in _frames(resp.iter_bytes()):
                text = partial + data.decode("utf-8", "replace")
                lines = text.split("\n")
                partial = lines.pop()
                for ln in lines:
                    yield ln.rstrip("\r")
            if partial:
                yield partial
    finally:
        if client is None:
            c.close()


def level_of(line: str) -> str:
    m = LEVEL_RE.search(line[:200])
    if not m:
        return "info"
    v = m.group(1)
    return {"WARNING": "warn", "WARN": "warn", "CRITICAL": "error", "FATAL": "error"}.get(v, v.lower())
