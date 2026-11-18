"""Local LLM access through Ollama with schema-constrained JSON output."""
import json
import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "rationale": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["confidence", "rationale", "concerns"],
}


class LLMUnavailable(Exception):
    pass


class OllamaClient:
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
            return {"reachable": True, "model": self.model, "model_present": wanted in names, "models": names}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "model": self.model, "model_present": False, "error": str(exc)}

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
        out = self.chat_json(system, user, REVIEW_SCHEMA)
        conf = int(max(0, min(100, int(out.get("confidence", 0)))))
        return {
            "confidence": conf,
            "rationale": str(out.get("rationale", "")).strip()[:2000],
            "concerns": [str(c)[:300] for c in (out.get("concerns") or [])][:8],
        }
