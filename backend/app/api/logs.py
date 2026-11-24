"""Live container logs (admin only, audited) as server-sent events, via the read-only logproxy."""
import json
import queue
import re
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import dockerapi
from app.audit import audit
from app.deps import get_db, require
from app.framework import registry
from app.models import TaskRun, User

router = APIRouter(prefix="/admin/logs", tags=["logs"])
MAX_SERVICES = 12
PING_SECONDS = 15


@router.get("/services")
def services(_: User = Depends(require("admin"))):
    try:
        return [{"service": c["service"], "state": c["state"], "health": c["health"], "id": c["id"]}
                for c in dockerapi.list_containers()]
    except dockerapi.DockerUnavailable as exc:
        raise HTTPException(503, f"log access unavailable: {exc}") from exc


class _Follower(threading.Thread):
    def __init__(self, service: str, cid: str, tail: int, out: queue.Queue, stop: threading.Event):
        super().__init__(daemon=True, name=f"logs-{service}")
        self.service, self.cid, self.tail, self.out, self.stop = service, cid, tail, out, stop
        self.client = dockerapi.stream_client()

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        try:
            for line in dockerapi.follow_logs(self.cid, tail=self.tail, client=self.client):
                if self.stop.is_set():
                    return
                self.out.put((self.service, line))
        except Exception as exc:  # noqa: BLE001
            if not self.stop.is_set():
                self.out.put((self.service, f"[log stream ended: {exc}]"))
        finally:
            self.out.put((self.service, None))


@router.get("/stream")
def stream(services: str = "", tail: int = 200, grep: str = "", run_id: int | None = None,
           db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    wanted = [s.strip() for s in services.split(",") if s.strip()]
    pattern = None
    if run_id is not None:
        run = db.get(TaskRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        defn = registry.load_all().get(run.task_key)
        wanted = wanted or [f"worker-{defn.worker if defn else 'default'}"]
        pattern = re.compile(rf"\brun_id={run_id}\b")
    if not wanted:
        raise HTTPException(400, "choose at least one service")
    try:
        containers = {c["service"]: c for c in dockerapi.list_containers()}
    except dockerapi.DockerUnavailable as exc:
        raise HTTPException(503, f"log access unavailable: {exc}") from exc
    unknown = [s for s in wanted if s not in containers]
    if unknown:
        raise HTTPException(400, f"not a Steward service: {', '.join(unknown)}")
    wanted = wanted[:MAX_SERVICES]
    grep_l = grep.lower()
    audit(db, admin.username, "logs.followed", "service", ",".join(wanted), tail=tail, grep=grep or None,
          run_id=run_id)
    db.commit()
    tail = max(0, min(tail, 2000))

    def events():
        q: queue.Queue = queue.Queue(maxsize=10000)
        stop = threading.Event()
        alive = len(wanted)
        followers = [_Follower(s, containers[s]["full_id"], tail if run_id is None else 2000, q, stop) for s in wanted]
        for f in followers:
            f.start()
        yield "retry: 3000\n\n"
        yield f"event: hello\ndata: {json.dumps({'services': wanted})}\n\n"
        last = time.monotonic()
        try:
            while alive:
                try:
                    service, line = q.get(timeout=1)
                except queue.Empty:
                    if time.monotonic() - last > PING_SECONDS:
                        last = time.monotonic()
                        yield ": ping\n\n"
                    continue
                if line is None:
                    alive -= 1
                    yield f"event: ended\ndata: {json.dumps({'service': service})}\n\n"
                    continue
                if pattern is not None and not pattern.search(line):
                    continue
                if grep_l and grep_l not in line.lower():
                    continue
                ts, _, msg = line.partition(" ")
                last = time.monotonic()
                yield "data: " + json.dumps({"service": service, "ts": ts, "line": msg,
                                             "level": dockerapi.level_of(msg)}) + "\n\n"
        finally:
            stop.set()
            for f in followers:
                f.close()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
