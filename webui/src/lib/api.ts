export type ApiMeta = {
  request_id?: string;
  schema_version?: string;
  next_cursor?: string | null;
  snapshot_id?: string;
  schema_versions?: string[];
  count?: number;
  warnings?: string[];
  [key: string]: unknown;
};

export type ApiResult<T> = { data: T; meta: ApiMeta };
type Envelope<T> = {
  data: T;
  meta: ApiMeta;
  errors: Array<{ message: string; code?: string }>;
};

export type RunScope = "production" | "acceptance" | "migration" | "maintenance";
export type EconomicQueryMode = "current" | "pit";

export type Dataset = {
  dataset_id: string;
  schema_version: string;
  description: string;
  partitioning: string[];
};

export type RunWindowReceipt = {
  ordinal: number;
  start: string;
  end: string;
  reason?: string;
  semantics?: string;
  row_count?: number;
  min_ts?: string;
  max_ts?: string;
};

export type RunAttemptError = {
  attempt: number;
  error_type: string;
  failure_stage: string;
  error: string;
  retryable: boolean;
  at: string;
};

export type Run = {
  run_id: string;
  dataset_id: string;
  status: string;
  outcome?: string;
  stage?: string;
  job_id?: string;
  provider?: string;
  symbol?: string;
  timeframe?: string;
  series_id?: string;
  recipe_id?: string;
  recipe_version?: string;
  run_kind?: RunKind;
  window_count?: number;
  attempt_count?: number;
  attempt_errors?: RunAttemptError[];
  windows?: RunWindowReceipt[];
  schema_version?: string;
  output_hash?: string;
  min_ts?: string;
  max_ts?: string;
  min_date?: string;
  max_date?: string;
  next_attempt_at?: string | null;
  manifest_status?: string;
  finding_count?: number;
  row_count?: number;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error_type?: string;
  retry_count?: number;
  retry_of?: string;
  error?: string;
  run_scope?: RunScope | string;
  dead_letter_state?: {
    state?: string;
    acknowledged_at?: string;
    resolved_by_run_id?: string;
  };
};

export type IngestJob = {
  job_id: string;
  dataset_id: "provider_bars";
  provider: string;
  symbol: string;
  asset_class: string;
  timeframe: string;
  start: string;
  end: string;
  run_scope: RunScope;
};

export type IngestReceipt = {
  status: "queued" | string;
  state?: string;
  job_id: string;
  run_id: string;
  run_ids?: string[];
  window_count?: number;
  dataset_id?: string;
  run_kind?: RunKind;
  run_scope?: RunScope | string;
};

export type Finding = {
  finding_id?: string;
  severity: string;
  code: string;
  dataset_id?: string;
  run_id?: string;
  job_id?: string;
  series_id?: string;
  observation_date?: string;
  bar_ts?: string;
  message?: string;
  // Findings are additive records: identity, handling state and observation
  // counts stay separate from the run receipt that reported them.
  state?: FindingState;
  state_updated_at?: string | null;
  resolved_by_run_id?: string | null;
  occurrence_count?: number;
  first_observed_at?: string;
  last_observed_at?: string;
  last_run_id?: string;
  selector?: Record<string, string>;
  coverage?: CoverageReport;
};

export type Bar = {
  bar_ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

export type EconomicObservation = {
  observation_date: string;
  value: number | null;
  release_ts?: string;
  asof_ts?: string;
  frequency?: string;
  units?: string;
  missing_reason?: string;
};

export type ReadyState = {
  status: string;
  read_status: string;
  write_status: string;
  capacity_status: string;
  capacity_free_ratio?: number | null;
  operational_snapshot_status: string;
  worker_heartbeat_age_seconds: number | null;
  deployment_id: string;
  software_version: string;
  source_commit: string;
};

export type CapacityMetrics = {
  status: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  free_ratio: number;
  warning_free_ratio: number;
  critical_free_ratio: number;
};

export type SliWindow = { runs: number; passed: number; success_rate: number | null };
export type Metrics = {
  runs_total: number;
  runs_by_status: Record<string, number>;
  retry_attempts_total: number;
  timeouts_total: number;
  queue_depth: number;
  queue_oldest_age_seconds: number;
  dead_letter_total: number;
  dead_letter_by_state?: Record<string, number>;
  success_rate: number | null;
  production_sli?: Record<"1h" | "24h" | "7d", SliWindow>;
  worker_heartbeat_age_seconds: number | null;
  capacity: CapacityMetrics | null;
  last_successful_backup_at: string | null;
  last_successful_recovery_drill_at: string | null;
  temporary_backup_count: number;
  operational_snapshot_status: string;
  deployment_id: string;
  software_version: string;
  source_commit: string;
  query?: Record<string, unknown>;
};

export type BarsQuery = {
  provider: string;
  symbol: string;
  timeframe: string;
  start?: string;
  end?: string;
};

export type EconomicQuery = {
  provider: string;
  series_id: string;
  mode: EconomicQueryMode;
  start?: string;
  end?: string;
  asof_ts?: string;
};

export type BarsCoverage = {
  dataset_id: "provider_bars" | string;
  provider: string;
  symbol: string;
  timeframe: string;
  row_count: number;
  min_ts: string | null;
  max_ts: string | null;
};

// Coverage responses carry the governed readiness state when the API can
// evaluate it; summary-only responses leave those fields absent on purpose.
export type CoverageReport = BarsCoverage & {
  coverage_scope?: string;
  readiness_status?: string;
  gap_count?: number | null;
  missing_timestamp_count?: number | null;
  expected_timestamp_count?: number;
  duplicate_count?: number;
  first_missing_ts?: string | null;
  latest_complete_boundary?: string | null;
  ready_interval_count?: number;
  ready_intervals?: Array<{ start: string; end: string; semantics: string }>;
  timeframe_seconds?: number;
  calendar_unit?: string;
  selector?: Record<string, string>;
  quality_status?: string;
};

export type MarketBarsCoverage = {
  dataset_id: "market_bars" | string;
  provider: string;
  symbol: string;
  timeframe: string;
  price_basis: string;
  recipe_id: string;
  recipe_version: string;
  row_count: number;
  min_ts: string | null;
  max_ts: string | null;
  input_snapshot_ids?: string[];
  input_snapshot_count?: number;
  recipe_status?: "registered" | "not_registered";
  recipe?: RecipeInfo;
  readiness_status?: string;
  gap_count?: number | null;
  ready_intervals?: Array<{ start: string; end: string; semantics: string }>;
};

// A derived selector is only meaningful together with its recipe and price
// basis, so the market query carries both explicitly instead of implying them.
export type MarketBarsQuery = {
  provider: string;
  symbol: string;
  timeframe: string;
  price_basis: string;
  recipe_id: string;
  recipe_version: string;
  start?: string;
  end?: string;
};

// Detailed market coverage adds the governed coverage fields on top of the
// physical summary.  A summary-only response leaves them absent on purpose, so
// the console reports "not published" instead of inventing a readiness state.
export type MarketBarsCoverageReport = MarketBarsCoverage & {
  coverage_scope?: string;
  expected_timestamp_count?: number;
  duplicate_count?: number;
  latest_complete_boundary?: string | null;
  ready_interval_count?: number;
  missing_timestamp_count?: number | null;
  first_missing_ts?: string | null;
  timeframe_seconds?: number;
  calendar_unit?: string;
  physical_coverage?: string;
  session_coverage?: string;
  quality_status?: string;
  selector?: Record<string, string>;
};

export type EconomicCoverage = {
  dataset_id: "economic_observations" | string;
  provider: string;
  series_id: string;
  row_count: number;
  min_date: string | null;
  max_date: string | null;
};

export type RequestOptions = { allowStatuses?: readonly number[]; skipApiKey?: boolean };

// -- v0.4 maintenance workbench contracts --------------------------------

export type RunKind = "ingest" | "derive" | "backfill" | "gap_repair" | "quality" | "parity";
export type WriteStatus = "available" | "protected";

export type MaintenanceTaskRequest = {
  run_kind: RunKind;
  run_scope: RunScope;
  dataset_id?: string | null;
  provider: string;
  symbol?: string | null;
  asset_class?: string | null;
  timeframe?: string | null;
  series_id?: string | null;
  recipe_id?: string | null;
  recipe_version?: string | null;
  price_basis?: string | null;
  start: string;
  end: string;
  task_id?: string | null;
  schedule?: "manual";
};

/**
 * A hand-off from another workspace into Maintenance. Each workspace carries only
 * the facts it actually established: provider, symbol, window and the rest stay
 * absent when the page never recorded them, and Maintenance keeps its own values
 * for those fields instead of guessing on the operator's behalf.
 */
export type MaintenanceTaskDraft = Partial<MaintenanceTaskRequest> & { source?: string };

export type MaintenanceTask = {
  task_id: string;
  run_kind: RunKind;
  run_scope: RunScope;
  dataset_id: string;
  provider: string;
  symbol: string | null;
  asset_class: string | null;
  timeframe: string | null;
  series_id: string | null;
  recipe_id: string | null;
  recipe_version: string | null;
  price_basis: string | null;
  start: string;
  end: string;
  time_range: { start: string; end: string; semantics: string };
};

export type ValidationIssue = { field: string; code: string; message: string };
export type PlanWindow = { ordinal: number; start: string; end: string; reason: string; semantics: string };
export type MaintenancePlan = {
  plan_id: string;
  reason: string;
  window_count: number;
  semantics: string;
  windows: PlanWindow[];
  truncated: boolean;
  session_profile?: string | null;
  config_digest?: string | null;
};

export type CapacityReport = {
  status: string;
  free_ratio: number | null;
  warning_free_ratio: number;
  critical_free_ratio: number;
  requested_days: number;
  policy: string;
  estimated_windows: number;
  write_status: WriteStatus;
  protected_reason: { code: string; message: string } | null;
  blocked_by_validation: boolean;
};

export type RecipeInfo = {
  recipe_id: string;
  recipe_version: string;
  input_dataset: string;
  output_dataset: string;
  source_timeframe: string;
  target_timeframe: string;
  price_bases: string[];
  session_profile: string;
  materialization: string;
  partial_bucket_policy: string;
  missing_input_policy: string;
};

export type ProviderCapability = {
  provider: string;
  asset_classes: string[];
  timeframes: string[];
  maintenance_timeframes: string[];
  price_bases: string[];
  max_window_days: number;
  session_profile: string;
  instruments: Array<{
    provider: string;
    symbol: string;
    canonical_symbol: string;
    asset_class: string;
    currency: string;
    session_profile: string;
    calendar_profile: string;
    approved: boolean;
  }>;
};

export type Capabilities = {
  datasets: Array<Dataset & { kind: string; quality_profile: string; query_modes: string[]; materialization_policy: string }>;
  providers: ProviderCapability[];
  recipes: RecipeInfo[];
  run_kinds: Array<{ run_kind: RunKind; datasets: string[] }>;
  economic_series_provider: string;
  write_status: WriteStatus;
  capacity: CapacityMetrics;
  maintenance_policies: Array<{
    policy_id: string;
    max_window_days: number;
    tail_days: number;
    shard_days: number;
    shard_minutes: number | null;
    closed_bar_lag_minutes: number;
  }>;
};

export type SnapshotSummary = {
  input_snapshot_id: string;
  dataset_id: string;
  part_count: number;
  schema_versions: string[];
};

export type TaskPreview = {
  task: MaintenanceTask;
  validation: { status: "valid" | "invalid"; errors: ValidationIssue[]; warnings: Array<{ code: string; message: string }> };
  plan: MaintenancePlan | null;
  coverage: CoverageReport | null;
  snapshot: SnapshotSummary | null;
  recipe: RecipeInfo | null;
  capability: ProviderCapability | null;
  capacity: CapacityReport;
  submittable: boolean;
  write_status: WriteStatus;
  generated_at: string;
};
export type MaintenanceTaskRecord = { task_id: string; run_kind: RunKind; dataset_id: string; status: string; run_ids?: string[]; submitted_at?: string; updated_at?: string; schedule?: "manual" | null; recent_run_id?: string | null; recent_run_at?: string | null; recent_error?: string | null };

export type QueuedEnvelope = {
  status: "queued" | string;
  state: string;
  task_id: string;
  job_id: string;
  dataset_id: string;
  run_kind: RunKind;
  run_scope: RunScope | string;
  provider?: string | null;
  symbol?: string | null;
  series_id?: string | null;
  selector: Record<string, string>;
  time_range: { start: string; end: string; semantics: string };
  run_id: string | null;
  run_ids: string[];
  window_count: number;
  plan_id: string | null;
  input_snapshot_id: string | null;
  capacity: CapacityReport;
  warnings: Array<{ code: string; message: string }>;
  submitted_at: string;
  audit_id: number | null;
};

export type RunFilters = {
  status?: string;
  dataset_id?: string;
  run_kind?: string;
  run_scope?: string;
  provider?: string;
  symbol?: string;
  created_from?: string;
  created_to?: string;
};

export type PageInfo = { count: number; next_cursor: string | null; page_size: number | null; paginated: boolean; has_more: boolean; order: string };

export type DegradedReason = { code: string; message: string; source: string };
export type RetryLink = { run_id: string; status: string; created_at?: string; relation: "origin" | "retry"; stage: string; retry_of?: string | null };

export type RunDetail = Run & {
  stage: string;
  outcome: string;
  terminal: boolean;
  degraded_reasons: DegradedReason[];
  selector: Record<string, string>;
  time_range: { start: string; end: string; semantics: string } | null;
  window_count: number;
  input_snapshot_id?: string | null;
  manifest_status: "published" | "missing" | "not_applicable" | "unknown";
  finding_count: number;
  retry_chain: RetryLink[];
  findings?: Finding[];
  verification?: { kind: string; checked_at: string; publishes_parts: boolean };
  coverage?: CoverageReport | null;
};

export type FindingState = "open" | "acknowledged" | "resolved";

export type OperationAuditEntry = {
  audit_id: number;
  at: string;
  action: string;
  actor: string | null;
  request_id: string | null;
  task_id: string | null;
  run_ids: string[];
  run_kind: string | null;
  run_scope: string | null;
  dataset_id: string | null;
  selector: Record<string, string>;
  time_range: { start?: string; end?: string };
  outcome: string;
  code: string | null;
  message: string | null;
};

export type WorkerActivity = {
  heartbeat_age_seconds: number | null;
  heartbeat_status: "fresh" | "stale" | "unknown";
  heartbeat_limit_seconds: number;
  worker_heartbeat_age_seconds?: number | null;
  running_jobs: Array<{ job_id: string; run_id: string; attempts: number }>;
  running_count: number;
  queue: QueueState;
  observed_at: string;
};

export type CapacityEvent = {
  event: string;
  created_at: string | null;
  status?: string;
  free_ratio?: number | null;
  warning_free_ratio?: number;
  critical_free_ratio?: number;
};

export type CapacityHistory = {
  live: CapacityMetrics & { fixed_measurement?: boolean };
  events: CapacityEvent[];
  event_count: number;
  recorded_only: boolean;
  note: string;
};

export type OperationsReceipt = {
  action: string;
  completed_at: string;
  deployment_id: string | null;
  reference: string;
  result: string;
  fields: Record<string, unknown>;
};

export type ReceiptHistory = {
  available: boolean;
  receipts: OperationsReceipt[];
  latest: Record<string, OperationsReceipt | null>;
  note: string | null;
};

export type QueueState = {
  queued: number;
  running: number;
  completed: number;
  by_status: Record<string, number>;
  oldest_queued_available_at: string | null;
  runs_by_status: Record<string, number>;
};

const queryString = (params: Record<string, string | number | null | undefined>) => {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
};

export class DataCenterError extends Error {
  readonly status: number;
  readonly requestId?: string;
  readonly code?: string;

  constructor(message: string, status: number, requestId?: string, code?: string) {
    super(message);
    this.name = "DataCenterError";
    this.status = status;
    this.requestId = requestId;
    this.code = code;
  }
}

export function createDataCenterClient(apiKey: string) {
  const request = async <T,>(path: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<ApiResult<T>> => {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (apiKey && !options.skipApiKey) headers.set("X-API-Key", apiKey);
    const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "include" });
    let payload: Envelope<T>;
    try {
      payload = await response.json() as Envelope<T>;
    } catch {
      throw new DataCenterError(`Request failed (${response.status})`, response.status);
    }
    const allowed = options.allowStatuses?.includes(response.status) ?? false;
    if (!response.ok && !allowed) {
      const first = payload.errors?.[0];
      throw new DataCenterError(
        first?.message || `Request failed (${response.status})`,
        response.status,
        payload.meta?.request_id,
        first?.code,
      );
    }
    return { data: payload.data, meta: payload.meta };
  };

  return {
    auth: {
      login: (username: string, password: string) => request<{ username: string }>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }, { skipApiKey: true }),
      logout: () => request<{ logged_out: boolean }>("/auth/logout", { method: "POST" }, { skipApiKey: true }),
      me: () => request<{ username: string; expires_at: number }>("/auth/me", {}, { skipApiKey: true }),
      status: () => request<{ initialized: boolean; username: string }>("/auth/status", {}, { skipApiKey: true }),
      initialize: (username: string, password: string) => request<{ initialized: boolean; username: string }>("/auth/initialize", { method: "POST", body: JSON.stringify({ username, password }) }),
      changePassword: (currentPassword: string, newPassword: string) => request<{ changed: boolean }>("/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) }, { skipApiKey: true }),
    },
    // Readiness intentionally accepts HTTP 503: the API returns structured degraded state
    // so the console can keep reads visible while protecting writes when necessary.
    ready: () => request<ReadyState>("/health/ready", {}, { allowStatuses: [503] }),
    metrics: () => request<Metrics>("/metrics"),
    datasets: () => request<Dataset[]>("/datasets"),
    runs: (status?: string) => request<Run[]>(`/runs${queryString({ status: status && status !== "all" ? status : undefined })}`),
    findings: () => request<Finding[]>("/quality/findings"),
    barsPage: (query: BarsQuery, cursor?: string | null, pageSize = 1000) => request<Bar[]>(`/bars${queryString({
      ...query,
      page_size: pageSize,
      cursor,
    })}`),
    economicPage: (query: EconomicQuery, cursor?: string | null, pageSize = 1000) => request<EconomicObservation[]>(`/economic/observations${queryString({
      ...query,
      page_size: pageSize,
      cursor,
    })}`),
    marketBarsPage: (query: MarketBarsQuery, cursor?: string | null, pageSize = 1000) => request<Bar[]>(`/market-bars${queryString({
      ...query,
      page_size: pageSize,
      cursor,
    })}`),
    coverage: (query: Omit<BarsQuery, "start" | "end">) => request<BarsCoverage>(`/provider-bars/coverage${queryString(query)}`),
    economicCoverage: (query: Pick<EconomicQuery, "provider" | "series_id">) => request<EconomicCoverage>(`/economic/coverage${queryString(query)}`),
    ingest: (job: IngestJob) => request<IngestReceipt>("/ingest/runs", { method: "POST", body: JSON.stringify(job) }),
    retry: (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}/retry`, { method: "POST" }),
    acknowledge: (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}/acknowledge`, { method: "POST" }),
    run: (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}`),
    runDetail: (runId: string) => request<RunDetail>(`/runs/${encodeURIComponent(runId)}/detail`),
    manifest: (runId: string) => request<Record<string, unknown>>(`/runs/${encodeURIComponent(runId)}/manifest`),

    // Every write goes through the unified maintenance contract, so a caller
    // never has to know which legacy endpoint a run kind used to use.
    runsPage: (filters: RunFilters, cursor?: string | null, pageSize = 50) => request<RunDetail[]>(
      `/runs${queryString({ ...filters, page_size: pageSize, cursor })}`,
    ),
    maintenancePlan: (task: MaintenanceTaskRequest) => request<TaskPreview>("/maintenance/plans", {
      method: "POST", body: JSON.stringify(task),
    }),
    submitMaintenance: (task: MaintenanceTaskRequest) => request<QueuedEnvelope>("/maintenance/tasks", {
      method: "POST", body: JSON.stringify(task),
    }),
    maintenanceTasks: () => request<MaintenanceTaskRecord[]>("/maintenance/tasks"),
    updateMaintenanceTask: (taskId: string, status: "paused" | "enabled") => request<MaintenanceTaskRecord>(`/maintenance/tasks/${encodeURIComponent(taskId)}`, { method: "PATCH", body: JSON.stringify({ status }) }),
    capabilities: () => request<Capabilities>("/capabilities"),
    findingsPage: (query: Record<string, string | number | null | undefined> = {}, cursor?: string | null) =>
      request<Finding[]>(`/quality/findings${queryString({ ...query, cursor })}`),
    findingState: (findingId: string, body: { state: FindingState; note?: string; dataset_id?: string; resolved_by_run_id?: string }) =>
      request<Record<string, unknown>>(`/quality/findings/${encodeURIComponent(findingId)}/state`, {
        method: "POST", body: JSON.stringify(body),
      }),
    marketBarsCoverage: (query: Record<string, string | null | undefined>) =>
      request<MarketBarsCoverage>(`/market-bars/coverage${queryString(query)}`),
    queue: () => request<QueueState>("/operations/queue"),
    audit: (limit = 50) => request<OperationAuditEntry[]>(`/operations/audit${queryString({ limit })}`),
    capacityHistory: (limit = 50) => request<CapacityHistory>(`/operations/capacity-history${queryString({ limit })}`),
    worker: () => request<WorkerActivity>("/operations/worker"),
    receipts: (limit = 5) => request<ReceiptHistory>(`/operations/receipts${queryString({ limit })}`),
  };
}
