"""Cloud AI through the customer's own Azure OpenAI (Azure AI Foundry) deployment.

Microsoft 365 Copilot has no model API an appliance can call; Azure OpenAI is the Microsoft-hosted
equivalent that stays inside the customer's tenant and contract. Proxy variables from .env apply (httpx
reads HTTPS_PROXY / NO_PROXY).
"""
import copy
import json
import threading
import time
from typing import Any

import httpx

from app.config import get_settings
from app.llm.base import REVIEW_SCHEMA, LLMUnavailable, normalize_review

DEFAULT_API_VERSION = "2024-10-21"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
# Keywords the structured-output (strict) mode rejects; the prompt states the ranges instead.
_UNSUPPORTED = ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern", "format")

_token_lock = threading.Lock()
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


def strict_schema(schema: dict) -> dict:
    """Azure structured outputs need additionalProperties=false and every property required."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            for k in _UNSUPPORTED:
                node.pop(k, None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


class AzureOpenAIClient:
    name = "cloud"
    label = "Cloud AI"

    def __init__(self, endpoint: str, deployment: str, api_version: str | None = None, auth: str = "api_key",
                 api_key: str | None = None, tenant_id: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, timeout: float | None = None):
        self.endpoint = (endpoint or "").rstrip("/")
        self.deployment = deployment or ""
        self.api_version = api_version or DEFAULT_API_VERSION
        self.auth = auth if auth in ("api_key", "entra") else "api_key"
        self.api_key = api_key
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.timeout = timeout or min(get_settings().llm_timeout_seconds, 120)

    @property
    def configured(self) -> bool:
        if not (self.endpoint.startswith("https://") and self.deployment):
            return False
        if self.auth == "entra":
            return bool(self.tenant_id and self.client_id and self.client_secret)
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if self.auth == "api_key":
            return {"api-key": self.api_key or ""}
        return {"Authorization": f"Bearer {self._token()}"}

    def _token(self) -> str:
        key = (self.tenant_id or "", self.client_id or "")
        with _token_lock:
            cached = _token_cache.get(key)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
        try:
            r = httpx.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token", timeout=20,
                           data={"grant_type": "client_credentials", "client_id": self.client_id,
                                 "client_secret": self.client_secret, "scope": TOKEN_SCOPE})
        except httpx.TransportError as exc:
            raise LLMUnavailable(f"Entra ID token request failed: {exc}") from exc
        if r.status_code != 200:
            raise LLMUnavailable(f"Entra ID token request failed: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        token = body["access_token"]
        with _token_lock:
            _token_cache[key] = (token, time.time() + int(body.get("expires_in", 3600)))
        return token

    def status(self) -> dict:
        base = {"provider": self.name, "endpoint": self.endpoint, "model": self.deployment}
        if not self.configured:
            return {**base, "reachable": False, "ready": False, "error": "Azure OpenAI is not configured"}
        return {**base, "reachable": None, "ready": True}

    def chat_json(self, system: str, user: str, schema: dict, num_ctx: int = 4096) -> dict[str, Any]:
        if not self.configured:
            raise LLMUnavailable("Azure OpenAI is not configured")
        url = f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions"
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "steward_result", "strict": True,
                                                "schema": strict_schema(schema)}},
        }
        try:
            r = httpx.post(url, params={"api-version": self.api_version}, json=payload, headers=self._headers(),
                           timeout=self.timeout)
        except httpx.TransportError as exc:
            raise LLMUnavailable(f"Azure OpenAI not reachable: {exc}") from exc
        if r.status_code in (401, 403):
            raise LLMUnavailable(f"Azure OpenAI rejected the credentials (HTTP {r.status_code})")
        if r.status_code == 404:
            raise LLMUnavailable(f"Azure OpenAI deployment {self.deployment} not found")
        if r.status_code == 429:
            raise LLMUnavailable("Azure OpenAI rate limit or quota reached (HTTP 429)")
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or ""
        return json.loads(content)

    def review(self, system: str, user: str) -> dict[str, Any]:
        return normalize_review(self.chat_json(
            system + "\nconfidence is an integer from 0 to 100.", user, REVIEW_SCHEMA))
