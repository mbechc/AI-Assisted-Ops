"""Settings > AI assistance: platform-wide AI mode (none | local | cloud).

Local LLM: the portal never creates containers; it queues llm-* requests for the host updater
(scripts/update.sh), which starts Ollama and pulls the model only after an admin enables it here.
"""
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import updates
from app.audit import audit
from app.deps import current_user, get_db, require
from app.llm.base import AI_MODES, MODE_LABELS, PROBE_SCHEMA, LLMUnavailable
from app.llm.factory import cloud_client, effective_mode, requested_mode
from app.models import User
from app.security import ROLE_RANK, encrypt_secret
from app.store import get_setting, set_setting

router = APIRouter(tags=["ai"])

LOCAL_BUSY = ("queued", "installing", "pulling")
CLOUD_FIELDS = ("endpoint", "deployment", "api_version", "auth", "tenant_id", "client_id")


class CloudIn(BaseModel):
    endpoint: str | None = None
    deployment: str | None = None
    api_version: str | None = None
    auth: Literal["api_key", "entra"] | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    api_key: str | None = None  # write-only
    client_secret: str | None = None  # write-only


class AiSettingsIn(BaseModel):
    mode: Literal["none", "local", "cloud"] | None = None
    cloud: CloudIn | None = None
    remove_local: bool = False  # when leaving local mode: delete the model volume instead of only stopping


class LocalActionIn(BaseModel):
    action: Literal["enable", "disable", "remove"]


class TestIn(BaseModel):
    mode: Literal["local", "cloud"] | None = None
    cloud: CloudIn | None = None


def _public_cloud(cfg: dict) -> dict:
    out = {k: cfg.get(k) or "" for k in CLOUD_FIELDS}
    out["api_key_set"] = bool(cfg.get("api_key_enc"))
    out["client_secret_set"] = bool(cfg.get("client_secret_enc"))
    return out


def _merge_cloud(saved: dict, body: CloudIn | None) -> dict:
    cfg = dict(saved or {})
    if body is None:
        return cfg
    for k in CLOUD_FIELDS:
        v = getattr(body, k)
        if v is not None:
            cfg[k] = v.strip()
    if cfg.get("endpoint"):
        cfg["endpoint"] = cfg["endpoint"].rstrip("/")
    if body.api_key:
        cfg["api_key_enc"] = encrypt_secret(body.api_key.strip())
    if body.client_secret:
        cfg["client_secret_enc"] = encrypt_secret(body.client_secret.strip())
    return cfg


def ai_state(db: Session, user: User | None = None) -> dict:
    mode = requested_mode(db)
    return {
        "mode": mode,
        "effective_mode": effective_mode(db),
        "labels": MODE_LABELS,
        "local": updates.llm_state(),
        "cloud": _public_cloud(get_setting(db, "ai_cloud") or {}),
        "can_manage": user is not None and ROLE_RANK.get(user.role, -1) >= ROLE_RANK["admin"],
    }


def _queue(action: str) -> None:
    try:
        updates.queue(action)
    except OSError as exc:
        raise HTTPException(503, f"cannot reach the host updater folder: {exc}") from exc


@router.get("/settings/ai")
def get_ai(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return ai_state(db, user)


@router.put("/settings/ai")
def put_ai(body: AiSettingsIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    before_mode = requested_mode(db)
    saved = get_setting(db, "ai_cloud") or {}
    cfg = _merge_cloud(saved, body.cloud)
    mode = body.mode or before_mode
    if mode not in AI_MODES:
        raise HTTPException(400, "invalid mode")
    if mode == "cloud" and not cloud_client(cfg).configured:
        raise HTTPException(400, "Azure OpenAI needs an https endpoint, a deployment and credentials")
    if mode == "local" and before_mode != "local" and updates.state()["install_running"]:
        raise HTTPException(409, "an update is in progress; enable the local LLM afterwards")

    queued = None
    if mode == "local" and updates.llm_state()["state"] not in LOCAL_BUSY + ("ready",):
        queued = "llm-enable"
    elif before_mode == "local" and mode != "local":
        queued = "llm-remove" if body.remove_local else "llm-disable"
    if queued:
        _queue(queued)

    if cfg != saved:
        set_setting(db, "ai_cloud", cfg)
    set_setting(db, "ai_mode", mode)
    changed = sorted(k for k in CLOUD_FIELDS if (cfg.get(k) or "") != (saved.get(k) or ""))
    if cfg.get("api_key_enc") != saved.get("api_key_enc"):
        changed.append("api_key")
    if cfg.get("client_secret_enc") != saved.get("client_secret_enc"):
        changed.append("client_secret")
    audit(db, admin.username, "settings.ai_updated", "settings", "ai", before=before_mode, after=mode,
          cloud_fields_changed=changed, host_request=queued)
    db.commit()
    return ai_state(db, admin)


@router.post("/settings/ai/local")
def local_action(body: LocalActionIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    """Retry a failed install, or stop / delete the local LLM container and model."""
    if body.action == "enable" and updates.state()["install_running"]:
        raise HTTPException(409, "an update is in progress; enable the local LLM afterwards")
    _queue(f"llm-{body.action}")
    if body.action != "enable" and requested_mode(db) == "local":
        set_setting(db, "ai_mode", "none")
    audit(db, admin.username, f"settings.ai_local_{body.action}", "settings", "ai")
    db.commit()
    return ai_state(db, admin)


@router.get("/settings/ai/log")
def local_log(_=Depends(require("admin"))):
    return {"log": updates.llm_log_tail()}


@router.post("/settings/ai/test")
def test_ai(body: TestIn, db: Session = Depends(get_db), _=Depends(require("admin"))):
    """Probe a provider. For cloud, unsaved form values are tested on top of the saved secrets."""
    mode = body.mode or requested_mode(db)
    if mode == "cloud":
        provider = cloud_client(_merge_cloud(get_setting(db, "ai_cloud") or {}, body.cloud))
    elif mode == "local":
        from app.llm.ollama import OllamaClient
        provider = OllamaClient()
    else:
        raise HTTPException(400, "No AI is selected: nothing to test")
    t0 = time.monotonic()
    try:
        out = provider.chat_json("Reply with JSON only.", 'Return {"ok": true}', PROBE_SCHEMA)
        ok, error = bool(out.get("ok")), None
    except LLMUnavailable as exc:
        ok, error = False, str(exc)
    except Exception as exc:  # noqa: BLE001
        ok, error = False, f"{type(exc).__name__}: {exc}"
    return {"ok": ok, "error": error, "latency_ms": int((time.monotonic() - t0) * 1000),
            "provider": provider.label, "model": provider.status().get("model")}
