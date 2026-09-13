import { CopyId } from "../components/ui";
import { useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import {
  AlertTriangle, CheckCircle2, ChevronLeft, ChevronRight, FileText, GitBranch, Layers,
  Lock, RefreshCw, ShieldAlert, XCircle,
} from "lucide-react";
import { ConfirmDialog, DataTable, DetailDrawer, EmptyState, ErrorState, FilterBar, LoadingSkeleton, PanelHeading, StatusBadge } from "../components/ui";
import { messageOf, permissionOf, useQuery } from "../hooks";
import { TimeDisplay, usePreferences } from "../preferences";
import type { RunDetail, RunFilters } from "../lib/api";
import type { Services } from "../services";

const statuses = ["all", "queued", "running", "pass", "failed", "dead_letter"];
const runKinds = ["all", "ingest", "backfill", "gap_repair", "derive", "quality", "parity"];
const runScopes = ["all", "production", "maintenance", "migration", "acceptance"];

const tone = (value: string) =>
  value === "pass" ? "good" as const
    : ["failed", "dead_letter"].includes(value) ? "bad" as const
      : ["degraded", "queued", "running"].includes(value) ? "warn" as const : "neutral" as const;

const utc = (value?: string | null) => <TimeDisplay value={value} />;
const dayToIso = (value: string, endOfDay = false) =>
  value ? new Date(`${value}T${endOfDay ? "23:59:59" : "00:00:00"}Z`).toISOString() : undefined;

export function RunsPage({ services, refreshToken, onMessage, onChanged }: {
  services: Services;
  refreshToken: number;
  onMessage: (message: string) => void;
  onChanged: () => void;
}) {
  const { t } = usePreferences();
  const [filters, setFilters] = useState<RunFilters>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState<{ action: "retry" | "acknowledge"; run: RunDetail } | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  const query = useQuery(
    () => services.runs.list(filters, cursor, 25),
    [services, JSON.stringify(filters), cursor, refreshToken],
  );
  const runs = query.data?.items ?? [];
  const page = query.data?.page ?? null;
  const detail = useQuery(
    () => selected ? services.runs.detail(selected) : Promise.resolve(null),
    [services, selected, refreshToken],
  );

  const setFilter = (key: keyof RunFilters, value: string) => {
    setCursors([]);
    setCursor(null);
    setPageIndex(0);
    setFilters(current => ({ ...current, [key]: value === "all" || value === "" ? undefined : value }));
  };

  const nextPage = () => {
    setCursors(current => [...current, cursor]);
    setCursor(page?.next_cursor ?? null);
    setPageIndex(index => index + 1);
  };
  const previousPage = () => {
    const stack = [...cursors];
    const previous = stack.pop() ?? null;
    setCursors(stack);
    setCursor(previous);
    setPageIndex(index => Math.max(0, index - 1));
  };

  const act = async () => {
    if (!pending) return;
    setBusy(true);
    setActionError("");
    try {
      if (pending.action === "retry") {
        const created = await services.runs.retry(pending.run.run_id);
        onMessage(`Queued retry ${created.run_id}`);
      } else {
        await services.runs.acknowledge(pending.run.run_id);
        onMessage(`Acknowledged ${pending.run.run_id}`);
      }
      setPending(null);
      query.reload();
      detail.reload();
      onChanged();
    } catch (reason) {
      const permission = permissionOf(reason);
      setActionError(permission === "unauthorized" ? `Not authorized: ${messageOf(reason)}` : messageOf(reason));
      onMessage(messageOf(reason));
    } finally {
      setBusy(false);
    }
  };

  const columns = useMemo<ColumnDef<RunDetail>[]>(() => [
    { accessorKey: "run_id", header: "Run ID", cell: info => <CopyId value={String(info.getValue())} /> },
    { accessorKey: "dataset_id", header: "Dataset" },
    { accessorKey: "run_kind", header: t("Kind"), cell: info => String(info.getValue() ?? "ingest") },
    { accessorKey: "run_scope", header: t("Scope"), cell: info => String(info.getValue() ?? "—") },
    { accessorKey: "outcome", header: t("Outcome"), cell: ({ row }) => <div className="status-cell">
      <StatusBadge tone={tone(row.original.outcome)}>{row.original.outcome}</StatusBadge>
      {row.original.degraded_reasons.length > 0 && <span className="reason-chip" title={row.original.degraded_reasons.map(item => item.message).join("; ")}>
        <AlertTriangle size={11} />{row.original.degraded_reasons.length}</span>}
    </div> },
    { accessorKey: "stage", header: "Stage", cell: info => <span className="stage-chip"><Layers size={11} />{String(info.getValue())}</span> },
    { accessorKey: "window_count", header: "Windows", cell: info => String(info.getValue() ?? 1) },
    { accessorKey: "manifest_status", header: "Manifest", cell: ({ row }) => row.original.manifest_status === "published"
      ? <span className="stage-chip"><FileText size={11} />published</span>
      : <span className="filter-note">{row.original.manifest_status.replace(/_/g, " ")}</span> },
    { accessorKey: "finding_count", header: "Findings", cell: info => String(info.getValue() ?? 0) },
    { accessorKey: "created_at", header: "Created", cell: info => utc(String(info.getValue() ?? "")) },
    { id: "actions", header: "Actions", enableSorting: false, cell: ({ row }) => <div className="row-actions">
      {["failed", "dead_letter"].includes(row.original.status) && <button onClick={event => { event.stopPropagation(); setPending({ action: "retry", run: row.original }); }}>Retry</button>}
      {row.original.status === "dead_letter" && row.original.dead_letter_state?.state !== "acknowledged" && <button onClick={event => { event.stopPropagation(); setPending({ action: "acknowledge", run: row.original }); }}>Acknowledge</button>}
    </div> },
  ], []);

  return <>
    <section className="panel" aria-label="Runs">
      <PanelHeading eyebrow="Live activity" title="Runs"
        action={<div className="header-actions"><StatusBadge tone={query.permission === "unauthorized" ? "bad" : "neutral"}>
          {page ? `${page.count} on this page` : "—"}</StatusBadge>
          <button className="link-button" onClick={query.reload}>Refresh →</button></div>} />
      <FilterBar>
        <label>Status<select aria-label="Status" value={filters.status ?? "all"} onChange={event => setFilter("status", event.target.value)}>
          {statuses.map(value => <option key={value}>{value}</option>)}</select></label>
        <label>Run kind<select aria-label="Run kind filter" value={filters.run_kind ?? "all"} onChange={event => setFilter("run_kind", event.target.value)}>
          {runKinds.map(value => <option key={value}>{value}</option>)}</select></label>
        <label>Scope<select aria-label="Run scope filter" value={filters.run_scope ?? "all"} onChange={event => setFilter("run_scope", event.target.value)}>
          {runScopes.map(value => <option key={value}>{value}</option>)}</select></label>
        <label>Dataset<input aria-label="Dataset filter" value={filters.dataset_id ?? ""} placeholder="provider_bars"
          onChange={event => setFilter("dataset_id", event.target.value)} /></label>
        <label>From<input aria-label="Runs created from" type="date" onChange={event => setFilter("created_from", dayToIso(event.target.value) ?? "")} /></label>
        <label>Through<input aria-label="Runs created through" type="date" onChange={event => setFilter("created_to", dayToIso(event.target.value, true) ?? "")} /></label>
      </FilterBar>

      {actionError && <div className="error-state" role="alert"><AlertTriangle size={18} /><div><b>Action rejected</b><p>{actionError}</p></div></div>}
      {query.status === "loading" && <LoadingSkeleton rows={5} />}
      {query.status === "error" && (query.permission === "unauthorized"
        ? <div className="locked-state" role="status"><Lock size={18} /><div><b>Not authorized</b><p>{query.error}</p></div></div>
        : <ErrorState message={query.error ?? "Unable to load runs"} onRetry={query.reload} />)}
      {query.status === "empty" && <EmptyState title="No matching runs." detail="Adjust the filters or queue work from the Maintenance workspace." />}
      {query.status === "success" && <DataTable data={runs} columns={columns} empty="No matching runs." onRowClick={row => setSelected(row.run_id)} />}

      <div className="pager">
        <button className="secondary-button" aria-label="Previous page" onClick={previousPage} disabled={!cursors.length}>
          <ChevronLeft size={14} /> Previous</button>
        <span>Page {pageIndex + 1}{page ? ` · ${page.count} run(s)` : ""}</span>
        <button className="secondary-button" aria-label="Next page" onClick={nextPage} disabled={!page?.has_more}>
          <ChevronRight size={14} /> Next page</button>
        <span className="filter-note">Cursor paging is bound to these filters; changing a filter restarts at page 1.</span>
      </div>
    </section>

    {selected && <DetailDrawer title={selected} onClose={() => setSelected(null)}>
      {detail.status === "loading" && <LoadingSkeleton rows={4} />}
      {detail.status === "error" && <ErrorState message={detail.error ?? "Unable to load the run detail"} onRetry={detail.reload} />}
      {detail.data && <RunDetailBody run={detail.data} services={services} />}
    </DetailDrawer>}

    {pending && <ConfirmDialog
      title={pending.action === "retry" ? "Retry failed run?" : "Acknowledge dead letter?"}
      detail={pending.action === "retry"
        ? "A new run will be queued. The original terminal run remains immutable."
        : "This records an additive acknowledgement and does not modify the terminal run."}
      confirmLabel={pending.action === "retry" ? "Queue retry" : "Acknowledge"} busy={busy}
      onConfirm={() => void act()} onCancel={() => setPending(null)} />}
  </>;
}

function RunDetailBody({ run, services }: { run: RunDetail; services: Services }) {
  const [manifest, setManifest] = useState<Record<string, unknown> | null>(null);
  const [manifestError, setManifestError] = useState("");
  const loadManifest = async () => {
    setManifestError("");
    try {
      setManifest(await services.runs.manifest(run.run_id));
    } catch (reason) {
      setManifestError(messageOf(reason));
    }
  };
  return <>
    <div className="detail-status">
      <StatusBadge tone={tone(run.outcome)}>{run.outcome}</StatusBadge>
      <span className="stage-chip"><Layers size={12} /> stage {run.stage}</span>
      {run.terminal && <span className="stage-chip"><CheckCircle2 size={12} /> terminal</span>}
    </div>

    {run.degraded_reasons.length > 0 && <ul className="issue-list" aria-label="Degraded reasons">
      {run.degraded_reasons.map(reason => <li key={`${reason.code}-${reason.message}`} className="issue warning">
        <ShieldAlert size={14} /><b>{reason.source}</b><span>{reason.message}</span><code>{reason.code}</code></li>)}
    </ul>}

    <dl className="detail-list">
      <div><dt>Dataset</dt><dd>{run.dataset_id}</dd></div>
      <div><dt>Run kind</dt><dd>{run.run_kind ?? "ingest"}</dd></div>
      <div><dt>Run scope</dt><dd>{run.run_scope ?? "—"}</dd></div>
      <div><dt>Selector</dt><dd className="mono">{Object.entries(run.selector).map(([key, value]) => `${key}=${value}`).join(" ") || "—"}</dd></div>
      <div><dt>Time range</dt><dd className="mono">{run.time_range ? <><TimeDisplay value={run.time_range.start} /> → <TimeDisplay value={run.time_range.end} /> ({run.time_range.semantics})</> : "—"}</dd></div>
      <div><dt>Windows</dt><dd>{run.window_count}</dd></div>
      <div><dt>Input snapshot</dt><dd>{run.input_snapshot_id ? <CopyId value={run.input_snapshot_id} /> : "—"}</dd></div>
      <div><dt>Schema version</dt><dd className="mono">{run.schema_version ?? "—"}</dd></div>
      <div><dt>Rows</dt><dd>{run.row_count ?? "—"}</dd></div>
      <div><dt>Extent</dt><dd className="mono"><TimeDisplay value={run.min_ts ?? run.min_date} /> → <TimeDisplay value={run.max_ts ?? run.max_date} /></dd></div>
      <div><dt>Manifest</dt><dd>{run.manifest_status}</dd></div>
      <div><dt>Output hash</dt><dd>{run.output_hash ? <CopyId value={run.output_hash} /> : "—"}</dd></div>
      <div><dt>Findings</dt><dd>{run.finding_count}</dd></div>
      <div><dt>Attempts</dt><dd>{run.attempt_count ?? 0} · retries {run.retry_count ?? 0}</dd></div>
      <div><dt>Created</dt><dd>{utc(run.created_at)}</dd></div>
      <div><dt>Finished</dt><dd>{utc(run.finished_at)}</dd></div>
      {run.error && <div><dt>Error</dt><dd>{run.error}</dd></div>}
      {run.dead_letter_state && <div><dt>Dead letter</dt><dd>{run.dead_letter_state.state ?? "—"}</dd></div>}
    </dl>

    {(run.windows?.length ?? 0) > 0 && <>
      <h3 className="detail-heading"><Layers size={14} /> Windows</h3>
      <ol className="retry-chain">{run.windows?.map(window => <li key={window.ordinal}>
        <span className="stage-chip">#{window.ordinal} {window.reason ?? "window"}</span>
        <span className="mono"><TimeDisplay value={window.start} /> → <TimeDisplay value={window.end} /></span>
        <span className="filter-note">{window.row_count ?? 0} row(s)</span>
      </li>)}</ol>
    </>}

    <h3 className="detail-heading"><GitBranch size={14} /> Retry chain</h3>
    <ol className="retry-chain">{run.retry_chain.map(link => <li key={link.run_id}>
      <CopyId value={link.run_id} />
      <StatusBadge tone={tone(link.status)}>{link.status}</StatusBadge>
      <span className="filter-note">{link.relation} · {link.stage} · {utc(link.created_at)}</span>
    </li>)}</ol>

    {(run.attempt_errors?.length ?? 0) > 0 && <>
      <h3 className="detail-heading">Attempt errors</h3>
      <ul className="issue-list">{run.attempt_errors?.map((error, index) => <li key={index} className="issue error">
        <XCircle size={14} /><b>{error.failure_stage}</b><span>{error.error}</span><code>{error.error_type}</code></li>)}</ul>
    </>}

    {(run.findings?.length ?? 0) > 0 && <>
      <h3 className="detail-heading">Findings</h3>
      <ul className="issue-list">{run.findings?.map(finding => <li key={finding.finding_id} className="issue info">
        <AlertTriangle size={14} /><b>{finding.severity}</b><span>{finding.message ?? finding.code}</span>
        {finding.finding_id && <CopyId value={finding.finding_id} />}</li>)}</ul>
    </>}

    <h3 className="detail-heading"><FileText size={14} /> Manifest</h3>
    {run.manifest_status === "published"
      ? <><button className="secondary-button" onClick={() => void loadManifest()}>Load manifest</button>
        {manifest && <pre className="manifest-view">{JSON.stringify(manifest, null, 2)}</pre>}</>
      : <p className="filter-note">{run.manifest_status === "not_applicable"
        ? "Verification runs record findings and publish no canonical manifest."
        : "No published manifest is available for this run."}</p>}
    {manifestError && <p className="inline-warning"><AlertTriangle size={14} /> {manifestError}</p>}
    <p className="filter-note"><RefreshCw size={12} /> Terminal receipts cannot be rewritten by this console.</p>
  </>;
}
