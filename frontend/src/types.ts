export type Role = "viewer" | "operator" | "admin";

export interface User {
  id: number;
  username: string;
  role: Role;
  is_active: boolean;
  last_login?: string | null;
  created_at?: string;
  theme?: "dark" | "light";
}

export interface ParamSpec {
  key: string;
  label: string;
  type: "int" | "float" | "bool" | "str" | "json";
  default: unknown;
  description: string;
}

export interface Autonomy {
  level: number;
  selected: number;
  task_cap: number;
  global_cap: number;
  code_max: number;
  limited_by: string | null;
  kill_switch: boolean;
}

export interface Run {
  id: number;
  task_key: string;
  source_id: number | null;
  trigger: string;
  triggered_by: string | null;
  status: "queued" | "running" | "success" | "failed";
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  stats: Record<string, unknown>;
  error: string | null;
  log?: string;
}

export interface Task {
  key: string;
  name: string;
  description: string;
  category: string;
  source_kind: string;
  worker: string;
  optional_sources: string[];
  ai_features: Record<string, string>;
  ai_mode: AiMode;
  action_types: Record<string, string>;
  max_autonomy: number;
  level_names: Record<string, string>;
  param_specs: ParamSpec[];
  config: {
    enabled: boolean;
    cron: string;
    source_id: number | null;
    autonomy_level: number;
    autonomy_cap: number;
    auto_threshold: number;
    max_auto_per_run: number;
    params: Record<string, unknown>;
    updated_at: string;
    updated_by: string | null;
  };
  autonomy: Autonomy;
  counts: Record<string, number>;
  last_run: Run | null;
  next_run: string | null;
}

export interface EvidenceVote {
  site_id: string;
  site: string;
  strength: number;
}

export interface Evidence {
  signal: string;
  label: string;
  available: boolean;
  weight: number;
  detail: string;
  buildings: EvidenceVote[];
  floors: EvidenceVote[];
  agrees: boolean;
}

export interface WorkItem {
  id: number;
  task_key: string;
  target_key: string;
  target_label: string;
  group_label: string | null;
  action_type: string;
  confidence: number;
  rule_confidence: number;
  llm_confidence: number | null;
  llm_status: string;
  llm_provider: "local" | "cloud" | null;
  status: string;
  blocked_reason: string | null;
  rationale: string;
  concerns: string[];
  decided_by: string | null;
  decided_at: string | null;
  verify_status: string | null;
  updated_at: string;
  created_at: string;
  proposed: string;
  payload?: Record<string, any>;
  override_payload?: Record<string, any> | null;
  evidence?: Evidence[];
  decision_note?: string | null;
  exec_result?: Record<string, any> | null;
  outcome?: Record<string, any> | null;
  executed_at?: string | null;
  source_id?: number | null;
}

export interface Group {
  task_key: string;
  source_id: number | null;
  group: string | null;
  count: number;
  avg_confidence: number;
  min_confidence: number;
  max_confidence: number;
  above_threshold: number;
  blocked: number;
  threshold: number;
}

export interface FacetValue {
  value: string;
  count: number;
}

export interface Facets {
  sources: { id: number | null; name: string; count: number }[];
  tasks: FacetValue[];
  statuses: FacetValue[];
  groups: FacetValue[];
}

export interface SavedFilter {
  id: string;
  name: string;
  query: string;
  owner: string;
  shared: boolean;
  saved_at: string;
  mine: boolean;
  can_delete: boolean;
}

export interface Source {
  id: number;
  name: string;
  kind: string;
  base_url: string;
  username: string;
  verify_tls: boolean;
  has_ca_bundle: boolean;
  rate_limit_rps: number;
  enabled: boolean;
  detected_version: string | null;
  last_check_at: string | null;
  last_status: string | null;
  version_source: string | null;
  write_refused_at?: string | null;
}

export interface CapabilityRow {
  operation: string;
  selected: string | null;
  candidates: { name: string; api: string; path: string; verified: boolean; applies: boolean; status: string }[];
}

export interface Site {
  id: string;
  name: string;
  hierarchy: string;
  type: string;
}

export interface ContainerInfo {
  id: string; service: string; name: string; state: string | null; health: string | null; restarts: number;
  started_at: string | null; oom_killed: boolean; cpu_percent: number | null; mem_bytes: number | null; mem_limit: number | null;
}

export interface RunnerWorker {
  name: string; label: string; queue: string; queue_depth: number | null; alive: boolean;
  heartbeat: { hostname: string; pid: number; queues: string[]; concurrency: number | null; started_at: number; ts: number } | null;
  active: { task: string; started: number; run_id: number | null; task_key: string | null }[];
  queued_runs: { run_id: number; task_key: string; created_at: string }[];
  counters: Record<string, number>; tasks: string[]; runs_24h: Record<string, number>;
  failures_24h: { run_id: number; task_key: string; finished_at: string | null; error: string[] }[];
  restarts_seen: number; last_seen: string | null; container: ContainerInfo | null;
}

export interface RunnersOverview {
  now: string; redis_ok: boolean; docker_error: string | null; scheduler_alive: boolean;
  workers: RunnerWorker[]; services: ContainerInfo[]; legacy_queues: Record<string, number>;
  schedule: { task_key: string; name: string; worker: string; cron: string; enabled: boolean; next: string[];
    last_run: { id: number; status: string; created_at: string; finished_at: string | null } | null }[];
}

export interface Endpoint {
  host: string;
  ok: boolean;
  ms: number;
  error: string | null;
}

export interface Connectivity {
  status: "connected" | "airgapped" | "unknown";
  checked_at: string | null;
  endpoints: Endpoint[];
  failed: string[];
  reason: string | null;
  interval_min: number;
}

export interface Release {
  version: string;
  tag: string;
  prerelease: boolean;
  name: string;
  published_at: string | null;
  notes: string;
  url: string | null;
}

export type AiMode = "none" | "local" | "cloud";

export interface AiCloudConfig {
  endpoint: string;
  deployment: string;
  api_version: string;
  auth: "api_key" | "entra";
  tenant_id: string;
  client_id: string;
  api_key_set: boolean;
  client_secret_set: boolean;
}

export interface AiState {
  mode: AiMode;
  effective_mode: AiMode;
  labels: Record<AiMode, string>;
  local: {
    state: "absent" | "queued" | "installing" | "pulling" | "ready" | "failed" | "disabled" | "removed";
    model: string;
    updated_at: string | null;
    error: string | null;
    pending: string[];
    updater_stale: boolean;
  };
  cloud: AiCloudConfig;
  can_manage: boolean;
}

export interface UpdateState {
  current_version: string;
  channel: "production" | "development";
  channel_pending: string | null;
  repo: string;
  connectivity: Connectivity;
  releases_checked_at: string | null;
  releases_error: string | null;
  available: Release | null;
  install: { state: "running" | "succeeded" | "failed"; version: string | null; started_at: string | null;
             finished_at: string | null; error: string | null } | null;
  install_running: boolean;
  pending: { action: string; age_seconds: number }[];
  updater_stale: boolean;
  updater_installed: boolean;
  can_manage: boolean;
}
