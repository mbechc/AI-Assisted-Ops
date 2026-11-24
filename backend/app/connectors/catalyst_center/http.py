"""Low-level Catalyst Center HTTP transport shared by all releases."""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import httpx

log = logging.getLogger(__name__)

Recorder = Callable[[str, str, dict | None, Any, int, Any], None]

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CCError(Exception):
    def __init__(self, status: int, message: str, path: str = ""):
        super().__init__(f"HTTP {status} {path}: {message}")
        self.status = status
        self.path = path
        self.message = message


class CCNotSupported(CCError):
    """The endpoint does not exist on this release (404/405/501)."""


class CCForbidden(CCError):
    """Catalyst Center refused a change: the account may read but not write (for example OBSERVER-ROLE)."""

    def __str__(self) -> str:
        return f"Catalyst Center refused the change: the account has no write permission (HTTP {self.status})"


class CCHttp:
    def __init__(self, base_url: str, username: str, password: str, verify: bool | str = False,
                 rate_limit_rps: float = 5.0, timeout: float = 60.0, recorder: Recorder | None = None):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._lock = threading.Lock()
        self._min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self._last_call = 0.0
        self.recorder = recorder
        self.calls = 0
        self.write_ok = False
        self.write_refused_at: str | None = None
        self._client = httpx.Client(base_url=self.base_url, verify=verify, timeout=timeout,
                                    headers={"Accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def authenticate(self) -> None:
        self._throttle()
        resp = self._client.post("/dna/system/api/v1/auth/token", auth=(self._username, self._password),
                                 headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            raise CCError(resp.status_code, "authentication failed", "/dna/system/api/v1/auth/token")
        self._token = resp.json().get("Token")
        if not self._token:
            raise CCError(resp.status_code, "no token in auth response", "/dna/system/api/v1/auth/token")

    def request(self, method: str, path: str, params: dict | None = None, json: Any = None,
                headers: dict | None = None, retries: int = 4) -> Any:
        if self._token is None:
            self.authenticate()
        attempt = 0
        reauthed = False
        while True:
            self._throttle()
            self.calls += 1
            hdrs = {"X-Auth-Token": self._token or "", **(headers or {})}
            if json is not None:
                hdrs["Content-Type"] = "application/json"
            try:
                resp = self._client.request(method, path, params=params, json=json, headers=hdrs)
            except httpx.TransportError as exc:
                if attempt < retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise CCError(0, f"transport error: {exc}", path) from exc

            if resp.status_code == 401 and not reauthed:
                reauthed = True
                self.authenticate()
                continue
            if resp.status_code == 429 or resp.status_code >= 500 and resp.status_code not in (501,):
                if attempt < retries:
                    attempt += 1
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 60)
                    log.warning("Catalyst Center %s %s -> %s, retry in %.0fs", method, path, resp.status_code, delay)
                    time.sleep(delay)
                    continue

            body: Any
            try:
                body = resp.json() if resp.content else None
            except ValueError:
                body = resp.text

            if self.recorder:
                self.recorder(method, path, params, json, resp.status_code, body)

            write = method.upper() in WRITE_METHODS
            if write and resp.status_code in (401, 403):
                self.write_refused_at = datetime.now(timezone.utc).isoformat()
                raise CCForbidden(resp.status_code, _error_text(body), path)
            if resp.status_code in (404, 405, 501):
                raise CCNotSupported(resp.status_code, _error_text(body), path)
            if resp.status_code >= 400:
                raise CCError(resp.status_code, _error_text(body), path)
            self.write_ok = self.write_ok or write
            return body

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params=params)

    def paged(self, path: str, params: dict | None = None, limit: int = 500, offset_base: int = 1,
              items_key: str = "response", max_pages: int = 10_000) -> Iterator[dict]:
        """Iterate offset/limit paged collections. Catalyst Center offsets are 1-based."""
        offset = offset_base
        for _ in range(max_pages):
            body = self.get(path, {**(params or {}), "offset": offset, "limit": limit})
            items = body.get(items_key, []) if isinstance(body, dict) else (body or [])
            if not isinstance(items, list):
                items = [items] if items else []
            yield from items
            if len(items) < limit:
                return
            offset += limit


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error", "errorMessage"):
            if body.get(key):
                return str(body[key])
        resp = body.get("response")
        if isinstance(resp, dict):
            for key in ("message", "detail", "errorCode"):
                if resp.get(key):
                    return str(resp[key])
    return str(body)[:500] if body else ""
