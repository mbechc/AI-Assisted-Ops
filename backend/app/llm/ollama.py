"""Local LLM access through Ollama with schema-constrained JSON output."""
import json
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.llm.base import REVIEW_SCHEMA, LLMUnavailable, normalize_review

log = logging.getLogger(__name__)

__all__ = ["OllamaClient", "LLMUnavailable", "REVIEW_SCHEMA"]


class OllamaClient:
    name = "local"
    label = "Local AI"

    def __init__(self, url: str | None = None, model: str | None = None, timeout: float | None = None):
        s = get_settings()
        self.url = (url or s.ollama_url).rstrip("/")
        self.model = model or s.ollama_model
        self.timeout = timeout or s.llm_timeout_seconds

    def status(self) -> dict:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])]
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            present = wanted in names
            return {"provider": self.name, "reachable": True, "model": self.model, "model_present": present,
                    "ready": present, "models": names}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "reachable": False, "model": self.model, "model_present": False,
                    "ready": False, "error": str(exc)}

    def chat_json(self, system: str, user: str, schema: dict, num_ctx: int = 4096) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            r = httpx.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
        except httpx.TransportError as exc:
            raise LLMUnavailable(str(exc)) from exc
        if r.status_code == 404:
            raise LLMUnavailable(f"model {self.model} not present")
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        return json.loads(content)

    def review(self, system: str, user: str) -> dict[str, Any]:
        return normalize_review(self.chat_json(system, user, REVIEW_SCHEMA))
