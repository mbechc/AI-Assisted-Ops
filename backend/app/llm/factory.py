"""Selects the AI provider from the platform-wide ai_mode setting (Settings > AI assistance)."""
from sqlalchemy.orm import Session

from app.llm.azure_openai import AzureOpenAIClient
from app.llm.base import AI_MODES, LLMProvider
from app.llm.ollama import OllamaClient
from app.security import decrypt_secret
from app.store import get_setting


def requested_mode(db: Session) -> str:
    mode = get_setting(db, "ai_mode")
    return mode if mode in AI_MODES else "none"


def effective_mode(db: Session) -> str:
    """Local mode only takes effect once the host updater reports the model as ready."""
    mode = requested_mode(db)
    if mode == "local":
        from app.updates import llm_state
        return "local" if llm_state().get("state") == "ready" else "none"
    return mode


def cloud_client(cfg: dict | None) -> AzureOpenAIClient:
    cfg = cfg or {}

    def secret(field: str) -> str | None:
        token = cfg.get(field)
        if not token:
            return None
        try:
            return decrypt_secret(token)
        except Exception:  # noqa: BLE001 - a rotated FERNET_KEY must not break runs
            return None
    return AzureOpenAIClient(endpoint=cfg.get("endpoint") or "", deployment=cfg.get("deployment") or "",
                             api_version=cfg.get("api_version") or None, auth=cfg.get("auth") or "api_key",
                             api_key=secret("api_key_enc"), tenant_id=cfg.get("tenant_id"),
                             client_id=cfg.get("client_id"), client_secret=secret("client_secret_enc"))


def provider_for(db: Session, mode: str) -> LLMProvider | None:
    if mode == "local":
        return OllamaClient()
    if mode == "cloud":
        return cloud_client(get_setting(db, "ai_cloud"))
    return None


def get_provider(db: Session) -> LLMProvider | None:
    return provider_for(db, effective_mode(db))
