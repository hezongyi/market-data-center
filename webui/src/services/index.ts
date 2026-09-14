/**
 * Domain services: the single seam between the console and `/api/v1`.
 *
 * Pages never build requests, unwrap envelopes or handle cursors themselves;
 * they consume these services through the query and mutation hooks.
 */
import {
  createDataCenterClient,
  type CapacityHistory,
  type ApiMeta,
  type ApiResult,
  type BarsQuery,
  type Capabilities,
  type CoverageReport,
  type EconomicQuery,
  type Finding,
  type FindingState,
  type MaintenanceTaskRequest,
  type MaintenanceTaskRecord,
  type MarketBarsCoverage,
  type MarketBarsQuery,
  type OperationAuditEntry,
  type PageInfo,
  type QueueState,
  type QueuedEnvelope,
  type RunDetail,
  type RunFilters,
  type TaskPreview,
} from "../lib/api";

// ``run_id`` is a findings-only filter: the findings endpoint accepts it while
// the runs endpoint does not, so it is added next to the other quality filters.
export type Filters = RunFilters & { observed_from?: string; observed_to?: string; severity?: string; code?: string; state?: FindingState; run_id?: string };

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
      // Derived rows page through the same cursor contract as raw bars; the
      // recipe and price basis stay part of the selector.
      marketBars: async (query: MarketBarsQuery, cursor?: string | null, pageSize = 100) =>
        wrap(await client.marketBarsPage(query, cursor, pageSize)),
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
      list: async (): Promise<MaintenanceTaskRecord[]> => (await client.maintenanceTasks()).data,
      updateStatus: async (taskId: string, status: "paused" | "enabled"): Promise<MaintenanceTaskRecord> => (await client.updateMaintenanceTask(taskId, status)).data,
      preview: async (task: MaintenanceTaskRequest): Promise<TaskPreview> => (await client.maintenancePlan(task)).data,
      submit: async (task: MaintenanceTaskRequest): Promise<QueuedEnvelope> =>
        (await client.submitMaintenance(task)).data,
      track: async (runId: string): Promise<RunDetail> => (await client.runDetail(runId)).data,
    },
    quality: {
      findings: async (filters: Filters = {}, cursor?: string | null, pageSize = 100): Promise<Paged<Finding>> =>
        wrap(await client.findingsPage({ ...filters, page_size: pageSize }, cursor)),
      setState: async (findingId: string, state: FindingState,
        body: { note?: string; dataset_id?: string; resolved_by_run_id?: string } = {}) =>
        (await client.findingState(findingId, { state, ...body })).data,
    },
    operations: {
      queue: async (): Promise<QueueState> => (await client.queue()).data,
      audit: async (limit = 50): Promise<OperationAuditEntry[]> => (await client.audit(limit)).data,
      capacityHistory: async (limit = 50): Promise<CapacityHistory> => (await client.capacityHistory(limit)).data,
      worker: async () => (await client.worker()).data,
      receipts: async (limit = 5) => (await client.receipts(limit)).data,
      readiness: async () => (await client.ready()).data,
      metrics: async () => (await client.metrics()).data,
      ingest: async (job: Parameters<typeof client.ingest>[0]) => (await client.ingest(job)).data,
    },
    // The plan registry and the scheduler are one workspace: a plan is only
    // meaningful next to whether the scheduler may dispatch it.
    production: {
      plans: async (query: Parameters<typeof client.productionPlans>[0] = {}) => (await client.productionPlans(query)).data,
      plan: async (taskId: string) => (await client.productionPlan(taskId)).data,
      create: async (body: Parameters<typeof client.createProductionTask>[0], idempotencyKey: string) =>
        (await client.createProductionTask(body, idempotencyKey)).data,
      preview: async (definition: Record<string, unknown>) => (await client.productionPreview(definition)).data,
      act: async (taskId: string, command: string, idempotencyKey: string,
                  options: Parameters<typeof client.productionPlanAction>[3] = {}) =>
        (await client.productionPlanAction(taskId, command, idempotencyKey, options)).data,
      executions: async (taskId: string, pageSize = 10, cursor?: string | null) => {
        const result = await client.productionExecutions(taskId, pageSize, cursor);
        return { items: result.data, nextCursor: result.meta.next_cursor ?? null };
      },
      steps: async (executionId: string, limit = 100) => (await client.productionExecutionSteps(executionId, limit)).data,
      retry: async (executionId: string, idempotencyKey: string) =>
        (await client.retryProductionExecution(executionId, idempotencyKey)).data,
      matrix: async () => (await client.catalogMatrix()).data,
      governance: async () => (await client.governanceUnits()).data,
      scheduler: async () => (await client.scheduler()).data,
      dispatch: async (command: "pause_dispatch" | "resume_dispatch") => (await client.schedulerAction(command)).data,
    },
  };
}

export type Services = ReturnType<typeof createServices>;
