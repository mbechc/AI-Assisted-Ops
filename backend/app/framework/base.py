"""Contracts every Expert Task (use case) implements."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.connectors.catalyst_center import CatalystCenter
    from app.llm.base import LLMProvider
    from app.models import DataSource, WorkItem

LEVEL_NAMES = {0: "L0 Report only", 1: "L1 Human review", 2: "L2 Auto above threshold", 3: "L3 Full autonomy"}


@dataclass
class Proposal:
    target_key: str
    target_label: str
    action_type: str
    payload: dict
    rule_confidence: int
    evidence: list[dict]
    rationale: str
    concerns: list[str] = field(default_factory=list)
    group_label: str | None = None
    blocked_reason: str | None = None  # set when the item can never be auto-executed as-is
    llm_prompt: str | None = None  # compact evidence summary for the LLM reviewer
    fingerprint: str | None = None  # unchanged fingerprint -> reuse previous LLM review


@dataclass
class AnalysisResult:
    proposals: list[Proposal]
    # Targets whose open items should be closed as superseded, with the observed outcome.
    resolved: dict[str, dict] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


@dataclass
class ExecResult:
    success: bool
    detail: str = ""
    verified: bool | None = None
    data: dict = field(default_factory=dict)


@dataclass
class ParamSpec:
    key: str
    label: str
    type: str  # int | float | bool | str | json
    default: Any
    description: str = ""


class RunContext:
    def __init__(self, db: Session, task_key: str, source: DataSource | None, params: dict,
                 run_id: int | None = None, llm: LLMProvider | None = None):
        self.db = db
        self.task_key = task_key
        self.source = source
        self.params = params
        self.run_id = run_id
        self.llm = llm
        self._client: CatalystCenter | None = None
        self._log: list[str] = []
        self.llm_stats: dict = {}
        self.logger = logging.getLogger(f"task.{task_key}")

    @property
    def cc(self) -> CatalystCenter:
        if self._client is None:
            from app.sources import build_cc_client
            if self.source is None:
                raise RuntimeError("no data source configured for this task")
            self._client = build_cc_client(self.source)
            version = self._client.connect()
            self.log(f"Connected to {self.source.name} ({self.source.base_url}), release "
                     f"{'.'.join(map(str, version)) if version else 'undetected'} "
                     f"via {self._client.version_source}")
        return self._client

    def log(self, msg: str) -> None:
        line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
        self._log.append(line)
        self.logger.info(msg)

    @property
    def log_text(self) -> str:
        return "\n".join(self._log)

    def close(self) -> None:
        if self._client is not None:
            from app.sources import save_capabilities
            if self.source is not None:
                save_capabilities(self.db, self.source, self._client)
            self._client.close()


class TaskDefinition(ABC):
    key: str
    name: str
    description: str
    category: str = "General"
    worker: str = "default"  # specialist worker container (see app.workers.WORKERS)
    source_kind: str = "catalyst_center"
    optional_sources: tuple[str, ...] = ()  # extra source kinds used when configured, e.g. ("geoip",)
    default_cron: str = "0 6 * * *"
    max_autonomy: int = 3  # hard ceiling defined in code
    default_autonomy_level: int = 1
    default_autonomy_cap: int = 1
    default_auto_threshold: int = 95
    default_max_auto_per_run: int = 50
    action_types: dict[str, str] = {}
    params: list[ParamSpec] = []
    llm_system_prompt: str = ""
    # AI features (key -> short description). Every feature has a rule-based fallback, so a task with
    # features is "AI Capable" with AI off and "AI Assisted" with a provider. Empty: no AI label.
    ai_features: dict[str, str] = {}

    def default_params(self) -> dict:
        return {p.key: p.default for p in self.params}

    @abstractmethod
    def analyze(self, ctx: RunContext) -> AnalysisResult:
        ...

    @abstractmethod
    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        ...

    def apply_override(self, ctx: RunContext, item: WorkItem, override: dict) -> dict:
        """Validate an operator override and return the payload to execute."""
        raise ValueError("this task does not support overrides")

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        """Describe what happened outside the platform; `matched` feeds calibration."""
        return {**observed, "matched": None}

    def effective_payload(self, item: WorkItem) -> dict:
        return {**(item.payload or {}), **(item.override_payload or {})}
