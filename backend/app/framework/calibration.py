"""Per-task confidence calibration and autonomy promotion recommendation.

Only human decisions and externally observed outcomes count towards accuracy;
auto-approved items would make the statistic circular, so they are only
reported (with their verification result).
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.framework import registry
from app.framework.autonomy import effective_autonomy
from app.models import TaskConfig, WorkItem

TARGET_ACCURACY = 0.98
MIN_SAMPLES = 30


def _bucket(conf: int) -> int:
    return min(9, max(0, conf // 10))


def calibration(db: Session, task_key: str) -> dict:
    defn = registry.get(task_key)
    cfg = db.get(TaskConfig, task_key)
    buckets = [{"range": f"{b*10}-{b*10+9 if b < 9 else 100}", "lower": b * 10, "approved": 0,
                "overridden": 0, "rejected": 0, "external_match": 0, "external_mismatch": 0,
                "auto": 0, "verify_failed": 0, "open": 0} for b in range(10)]
    rows = db.execute(select(WorkItem.confidence, WorkItem.status, WorkItem.decided_by,
                             WorkItem.override_payload, WorkItem.outcome, WorkItem.verify_status)
                      .where(WorkItem.task_key == task_key)).all()
    for conf, status, decided_by, override, outcome, verify in rows:
        b = buckets[_bucket(conf or 0)]
        if status in ("proposed", "reported"):
            b["open"] += 1
            continue
        if verify == "failed":
            b["verify_failed"] += 1
        if decided_by == "auto":
            b["auto"] += 1
            continue
        if status == "rejected":
            b["rejected"] += 1
        elif status == "superseded":
            matched = (outcome or {}).get("matched")
            if matched is True:
                b["external_match"] += 1
            elif matched is False:
                b["external_mismatch"] += 1
        elif status in ("approved", "executing", "done", "failed"):
            if override:
                b["overridden"] += 1
            else:
                b["approved"] += 1

    for b in buckets:
        good = b["approved"] + b["external_match"]
        n = good + b["overridden"] + b["rejected"] + b["external_mismatch"]
        b["samples"] = n
        b["accuracy"] = round(good / n, 4) if n else None

    recommendation = _recommend(buckets)
    autonomy = effective_autonomy(db, defn, cfg)
    if recommendation:
        allowed = min(autonomy.task_cap, autonomy.global_cap, autonomy.code_max)
        recommendation["blocked_by_cap"] = allowed < 2
        recommendation["current_level"] = autonomy.level
    return {"task_key": task_key, "buckets": buckets, "recommendation": recommendation,
            "target_accuracy": TARGET_ACCURACY, "min_samples": MIN_SAMPLES, "autonomy": autonomy.to_dict()}


def _recommend(buckets: list[dict]) -> dict | None:
    """Lowest threshold whose cumulative accuracy (all buckets above it) meets the target."""
    best = None
    for lower in range(90, 30, -10):
        sel = [b for b in buckets if b["lower"] >= lower]
        n = sum(b["samples"] for b in sel)
        good = sum(b["approved"] + b["external_match"] for b in sel)
        vf = sum(b["verify_failed"] for b in sel)
        if n >= MIN_SAMPLES and good / n >= TARGET_ACCURACY and vf == 0:
            best = {"level": 2, "threshold": lower, "samples": n, "accuracy": round(good / n, 4)}
        elif best is not None:
            break
    return best
