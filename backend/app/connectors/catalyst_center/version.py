import re

from app.connectors.catalyst_center.http import CCError, CCHttp

Version = tuple[int, ...]


def parse_version(text: str | None) -> Version | None:
    if not text:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text))
    if not m:
        return None
    return tuple(int(p) for p in m.groups() if p is not None)


def format_version(v: Version | None) -> str:
    return ".".join(str(p) for p in v) if v else "unknown"


def detect_release(http: CCHttp) -> tuple[Version | None, str]:
    """Return (parsed version, how it was detected)."""
    try:
        body = http.get("/dna/intent/api/v1/dnac-release")
        resp = body.get("response", body) if isinstance(body, dict) else {}
        for key in ("installedVersion", "displayVersion", "corePlatformVersion", "systemVersion"):
            v = parse_version(resp.get(key)) if isinstance(resp, dict) else None
            if v:
                return v, f"dnac-release.{key}"
    except CCError:
        pass

    try:
        body = http.get("/dna/intent/api/v1/dnac-packages")
        pkgs = body.get("response", []) if isinstance(body, dict) else []
        preferred = [p for p in pkgs if str(p.get("name", "")).lower() in ("dnac", "system", "catalyst-center")]
        for p in preferred or pkgs:
            v = parse_version(p.get("version"))
            if v:
                return v, f"dnac-packages.{p.get('name')}"
    except CCError:
        pass

    return None, "undetected"
