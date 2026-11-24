"""Runs task definitions, stores work items, applies autonomy policy and executes approved items."""
import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import audit
from app.connectors.catalyst_center.http import CCError, CCForbidden
from app.db import session_scope
from app.framework import registry
from app.framework.autonomy import effective_autonomy
from app.framework.base import Proposal, RunContext, TaskDefinition
from app.framework.confidence import combine
from app.llm.base import LLMUnavailable
from app.llm.factory import get_provider
from app.logging_setup import log_context
from app.models import DataSource, TaskConfig, TaskRun, WorkItem
from app.store import get_setting

log = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


class RunAlreadyActive(Exception):
    pass


def ensure_task_configs(db: Session) -> None:
    for defn in registry.load_all().values():
        cfg = db.get(TaskConfig, defn.key)
        if cfg is None:
            db.add(TaskConfig(
                task_key=defn.key, enabled=True, cron=defn.default_cron,
                autonomy_level=min(defn.default_autonomy_level, defn.max_autonomy),
                autonomy_cap=min(defn.default_autonomy_cap, defn.max_autonomy),
                auto_threshold=defn.default_auto_threshold,
                max_auto_per_run=defn.default_max_auto_per_run,
                params=defn.default_params(),
            ))
        else:
            merged = {**defn.default_params(), **(cfg.params or {})}
            if merged != cfg.params:
                cfg.params = merged


def resolve_source(db: Session, defn: TaskDefinition, cfg: TaskConfig) -> DataSource | None:
    if cfg.source_id:
        return db.get(DataSource, cfg.source_id)
    return db.scalars(select(DataSource).where(DataSource.kind == defn.source_kind, DataSource.enabled.is_(True))
                      .order_by(DataSource.id)).first()


def create_run(db: Session, task_key: str, trigger: str, actor: str | None) -> TaskRun:
    registry.get(task_key)
    active = db.scalars(select(TaskRun).where(TaskRun.task_key == task_key,
                                              TaskRun.status.in_(("queued", "running")))).first()
    if active:
        raise RunAlreadyActive(f"run {active.id} is already {active.status}")
    cfg = db.get(TaskConfig, task_key)
    run = TaskRun(task_key=task_key, trigger=trigger, triggered_by=actor, status="queued",
                  source_id=cfg.source_id if cfg else None)
    db.add(run)
    db.flush()
    return run


# ------------------------------------------------------------------ run execution

def _llm_review(ctx: RunContext, defn: TaskDefinition, proposals: list[Proposal],
                previous: dict[str, WorkItem]) -> dict[str, dict]:
    params = ctx.params
    lo, hi = int(params.get("llm_min_confidence", 35)), int(params.get("llm_max_confidence", 90))
    budget = int(params.get("llm_max_items", 150))
    results: dict[str, dict] = {}
    if ctx.llm is None:
        return {p.target_key: {"status": "off"} for p in proposals}
    if not params.get("llm_enabled", True):
        return results

    eligible = [p for p in proposals if p.llm_prompt and lo <= p.rule_confidence <= hi]
    eligible.sort(key=lambda p: -p.rule_confidence)
    called = reused = 0
    unavailable = False
    for p in eligible:
        prev = previous.get(p.target_key)
        if (prev and prev.llm_status == "ok" and p.fingerprint and prev.fingerprint == p.fingerprint
                and (prev.llm_provider or ctx.llm.name) == ctx.llm.name):
            results[p.target_key] = {"status": "ok", "confidence": prev.llm_confidence,
                                     "rationale": prev.rationale, "concerns": prev.concerns, "reused": True}
            reused += 1
            continue
        if unavailable:
            results[p.target_key] = {"status": "unavailable"}
            continue
        if called >= budget:
            continue
        try:
            out = ctx.llm.review(defn.llm_system_prompt, p.llm_prompt)
            results[p.target_key] = {"status": "ok", **out}
            called += 1
        except LLMUnavailable as exc:
            ctx.log(f"{ctx.llm.label} unavailable, continuing with rule-based confidence only: {exc}")
            unavailable = True
            results[p.target_key] = {"status": "unavailable"}
        except Exception as exc:  # noqa: BLE001 - a bad LLM answer must not fail the run
            results[p.target_key] = {"status": "error", "error": str(exc)}
            called += 1
    ctx.log(f"{ctx.llm.label} review: {called} new, {reused} reused, {len(eligible)} eligible "
            f"(band {lo}-{hi}, budget {budget})")
    ctx.llm_stats = {"llm_provider": ctx.llm.name, "llm_reviewed": called, "llm_reused": reused,
                     "llm_eligible": len(eligible), "llm_unavailable": unavailable}
    return results


def execute_run(run_id: int) -> None:
    with session_scope() as db:
        run = db.get(TaskRun, run_id)
        if run is None or run.status not in ("queued",):
            return
        run.status, run.started_at = "running", now()
        task_key = run.task_key
    with log_context(run_id=run_id, task_key=task_key):
        try:
            _execute_run(run_id)
        except Exception as exc:
            log.exception("run %s could not be saved", run_id)
            with session_scope() as db:
                run = db.get(TaskRun, run_id)
                if run is not None and run.status == "running":
                    run.status, run.finished_at = "failed", now()
                    run.error = f"Run results could not be saved: {friendly_error(exc)}"
            raise
    dispatch_approved(task_key)


def friendly_error(exc: BaseException) -> str:
    """One readable line for operators; the traceback only goes to the container log."""
    if isinstance(exc, CCForbidden):
        return str(exc)
    if isinstance(exc, CCError):
        return f"Catalyst Center request failed (HTTP {exc.status}): {exc.message[:160]}"
    if isinstance(exc, httpx.TimeoutException):
        return "Catalyst Center did not answer in time; the next run will try again."
    if isinstance(exc, httpx.TransportError):
        return f"Cannot reach Catalyst Center ({type(exc).__name__})."
    if isinstance(exc, IntegrityError):
        return "Database conflict while saving the results (another task wrote the same data); the next run will try again."
    if isinstance(exc, SQLAlchemyError):
        return "Database error while saving the results; the next run will try again."
    first = (str(exc).strip().splitlines() or [""])[0][:300]
    return first or f"Unexpected error ({type(exc).__name__})."


def display_error(text: str | None) -> str | None:
    """Errors saved by older releases are full tracebacks: show only the final exception line."""
    if not text or "Traceback (most recent call last)" not in text:
        return text
    if "sqlalchemy.exc." in text or "psycopg." in text:
        return "Database error while saving the results; the next run will try again."
    last = next((ln.strip() for ln in reversed(text.splitlines())
                 if ln.strip() and not ln.strip().startswith("(Background on this error")), "")
    return last.split(": ", 1)[-1][:300] or "Unexpected error."


def fail_orphaned_runs(worker: str) -> int:
    """At worker start: runs of this worker's tasks still marked running died with the previous container."""
    keys = [k for k, d in registry.load_all().items() if d.worker == worker]
    if not keys:
        return 0
    with session_scope() as db:
        n = db.execute(update(TaskRun).where(TaskRun.task_key.in_(keys), TaskRun.status == "running")
                       .values(status="failed", finished_at=now(),
                               error=f"run did not finish: worker-{worker} restarted")).rowcount
    if n:
        log.warning("marked %s orphaned run(s) of worker-%s as failed", n, worker)
    return n


def _execute_run(run_id: int) -> None:
    with session_scope() as db:
        run = db.get(TaskRun, run_id)
        defn = registry.get(run.task_key)
        cfg = db.get(TaskConfig, run.task_key)
        source = resolve_source(db, defn, cfg)
        run.source_id = source.id if source else None
        ctx = RunContext(db, defn.key, source, dict(cfg.params or {}), run_id=run.id, llm=get_provider(db))
        try:
            if source is None:
                raise RuntimeError(f"no enabled {defn.source_kind} data source configured")
            result = defn.analyze(ctx)
            stats = _store_results(db, ctx, defn, cfg, run, result)
            run.stats = {**result.stats, **stats, **ctx.llm_stats}
            run.status = "success"
            db.flush()
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s failed", run_id)
            db.rollback()
            run = db.get(TaskRun, run_id)
            run.error = friendly_error(exc)
            ctx.log(f"FAILED: {run.error} (admins: technical details in Expert Task history > Live container log)")
            run.status = "failed"
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                log.exception("closing run context failed")
            run.log = ctx.log_text[-200_000:]
            run.finished_at = now()


def _store_results(db: Session, ctx: RunContext, defn: TaskDefinition, cfg: TaskConfig,
                   run: TaskRun, result) -> dict:
    autonomy = effective_autonomy(db, defn, cfg)
    items = db.scalars(select(WorkItem).where(WorkItem.task_key == defn.key,
                                              WorkItem.source_id == run.source_id if run.source_id
                                              else WorkItem.source_id.is_(None))).all()
    open_by_target = {i.target_key: i for i in items if i.status in ("proposed", "reported")}
    locked_targets = {i.target_key for i in items if i.status in ("approved", "executing")}
    last_rejected: dict[str, WorkItem] = {}
    for i in items:
        if i.status == "rejected":
            cur = last_rejected.get(i.target_key)
            if cur is None or (i.decided_at and cur.decided_at and i.decided_at > cur.decided_at):
                last_rejected[i.target_key] = i

    llm_results = _llm_review(ctx, defn, result.proposals, open_by_target)

    created = updated = suppressed = 0
    touched: list[WorkItem] = []
    for p in result.proposals:
        if p.target_key in locked_targets:
            continue
        if p.fingerprint and len(p.fingerprint) > 64:
            p.fingerprint = hashlib.sha256(p.fingerprint.encode()).hexdigest()
        rej = last_rejected.get(p.target_key)
        if rej and p.fingerprint and rej.fingerprint == p.fingerprint and p.target_key not in open_by_target:
            suppressed += 1
            continue
        item = open_by_target.get(p.target_key)
        if item is None:
            item = WorkItem(task_key=defn.key, source_id=run.source_id, first_run_id=run.id, target_key=p.target_key)
            db.add(item)
            created += 1
        else:
            updated += 1
        llm = llm_results.get(p.target_key)
        item.last_run_id = run.id
        item.target_label = (p.target_label or "")[:255]
        item.group_label = p.group_label[:255] if p.group_label else p.group_label
        item.action_type = p.action_type
        item.payload = p.payload
        item.evidence = p.evidence
        item.rule_confidence = int(p.rule_confidence)
        item.blocked_reason = p.blocked_reason
        item.fingerprint = p.fingerprint
        if llm and llm["status"] == "ok":
            item.llm_status, item.llm_confidence = "ok", int(llm["confidence"])
            item.llm_provider = ctx.llm.name if ctx.llm else None
            item.rationale = llm["rationale"] or p.rationale
            item.concerns = list(dict.fromkeys(p.concerns + list(llm.get("concerns") or [])))
        else:
            item.llm_status = llm["status"] if llm else "skipped"
            item.llm_confidence = None
            item.llm_provider = None
            item.rationale = p.rationale
            item.concerns = p.concerns
        item.confidence = combine(item.rule_confidence, item.llm_confidence)
        item.status = "reported" if autonomy.level == 0 else "proposed"
        touched.append(item)

    superseded = 0
    for target, observed in (result.resolved or {}).items():
        item = open_by_target.get(target)
        if item is not None and item.status in ("proposed", "reported"):
            item.status = "superseded"
            item.outcome = defn.superseded_outcome(item, observed)
            item.decided_at = now()
            item.decided_by = "external"
            superseded += 1
    db.flush()

    auto = 0
    if autonomy.level >= 2 and not autonomy.kill_switch:
        candidates = [i for i in touched if i.status == "proposed" and not i.blocked_reason
                      and (i.payload or {}).get("auto_eligible", True)
                      and (autonomy.level == 3 or i.confidence >= cfg.auto_threshold)]
        candidates.sort(key=lambda i: -i.confidence)
        for i in candidates[: max(0, cfg.max_auto_per_run)]:
            i.status = "approved"
            i.decided_by = "auto"
            i.decided_at = now()
            i.decision_note = (f"auto-approved at L{autonomy.level}"
                               + ("" if autonomy.level == 3 else f", confidence {i.confidence} >= {cfg.auto_threshold}"))
            auto += 1
        if auto:
            audit(db, "system", "items.auto_approved", "task", defn.key, count=auto, run_id=run.id,
                  level=autonomy.level, threshold=cfg.auto_threshold)

    ctx.log(f"Work items: {created} new, {updated} refreshed, {superseded} superseded, "
            f"{suppressed} suppressed (previously rejected, unchanged), {auto} auto-approved "
            f"(effective autonomy L{autonomy.level})")
    return {"items_created": created, "items_updated": updated, "items_superseded": superseded,
            "items_suppressed": suppressed, "items_auto_approved": auto, "effective_autonomy": autonomy.level}


# ------------------------------------------------------------------ decisions and execution

def decide(db: Session, ids: list[int], decision: str, actor: str, note: str | None = None,
           override: dict | None = None) -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    items = db.scalars(select(WorkItem).where(WorkItem.id.in_(ids))).all()
    done, errors = [], []
    ctx_cache: dict[str, RunContext] = {}
    for item in items:
        if item.status != "proposed":
            errors.append({"id": item.id, "error": f"item is {item.status}"})
            continue
        defn = registry.get(item.task_key)
        if decision == "approve":
            if override:
                if item.task_key not in ctx_cache:
                    cfg = db.get(TaskConfig, item.task_key)
                    ctx_cache[item.task_key] = RunContext(db, item.task_key, None, dict(cfg.params or {}))
                ctx = ctx_cache[item.task_key]
                try:
                    item.override_payload = defn.apply_override(ctx, item, override)
                except ValueError as exc:
                    errors.append({"id": item.id, "error": str(exc)})
                    continue
            elif item.blocked_reason:
                errors.append({"id": item.id, "error": item.blocked_reason})
                continue
            item.status = "approved"
        else:
            item.status = "rejected"
        item.decided_by, item.decided_at, item.decision_note = actor, now(), note
        done.append(item.id)
    if done:
        audit(db, actor, f"items.{decision}d", "work_item", None, ids=done, note=note,
              override=override, count=len(done))
    return {"updated": done, "errors": errors}


def dispatch_approved(task_key: str | None = None) -> int:
    """Move approved items to executing and enqueue them. Honours kill switch and autonomy caps."""
    from app.workers import enqueue_execute

    batches: dict[str, list[int]] = defaultdict(list)
    with session_scope() as db:
        if get_setting(db, "kill_switch"):
            return 0
        q = select(WorkItem).where(WorkItem.status == "approved")
        if task_key:
            q = q.where(WorkItem.task_key == task_key)
        items = db.scalars(q.with_for_update(skip_locked=True)).all()
        revoked = 0
        for item in items:
            defn = registry.get(item.task_key)
            cfg = db.get(TaskConfig, item.task_key)
            if item.decided_by == "auto" and effective_autonomy(db, defn, cfg).level < 2:
                item.status, item.decided_by, item.decided_at = "proposed", None, None
                item.decision_note = "auto-approval revoked: autonomy level or cap lowered"
                revoked += 1
                continue
            item.status = "executing"
            batches[item.task_key].append(item.id)
        if revoked:
            audit(db, "system", "items.auto_approval_revoked", "work_item", None, count=revoked)
    for key, ids in batches.items():
        enqueue_execute(key, ids)
    return sum(len(v) for v in batches.values())


def execute_items(task_key: str, ids: list[int]) -> None:
    with log_context(task_key=task_key, execute=len(ids)):
        _execute_items(task_key, ids)


def _execute_items(task_key: str, ids: list[int]) -> None:
    with session_scope() as db:
        defn = registry.get(task_key)
        cfg = db.get(TaskConfig, task_key)
        items = db.scalars(select(WorkItem).where(WorkItem.id.in_(ids), WorkItem.status == "executing")).all()
        if not items:
            return
        by_source: dict[int | None, list[WorkItem]] = defaultdict(list)
        for item in items:
            by_source[item.source_id].append(item)
        results, logs = {}, []
        for source_id, group in by_source.items():
            source = db.get(DataSource, source_id) if source_id else resolve_source(db, defn, cfg)
            ctx = RunContext(db, task_key, source, dict(cfg.params or {}))
            try:
                results.update(defn.execute(ctx, group))
            except Exception as exc:  # noqa: BLE001
                log.exception("execution failed for %s", task_key)
                for item in group:
                    item.status, item.exec_result = "failed", {"error": friendly_error(exc)}
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    log.exception("closing execution context failed")
            logs.append(ctx.log_text)
        ok = failed = 0
        for item in items:
            res = results.get(item.id)
            if res is None:
                if item.status == "executing":
                    item.status, item.exec_result = "failed", {"error": "no result returned"}
                failed += item.status == "failed"
                continue
            item.status = "done" if res.success else "failed"
            item.exec_result = {"detail": res.detail, **res.data}
            item.verify_status = None if res.verified is None else ("ok" if res.verified else "failed")
            item.executed_at = now()
            ok += res.success
            failed += not res.success
        audit(db, "system", "items.executed", "task", task_key, ids=ids, succeeded=ok, failed=failed,
              log="\n".join(logs)[-4000:])


def revoke_open_auto_items(db: Session) -> None:
    db.execute(update(WorkItem).where(WorkItem.status == "approved", WorkItem.decided_by == "auto")
               .values(status="proposed", decided_by=None, decided_at=None,
                       decision_note="auto-approval revoked: kill switch or cap change"))
