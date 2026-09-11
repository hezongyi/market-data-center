export type ApiMeta = { request_id?: string; schema_version?: string; next_cursor?: string | null; snapshot_id?: string; schema_versions?: string[]; count?: number; warnings?: string[]; [key: string]: unknown };
export type ApiResult<T> = { data: T; meta: ApiMeta };
type Envelope<T> = { data: T; meta: ApiMeta; errors: Array<{ message: string; code?: string }> };
export type Dataset = { dataset_id: string; schema_version: string; description: string; partitioning: string[] };
export type Run = { run_id: string; dataset_id: string; status: string; job_id?: string; provider?: string; symbol?: string; row_count?: number; created_at?: string; started_at?: string; finished_at?: string; error_type?: string; retry_count?: number; retry_of?: string; error?: string; run_scope?: string; dead_letter_state?: { state?: string; acknowledged_at?: string; resolved_by_run_id?: string } };
export type Finding = { severity: string; code: string; dataset_id?: string; run_id?: string; job_id?: string; series_id?: string; observation_date?: string; bar_ts?: string; message?: string };
export type Bar = { bar_ts: string; open: number; high: number; low: number; close: number; volume?: number };
export type EconomicObservation = { observation_date: string; value: number | null; release_ts?: string; asof_ts?: string; frequency?: string; units?: string; missing_reason?: string };
export type ReadyState = { status: string; read_status: string; write_status: string; capacity_status: string; operational_snapshot_status: string; worker_heartbeat_age_seconds: number | null; deployment_id: string; software_version: string; source_commit: string };
export type CapacityMetrics = { status: string; total_bytes: number; used_bytes: number; free_bytes: number; free_ratio: number; warning_free_ratio: number; critical_free_ratio: number };
export type SliWindow = { runs: number; passed: number; success_rate: number | null };
export type Metrics = { runs_total: number; runs_by_status: Record<string, number>; retry_attempts_total: number; timeouts_total: number; queue_depth: number; queue_oldest_age_seconds: number; dead_letter_total: number; dead_letter_by_state?: Record<string, number>; success_rate: number | null; production_sli?: Record<"1h" | "24h" | "7d", SliWindow>; worker_heartbeat_age_seconds: number | null; capacity: CapacityMetrics | null; last_successful_backup_at: string | null; last_successful_recovery_drill_at: string | null; temporary_backup_count: number; operational_snapshot_status: string; deployment_id: string; software_version: string; source_commit: string; query?: Record<string, unknown> };

export class DataCenterError extends Error {
  readonly status: number; readonly requestId?: string; readonly code?: string;
  constructor(message: string, status: number, requestId?: string, code?: string) { super(message); this.name = "DataCenterError"; this.status = status; this.requestId = requestId; this.code = code; }
}

export function createDataCenterClient(apiKey: string) {
  const request = async <T,>(path: string, init: RequestInit = {}): Promise<ApiResult<T>> => {
    const headers = new Headers(init.headers); headers.set("Content-Type", "application/json"); if (apiKey) headers.set("X-API-Key", apiKey);
    const response = await fetch(`/api/v1${path}`, { ...init, headers });
    let payload: Envelope<T>;
    try { payload = await response.json() as Envelope<T>; } catch { throw new DataCenterError(`Request failed (${response.status})`, response.status); }
    if (!response.ok) { const first = payload.errors?.[0]; throw new DataCenterError(first?.message || `Request failed (${response.status})`, response.status, payload.meta?.request_id, first?.code); }
    return { data: payload.data, meta: payload.meta };
  };
  return {
    ready: () => request<ReadyState>("/health/ready"), metrics: () => request<Metrics>("/metrics"), datasets: () => request<Dataset[]>("/datasets"),
    runs: (status?: string) => request<Run[]>(`/runs${status && status !== "all" ? `?status=${encodeURIComponent(status)}` : ""}`), findings: () => request<Finding[]>("/quality/findings"),
    barsPage: (query: string, cursor?: string | null, pageSize = 2) => request<Bar[]>(`/bars?${query}&page_size=${pageSize}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`),
    economicPage: (query: string, cursor?: string | null, pageSize = 100) => request<EconomicObservation[]>(`/economic/observations?${query}&page_size=${pageSize}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`),
    coverage: (query: string) => request<Record<string, unknown>>(`/provider-bars/coverage?${query}`), ingest: (job: Record<string, unknown>) => request<Run>("/ingest/runs", { method: "POST", body: JSON.stringify(job) }),
    retry: (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}/retry`, { method: "POST" }), acknowledge: (runId: string) => request<Record<string, unknown>>(`/runs/${encodeURIComponent(runId)}/acknowledge`, { method: "POST" }),
  };
}
