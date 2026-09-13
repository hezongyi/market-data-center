/**
 * Domain services: the single seam between the console and `/api/v1`.
 *
 * Pages never build requests, unwrap envelopes or handle cursors themselves;
 * they consume these services through the query and mutation hooks.
 */
import {
  createDataCenterClient,
  type ApiMeta,
  type ApiResult,
  type BarsQuery,
  type Capabilities,
  type CoverageReport,
  type EconomicQuery,
  type Finding,
  type FindingState,
  type MaintenanceTaskRequest,
  type MarketBarsCoverage,
  type OperationAuditEntry,
  type PageInfo,
  type QueueState,
  type QueuedEnvelope,
  type RunDetail,
  type RunFilters,
  type TaskPreview,
} from "../lib/api";

export type Filters = RunFilters & { observed_from?: string; observed_to?: string; severity?: string; code?: string; state?: FindingState };

export type Paged<T> = { items: T[]; meta: ApiMeta; page: PageInfo | null; warnings: string[] };

const pageOf = (meta: ApiMeta): PageInfo | null => (meta.page as PageInfo | undefined) ?? null;
const warningsOf = (meta: ApiMeta): string[] => (meta.warnings as string[] | undefined) ?? [];

export function createServices(apiKey: string) {
  const client = createDataCenterClient(apiKey);

  const wrap = <T,>(result: ApiResult<T[]>): Paged<T> => ({
    items: result.data ?? [],
    meta: result.meta,
    page: pageOf(result.meta),
    warnings: warningsOf(result.meta),
  });

  return {
    catalog: {
      datasets: async () => (await client.datasets()).data,
      capabilities: async (): Promise<Capabilities> => (await client.capabilities()).data,
      providerCoverage: async (query: Omit<BarsQuery, "start" | "end"> & { start?: string; end?: string }) =>
        (await client.coverage(query)).data as CoverageReport,
      marketCoverage: async (query: Record<string, string | null | undefined>): Promise<MarketBarsCoverage> =>
        (await client.marketBarsCoverage(query)).data,
      economicCoverage: async (query: { provider: string; series_id: string }) =>
        (await client.economicCoverage(query)).data,
    },
    query: {
      bars: async (query: BarsQuery, cursor?: string | null, pageSize = 100) =>
        wrap(await client.barsPage(query, cursor, pageSize)),
      economic: async (query: EconomicQuery, cursor?: string | null, pageSize = 100) =>
        wrap(await client.economicPage(query, cursor, pageSize)),
    },
    runs: {
      list: async (filters: Filters, cursor?: string | null, pageSize = 50): Promise<Paged<RunDetail>> =>
        wrap(await client.runsPage(filters, cursor, pageSize)),
      detail: async (runId: string): Promise<RunDetail> => (await client.runDetail(runId)).data,
      manifest: async (runId: string) => (await client.manifest(runId)).data,
      retry: async (runId: string) => (await client.retry(runId)).data,
      acknowledge: async (runId: string) => (await client.acknowledge(runId)).data,
      // A run is "settled" once the API marks it terminal; only then is the
      // displayed state final and safe to cache.
      isTerminal: (run: { status?: string; terminal?: boolean }) =>
        run.terminal === true || ["pass", "failed", "dead_letter"].includes(run.status ?? ""),
    },
    maintenance: {
      preview: async (task: MaintenanceTaskRequest): Promise<TaskPreview> => (await client.maintenancePlan(task)).data,
      submit: async (task: MaintenanceTaskRequest): Promise<QueuedEnvelope> =>
        (await client.submitMaintenance(task)).data,
      track: async (runId: string): Promise<RunDetail> => (await client.runDetail(runId)).data,
    },
    quality: {
      findings: async (filters: Filters = {}, cursor?: string | null, pageSize = 100): Promise<Paged<Finding>> =>
        wrap(await client.findingsPage({ ...filters, page_size: pageSize }, cursor)),
      setState: async (findingId: string, state: FindingState, body: { note?: string; dataset_id?: string } = {}) =>
        (await client.findingState(findingId, { state, ...body })).data,
    },
    operations: {
      queue: async (): Promise<QueueState> => (await client.queue()).data,
      audit: async (limit = 50): Promise<OperationAuditEntry[]> => (await client.audit(limit)).data,
      capacityHistory: async () => (await client.capacityHistory()).data,
      readiness: async () => (await client.ready()).data,
      metrics: async () => (await client.metrics()).data,
      ingest: async (job: Parameters<typeof client.ingest>[0]) => (await client.ingest(job)).data,
    },
  };
}

export type Services = ReturnType<typeof createServices>;
