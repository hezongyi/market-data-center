export type ApiErrorDetail = { message: string; code?: string };
export type Envelope<T> = {
  data: T;
  meta: { request_id?: string; next_cursor?: string | null; [key: string]: unknown };
  errors: ApiErrorDetail[];
};

export type Dataset = { dataset_id: string; schema_version: string; description: string; partitioning: string[] };
export type Run = {
  run_id: string;
  dataset_id: string;
  status: string;
  job_id?: string;
  provider?: string;
  symbol?: string;
  row_count?: number;
  created_at?: string;
  completed_at?: string;
  error_type?: string;
  retry_count?: number;
  retry_of?: string;
  error?: string;
};
export type Finding = { severity: string; code: string; dataset_id?: string; observation_date?: string; bar_ts?: string };
export type Bar = { bar_ts: string; open: number; high: number; low: number; close: number };
export type ReadyState = {
  status: string;
  read_status: string;
  write_status: string;
  capacity_status: string;
  operational_snapshot_status: string;
  worker_heartbeat_age_seconds: number | null;
  deployment_id: string;
  software_version: string;
  source_commit: string;
};

export class DataCenterError extends Error {
  readonly status: number;
  readonly requestId?: string;

  constructor(message: string, status: number, requestId?: string) {
    super(message);
    this.name = "DataCenterError";
    this.status = status;
    this.requestId = requestId;
  }
}

export function createDataCenterClient(apiKey: string) {
  const request = async <T,>(path: string, init: RequestInit = {}): Promise<{ data: T; meta: Envelope<T>["meta"] }> => {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (apiKey) headers.set("X-API-Key", apiKey);
    const response = await fetch(`/api/v1${path}`, { ...init, headers });
    const payload = await response.json() as Envelope<T>;
    if (!response.ok) {
      const message = payload.errors?.map((error) => error.message).join(", ") || `Request failed (${response.status})`;
      throw new DataCenterError(message, response.status, payload.meta?.request_id);
    }
    return { data: payload.data, meta: payload.meta };
  };

  const list = async <T,>(path: string): Promise<T[]> => {
    const rows: T[] = [];
    let cursor: string | null = null;
    do {
      const separator = path.includes("?") ? "&" : "?";
      const cursorQuery: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const result: { data: T[]; meta: Envelope<T[]>["meta"] } = await request<T[]>(`${path}${separator}page_size=1000${cursorQuery}`);
      rows.push(...result.data);
      cursor = typeof result.meta.next_cursor === "string" ? result.meta.next_cursor : null;
    } while (cursor);
    return rows;
  };

  return {
    ready: () => request<ReadyState>("/health/ready"),
    datasets: () => request<Dataset[]>("/datasets"),
    runs: () => request<Run[]>("/runs"),
    findings: () => request<Finding[]>("/quality/findings"),
    bars: (query: string) => list<Bar>(`/bars?${query}`),
    coverage: (query: string) => request<Record<string, unknown>>(`/provider-bars/coverage?${query}`),
    ingest: (job: Record<string, unknown>) => request<Run>("/ingest/runs", { method: "POST", body: JSON.stringify(job) }),
    retry: (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}/retry`, { method: "POST" }),
  };
}

export type DataCenterClient = ReturnType<typeof createDataCenterClient>;
