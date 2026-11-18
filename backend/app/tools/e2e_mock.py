"""End-to-end acceptance test against the simulated Catalyst Centers (install with --with-mock).

Drives the public portal API through the web proxy exactly like the UI does, compares every task's work
items with the simulator's ground truth and executes small approved batches through the Catalyst Center API.
The first simulator runs without GeoIP, the second with the simulator's GeoIP database uploaded, so both the
GeoIP-less and the GeoIP-assisted paths are exercised.

    python -m app.tools.e2e_mock --password <admin password> [--llm-items 10] [--approve 40]
"""
import argparse
import json
import sys
import time
from collections import Counter

import httpx

TASK = "ap_site_assignment"
MOCKS = {"Mock Catalyst Center 2.3.5": "http://mockcc-23:8080", "Mock Catalyst Center 3.1.3": "http://mockcc-31:8080"}
GEOIP_FOR = "Mock Catalyst Center 3.1.3"
GEOIP_NAME = "Mock GeoIP"
BANDS = [(95, 101), (90, 95), (80, 90), (60, 80), (0, 60)]
WORKERS = {"default", "wireless", "inventory", "health"}


class Portal:
    def __init__(self, base: str, username: str, password: str):
        self.c = httpx.Client(base_url=base, verify=False, timeout=120, headers={"X-AIOps": "1"})
        r = self.c.post("/api/auth/login", json={"username": username, "password": password})
        r.raise_for_status()

    def get(self, path, **params):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def send(self, method, path, body=None, expect=(200,), **kw):
        r = self.c.request(method, path, json=body, **kw)
        if r.status_code not in expect:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r


class Check:
    def __init__(self):
        self.good = True

    def ok(self, msg):
        print(f"[ OK ] {msg}", flush=True)

    def fail(self, msg):
        print(f"[FAIL] {msg}", flush=True)
        self.good = False

    def expect(self, cond, good_msg, bad_msg):
        self.ok(good_msg) if cond else self.fail(bad_msg)
        return cond


def info(msg):
    print(f"       {msg}", flush=True)


def wait_run(p: Portal, run_id: int, timeout: int) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = p.get(f"/api/runs/{run_id}")
        if run["status"] in ("success", "failed"):
            return run
        time.sleep(3)
    raise TimeoutError(f"run {run_id} did not finish in {timeout}s")


def run_task(p: Portal, c: Check, key: str, timeout: int) -> dict | None:
    t0 = time.time()
    for _ in range(60):
        r = p.send("POST", f"/api/tasks/{key}/run", expect=(200, 409))
        if r.status_code == 200:
            break
        time.sleep(5)
    run = wait_run(p, r.json()["id"], timeout)
    if run["status"] != "success":
        c.fail(f"{key}: run {run['id']} failed: {run.get('error')}")
        print("\n".join(run.get("log") or []))
        return None
    run["elapsed"] = time.time() - t0
    return run


def configure(p: Portal, key: str, source_id: int, level: int = 1, **params):
    task = p.get(f"/api/tasks/{key}")
    cap = min(task["max_autonomy"], max(level, 1))
    p.send("PUT", f"/api/tasks/{key}", {"enabled": True, "source_id": source_id, "autonomy_cap": cap,
                                        "autonomy_level": min(level, cap), "params": params})


def items(p: Portal, key: str, source_id: int, status: str = "proposed,reported", full: bool = False) -> list[dict]:
    out, offset = [], 0
    while True:
        page = p.get("/api/items", task_key=key, status=status, limit=500, offset=offset)
        out += [i for i in page["items"] if i["source_id"] == source_id]
        offset += 500
        if offset >= page["total"]:
            break
    if full:
        out = [p.get(f"/api/items/{i['id']}") for i in out]
    return out


def approve_and_wait(p: Portal, ids: list[int], timeout: int) -> dict[int, dict]:
    if not ids:
        return {}
    p.send("POST", "/api/items/decide", {"decision": "approve", "ids": ids, "note": "acceptance test"})
    pending, results, t0 = set(ids), {}, time.time()
    while pending and time.time() - t0 < timeout:
        time.sleep(3)
        for iid in list(pending):
            it = p.get(f"/api/items/{iid}")
            if it["status"] in ("done", "failed"):
                results[iid] = it
                pending.discard(iid)
    return results


def verified(results: dict[int, dict], n: int) -> bool:
    return len(results) == n and all(r["status"] == "done" and r["verify_status"] == "ok" for r in results.values())


def show_failures(results: dict[int, dict]):
    for it in list(results.values())[:5]:
        if it["status"] != "done" or it["verify_status"] != "ok":
            info(f"{it['target_label']}: {it['status']} {(it.get('exec_result') or {}).get('detail')}")


def mock_truth(url: str) -> dict:
    return httpx.get(f"{url}/mock/truth", timeout=60).json()


def collect(p: Portal, c: Check, src: dict, timeout: int, times: int = 1):
    configure(p, "cc_inventory_snapshot", src["id"], level=0)
    for _ in range(times):
        if run_task(p, c, "cc_inventory_snapshot", timeout) is None:
            return False
    return True


# ----------------------------------------------------------------------------------------------- platform


def check_platform(p: Portal, c: Check):
    print("\n=== Platform: runners and logs ===", flush=True)
    r = p.get("/api/runners")
    names = {w["name"] for w in r["workers"]}
    alive = {w["name"] for w in r["workers"] if w["alive"]}
    c.expect(WORKERS <= alive, f"specialist workers online: {', '.join(sorted(alive))}",
             f"workers missing or down: {sorted(WORKERS - alive)} (known: {sorted(names)})")
    c.expect(r["scheduler_alive"], "scheduler heartbeat present", "scheduler heartbeat missing")
    c.expect(not r.get("docker_error"), "container metrics available through logproxy",
             f"container metrics unavailable: {r.get('docker_error')}")
    for w in r["workers"]:
        ct = w.get("container") or {}
        info(f"worker-{w['name']:10s} queue {w['queue']:14s} depth {w['queue_depth']}  tasks {len(w['tasks'])}  "
             f"state {ct.get('state')}/{ct.get('health')}  mem {ct.get('mem_limit')}")
    svcs = {s["service"] for s in p.get("/api/admin/logs/services")}
    c.expect({"api", "web", "postgres", "redis"} <= svcs and {f"worker-{w}" for w in WORKERS} <= svcs,
             f"log follow lists {len(svcs)} CiscoAIOps services",
             f"log follow services incomplete: {sorted(svcs)}")
    c.expect("midserver" not in " ".join(svcs) and "zabbix" not in " ".join(svcs),
             "log follow limited to the CiscoAIOps project", "log follow exposes foreign containers")


def follow_run_log(p: Portal, c: Check, run: dict, worker: str, seconds: int = 8):
    lines = []
    try:
        with p.c.stream("GET", "/api/admin/logs/stream", params={"run_id": run["id"]},
                        timeout=httpx.Timeout(seconds, read=seconds)) as r:
            if r.status_code != 200:
                c.fail(f"run-scoped log follow -> {r.status_code}")
                return
            t0, event = time.time(), "message"
            for raw in r.iter_lines():
                if raw.startswith("event: "):
                    event = raw[7:]
                elif raw.startswith("data: ") and event == "message":
                    lines.append(json.loads(raw[6:]))
                elif not raw:
                    event = "message"
                if len(lines) >= 3 or time.time() - t0 > seconds:
                    break
    except httpx.ReadTimeout:
        pass
    good = lines and all(l["service"] == f"worker-{worker}" and f"run_id={run['id']}" in l["line"] for l in lines)
    c.expect(good, f"run {run['id']} ({run['task_key']}) logged by worker-{worker} (live log follow, "
                   f"{len(lines)} lines)", f"run-scoped log follow for run {run['id']} returned {lines[:2]}")


# ------------------------------------------------------------------------------------------------ geoip


def geoip_source(p: Portal) -> dict:
    for s in p.get("/api/sources"):
        if s["kind"] == "geoip":
            return s
    return p.send("POST", "/api/sources", {"name": GEOIP_NAME, "kind": "geoip", "enabled": False}).json()


def set_geoip(p: Portal, c: Check, geo: dict, mock_url: str | None):
    p.send("PUT", f"/api/sources/{geo['id']}", {"name": geo["name"], "kind": "geoip", "enabled": bool(mock_url)})
    if not mock_url:
        info("GeoIP source disabled: this simulator is analysed without GeoIP")
        return
    data = httpx.get(f"{mock_url}/mock/geoip.mmdb", timeout=60).content
    f = p.send("POST", f"/api/sources/{geo['id']}/upload", content=data,
               headers={"Content-Type": "application/octet-stream"},
               params={"filename": f"mock-{mock_url.rsplit('-', 1)[-1].split(':')[0]}.mmdb", "activate": "true"}).json()
    state = p.get(f"/api/sources/{geo['id']}/geoip")
    active = [x for x in state["files"] if x["active"]]
    c.expect(active and active[0]["id"] == f["id"],
             f"GeoIP database uploaded and active ({f['size']} bytes, {len(state['files'])} versions kept)",
             "GeoIP upload did not become the active version")


# ------------------------------------------------------------------------------------------------ tasks


def check_ap_site(p: Portal, c: Check, src: dict, url: str, args, geo: bool):
    configure(p, TASK, src["id"], level=1, llm_max_items=args.llm_items)
    r = p.send("PUT", f"/api/tasks/{TASK}", {"autonomy_level": 2}, expect=(400, 200))
    c.expect(r.status_code == 400, "autonomy cap enforced: level 2 rejected while the task cap is 1",
             "autonomy level above the cap was accepted")
    run = run_task(p, c, TASK, args.timeout)
    if run is None:
        return
    st = run["stats"] or {}
    c.ok(f"ap_site_assignment run {run['id']} in {run['elapsed']:.0f}s: {st.get('aps_total')} APs, "
         f"{st.get('aps_unassigned')} without site, {st.get('cc_api_calls')} CC API calls, "
         f"LLM reviewed {st.get('llm_reviewed', 0)} of {st.get('llm_eligible', 0)} eligible")
    truth = mock_truth(url)
    its = [i for i in items(p, TASK, src["id"], full=True) if i["target_key"] in truth["truth"]]
    bands = {b: Counter() for b in BANDS}
    blocked = with_geo = 0
    for i in its:
        if any(e.get("signal") == "geoip" and e.get("buildings") for e in i.get("evidence") or []):
            with_geo += 1
        if i["blocked_reason"]:
            blocked += 1
            continue
        correct = i["payload"].get("site_id") == truth["truth"][i["target_key"]]
        for lo, hi in BANDS:
            if lo <= i["confidence"] < hi:
                bands[(lo, hi)]["ok" if correct else "wrong"] += 1
    info(f"proposals: {len(its)} (blocked for operator choice: {blocked}, with a GeoIP vote: {with_geo})")
    for (lo, hi), cnt in bands.items():
        n = cnt["ok"] + cnt["wrong"]
        if n:
            info(f"confidence {lo:3d}-{min(hi, 100):3d}: {n:5d} items, accuracy {100 * cnt['ok'] / n:5.1f}%")
    top = bands[(95, 101)]
    c.expect(not (top["ok"] + top["wrong"]) or top["wrong"] / (top["ok"] + top["wrong"]) <= 0.02,
             "high-confidence proposals match ground truth", "more than 2% wrong at confidence >= 95")
    if geo:
        c.expect(with_geo > 0, "GeoIP contributes votes when configured", "no GeoIP votes despite an active database")
    else:
        c.expect(with_geo == 0, "no GeoIP votes without a GeoIP source", "GeoIP votes without a GeoIP source")
    info(f"LLM review status: {dict(Counter(i['llm_status'] for i in its))}")

    approve = [i for i in sorted(its, key=lambda x: -x["confidence"]) if not i["blocked_reason"]][: args.approve]
    res = approve_and_wait(p, [i["id"] for i in approve], args.timeout)
    still = set(mock_truth(url)["unassigned"]) & {i["target_key"] for i in approve}
    if verified(res, len(approve)) and not still:
        c.ok(f"executed {len(approve)} approved assignments via Catalyst Center API and verified membership")
    else:
        c.fail(f"ap_site execution: {sum(r['verify_status'] == 'ok' for r in res.values())}/{len(approve)} verified, "
               f"{len(still)} still unassigned")
        show_failures(res)


def check_wrong_site(p: Portal, c: Check, src: dict, url: str, args):
    configure(p, "site_reassignment", src["id"], level=1)
    run = run_task(p, c, "site_reassignment", args.timeout)
    if run is None:
        return
    truth = mock_truth(url)
    wrong, devs = truth["wrong_site"], set(truth["hostnames"])
    its = [i for i in items(p, "site_reassignment", src["id"], full=True) if i["target_key"] in devs]
    hits = [i for i in its if i["target_key"] in wrong]
    recall, precision = len(hits) / max(1, len(wrong)), len(hits) / max(1, len(its))
    info(f"wrong-site: {len(wrong)} misplaced devices in CC, {len(its)} proposals, recall {recall:.0%}, "
         f"precision {precision:.0%} (GeoIP {'on' if run['stats'].get('signals_available', {}).get('geoip') else 'off'})")
    c.expect(recall >= 0.8 and precision >= 0.9, "wrong-site detection meets recall >= 80% and precision >= 90%",
             "wrong-site detection below target")
    unblocked = [i for i in hits if not i["blocked_reason"]]
    exact = sum(i["payload"]["site_id"] == truth["truth"][i["target_key"]] for i in unblocked)
    c.expect(exact >= 0.95 * len(unblocked), f"{exact}/{len(unblocked)} proposed sites are the true site",
             f"only {exact}/{len(unblocked)} proposed sites are correct")
    sample = sorted(unblocked, key=lambda x: -x["confidence"])[:5]
    res = approve_and_wait(p, [i["id"] for i in sample], args.timeout)
    after = mock_truth(url)["wrong_site"]
    c.expect(verified(res, len(sample)) and not set(after) & {i["target_key"] for i in sample},
             f"reassigned {len(sample)} devices via Catalyst Center and verified", "wrong-site execution failed")
    show_failures(res)


def check_hostname(p: Portal, c: Check, src: dict, url: str, args):
    configure(p, "ap_hostname_reset", src["id"], level=1)
    if not collect(p, c, src, args.timeout):
        return
    resets = httpx.post(f"{url}/mock/reset_names", timeout=60).json()
    if not collect(p, c, src, args.timeout):
        return
    if run_task(p, c, "ap_hostname_reset", args.timeout) is None:
        return
    truth = mock_truth(url)
    its = {i["target_key"]: i for i in items(p, "ap_hostname_reset", src["id"], full=True)}
    hist = [its[a] for a in resets if a in its and its[a]["payload"]["method"] == "history"]
    still_reset = [a for a in resets if truth["hostnames"][a] != truth["name_truth"][a]]
    exact = sum(i["payload"]["hostname_new"] == truth["name_truth"][i["target_key"]] for i in hist)
    info(f"hostname reset: {len(still_reset)} APs with factory names, {len(its)} proposals, "
         f"{len(hist)} restored from inventory history")
    c.expect(len(hist) >= len(still_reset) and exact == len(hist),
             "every reset AP gets its previous name back from inventory history",
             f"history proposals {len(hist)} ({exact} exact) for {len(still_reset)} reset APs")
    sample = hist[:5]
    res = approve_and_wait(p, [i["id"] for i in sample], args.timeout)
    names = mock_truth(url)["hostnames"]
    c.expect(verified(res, len(sample)) and all(names[i["target_key"]] == i["payload"]["hostname_new"] for i in sample),
             f"renamed {len(sample)} APs via Catalyst Center and verified", "AP rename failed")
    show_failures(res)


def check_hierarchy(p: Portal, c: Check, src: dict, url: str, args):
    configure(p, "site_hierarchy_drift", src["id"], level=0)
    httpx.delete(f"{url}/mock/drift", timeout=60)
    if not collect(p, c, src, args.timeout):
        return
    b = p.send("POST", f"/api/inventory/{src['id']}/baseline").json()
    info(f"baseline approved: {b['sites']} sites")
    if run_task(p, c, "site_hierarchy_drift", args.timeout) is None:
        return
    pre = {i["target_key"] for i in items(p, "site_hierarchy_drift", src["id"])}
    c.expect(not [k for k in pre if k.split(":")[0] in ("added", "removed", "renamed", "moved")],
             "no drift reported right after approving the baseline", f"unexpected drift: {sorted(pre)[:5]}")
    changed = httpx.post(f"{url}/mock/drift", timeout=60).json()
    if not collect(p, c, src, args.timeout):
        return
    if run_task(p, c, "site_hierarchy_drift", args.timeout) is None:
        return
    found = {i["target_key"] for i in items(p, "site_hierarchy_drift", src["id"])}
    want = {f"{k}:{sid}" for k, ids in changed.items() for sid in ids}
    info(f"drift findings: {sorted(k.split(':')[0] for k in found)}")
    c.expect(want <= found, "drift report finds the renamed, moved, added and removed sites",
             f"missing drift findings: {sorted(want - found)}")
    reported = items(p, "site_hierarchy_drift", src["id"], status="reported")
    c.expect(len(reported) == len(found), "drift findings are report-only (L0)", "drift findings are approvable")
    httpx.delete(f"{url}/mock/drift", timeout=60)


def check_expected_down(p: Portal, c: Check, src: dict, url: str, args):
    configure(p, "expected_down_devices", src["id"], level=1)
    if not collect(p, c, src, args.timeout, times=5):
        return
    if run_task(p, c, "expected_down_devices", args.timeout) is None:
        return
    t = mock_truth(url)
    its = {i["target_key"]: i for i in items(p, "expected_down_devices", src["id"]) if i["action_type"] == "exclude_device"}
    excluded = set(t["excluded"])
    down, flap = set(t["expected_down"]) - excluded, set(t["flapping"]) - excluded
    info(f"expected-down: {len(down)} long-down, {len(flap)} flapping, {len(t['transient_down'])} briefly down, "
         f"{len(its)} exclusion proposals")
    c.expect(down <= set(its), "all long-down devices proposed for exclusion", f"missed {len(down - set(its))} long-down")
    c.expect(len(flap & set(its)) >= 0.9 * len(flap), "flapping devices detected (>= 90%)", "flapping detection below 90%")
    c.expect(not set(t["transient_down"]) & set(its), "briefly-down devices left alone", "briefly-down devices proposed")
    sample = [its[d] for d in sorted(down) if d in its][:3]
    res = approve_and_wait(p, [i["id"] for i in sample], args.timeout)
    mech = {(r.get("exec_result") or {}).get("mechanism") for r in res.values()}
    c.expect(verified(res, len(sample)) and {i["target_key"] for i in sample} <= set(mock_truth(url)["excluded"]),
             f"excluded {len(sample)} devices via {', '.join(sorted(m for m in mech if m))} and verified",
             "device exclusion failed")
    show_failures(res)


def check_telemetry(p: Portal, c: Check, src: dict, url: str, args) -> dict | None:
    configure(p, "telemetry_baseline", src["id"], level=1)
    run = run_task(p, c, "telemetry_baseline", args.timeout)
    if run is None:
        return None
    t = mock_truth(url)["telemetry_truth"]
    its = {i["target_key"]: i for i in items(p, "telemetry_baseline", src["id"], full=True)}
    exact = all(its[d]["payload"].get("class") == cls for d, cls in t.items() if d in its)
    info(f"telemetry: {dict(Counter(t.values()))} in CC, {len(its)} items")
    c.expect(set(its) == set(t) and exact, "telemetry problems classified exactly", f"telemetry mismatch "
             f"({len(set(its) - set(t))} extra, {len(set(t) - set(its))} missed)")
    c.expect(all(i["blocked_reason"] for i in its.values() if i["action_type"] == "report"),
             "credential/SNMP findings are report-only", "report items are approvable")
    sample = [i for i in its.values() if i["action_type"] == "resync_device"][:5]
    res = approve_and_wait(p, [i["id"] for i in sample], args.timeout)
    c.expect(verified(res, len(sample)), f"resynced {len(sample)} devices via Catalyst Center and verified",
             "resync failed")
    show_failures(res)
    return run


def check_roles(p: Portal, c: Check, src: dict, url: str, args) -> dict | None:
    configure(p, "device_role_correction", src["id"], level=1)
    run = run_task(p, c, "device_role_correction", args.timeout)
    if run is None:
        return None
    t = mock_truth(url)
    wrong = {d: r for d, r in t["role_truth"].items() if t["roles"][d] != r}
    its = {i["target_key"]: i for i in items(p, "device_role_correction", src["id"], full=True)}
    exact = all(its[d]["payload"]["role"] == r for d, r in wrong.items() if d in its)
    info(f"roles: {len(wrong)} mis-tagged switches, {len(its)} proposals")
    c.expect(set(its) == set(wrong) and exact, "role corrections match the topology ground truth",
             f"role mismatch ({len(set(its) - set(wrong))} extra, {len(set(wrong) - set(its))} missed)")
    sample = list(its.values())[:2]
    res = approve_and_wait(p, [i["id"] for i in sample], args.timeout)
    roles = mock_truth(url)["roles"]
    c.expect(verified(res, len(sample)) and all(roles[i["target_key"]] == i["payload"]["role"] for i in sample),
             f"set the role on {len(sample)} devices via Catalyst Center and verified", "role change failed")
    show_failures(res)
    return run


def check_source(p: Portal, c: Check, src: dict, geo: dict, args):
    name, url = src["name"], MOCKS[src["name"]]
    print(f"\n=== {name} ===", flush=True)
    body = {"name": name, "kind": "catalyst_center", "base_url": src["base_url"], "username": src["username"],
            "verify_tls": False, "rate_limit_rps": src["rate_limit_rps"]}
    p.send("PUT", f"/api/sources/{src['id']}", {**body, "enabled": True})
    try:
        test = p.send("POST", f"/api/sources/{src['id']}/test").json()
        if test["status"] != "ok":
            c.fail(f"source test: {test['status']}")
            return
        c.ok(f"connected: release {test.get('version')} (detected via {test.get('version_source')})")
        for row in test.get("capabilities") or []:
            info(f"{row['operation']:28s} -> {row['selected'] or '-'}")
        use_geo = name == GEOIP_FOR
        set_geoip(p, c, geo, url if use_geo else None)
        if not collect(p, c, src, args.timeout):
            return
        check_ap_site(p, c, src, url, args, use_geo)
        check_wrong_site(p, c, src, url, args)
        check_hostname(p, c, src, url, args)
        check_hierarchy(p, c, src, url, args)
        check_expected_down(p, c, src, url, args)
        tel = check_telemetry(p, c, src, url, args)
        roles = check_roles(p, c, src, url, args)
        for run, worker in ((tel, "health"), (roles, "inventory")):
            if run:
                follow_run_log(p, c, run, worker)
    finally:
        p.send("PUT", f"/api/sources/{src['id']}", {**body, "enabled": False})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://web")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--llm-items", type=int, default=10, help="LLM reviews per run (CPU inference is slow)")
    ap.add_argument("--approve", type=int, default=40, help="high-confidence AP site items to approve and execute")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    p = Portal(args.url, args.username, args.password)
    c = Check()
    c.ok(f"logged in to {args.url} as {args.username}")
    check_platform(p, c)
    sources = sorted((s for s in p.get("/api/sources") if s["name"] in MOCKS), key=lambda s: s["name"])
    if len(sources) != len(MOCKS):
        c.fail("mock sources not found; install with --with-mock")
        return 1
    geo = geoip_source(p)
    try:
        for s in sources:
            check_source(p, c, s, geo, args)
    finally:
        p.send("PUT", f"/api/sources/{geo['id']}", {"name": geo["name"], "kind": "geoip", "enabled": False})
    print()
    if c.good:
        c.ok("acceptance test passed")
        return 0
    c.fail("acceptance test failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
