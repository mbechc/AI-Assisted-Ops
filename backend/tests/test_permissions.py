"""Operators review and decide; everything under Configure, the audit log and container logs is admin-only."""
import pytest

from tests.test_ai_modes import _client, update_dir  # noqa: F401

ADMIN_ONLY = [
    ("get", "/api/audit", None),
    ("post", "/api/tasks/ap_site_assignment/run", None),
    ("post", "/api/tasks/ap_site_assignment/insights/ai", None),
    ("post", "/api/digest/owner-reports/ai", {"cls": "credential"}),
    ("post", "/api/sources/1/test", None),
    ("put", "/api/sources/1/geoip/mappings", {"location_key": "x", "site_id": "y"}),
    ("delete", "/api/sources/1/geoip/mappings/1", None),
    ("put", "/api/tasks/ap_site_assignment", {"enabled": True}),
    ("put", "/api/settings", {"kill_switch": False}),
    ("put", "/api/settings/ai", {"mode": "none"}),
    ("get", "/api/users", None),
]


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_admin_only_endpoints(update_dir, role):
    c, db = _client(role)
    for method, path, body in ADMIN_ONLY:
        r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (role, method, path, r.status_code)
    db.close()


def test_operator_can_decide_and_summarise(update_dir):
    c, db = _client("operator")
    assert c.post("/api/items/decide", json={"decision": "approve", "ids": [999999]}).status_code != 403
    assert c.post("/api/digest/shift/ai", json={"hours": 12}).status_code == 400, "allowed, but AI is off"
    db.close()
