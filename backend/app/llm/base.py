"""Provider-neutral contract for the optional AI layer (local Ollama or Azure OpenAI)."""
from typing import Any, Protocol

AI_MODES = ("none", "local", "cloud")
MODE_LABELS = {"none": "No AI", "local": "Local AI", "cloud": "Cloud AI"}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "rationale": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["confidence", "rationale", "concerns"],
}

PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class LLMUnavailable(Exception):
    """The provider cannot be reached or is not set up; callers fall back to rules only."""


class LLMProvider(Protocol):
    name: str  # local | cloud
    label: str

    def status(self) -> dict: ...

    def chat_json(self, system: str, user: str, schema: dict, num_ctx: int = 4096) -> dict[str, Any]: ...

    def review(self, system: str, user: str) -> dict[str, Any]: ...


def normalize_review(out: dict) -> dict[str, Any]:
    conf = int(max(0, min(100, int(out.get("confidence", 0)))))
    return {
        "confidence": conf,
        "rationale": str(out.get("rationale", "")).strip()[:2000],
        "concerns": [str(c)[:300] for c in (out.get("concerns") or [])][:8],
    }
