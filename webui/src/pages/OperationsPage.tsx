import { TimeDisplay, usePreferences } from "../preferences";
import { type FormEvent, type ReactNode, useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import {
  Activity, AlertTriangle, CheckCircle2, Clock, HardDrive, HeartPulse, History, Layers, Lock,
  Package, Pin, RefreshCw, ScrollText, ShieldAlert, ShieldCheck, Timer, XCircle,
} from "lucide-react";
import {
  ConfirmDialog, CopyId, DataTable, EmptyState, ErrorState, LoadingSkeleton, MetricCard, PanelHeading, StatusBadge,
} from "../components/ui";
import { useQuery, type QueryResult } from "../hooks";
import type {
  CapacityEvent, IngestJob, Metrics, OperationAuditEntry, OperationsReceipt, ReadyState, RunScope,
  WorkerActivity,
} from "../lib/api";
import type { Services } from "../services";
import "./OperationsPage.css";

type Tone = "good" | "warn" | "bad" | "neutral";

const bytes = (value?: number | null) => value == null ? "—" : `${(value / 1024 ** 3).toFixed(1)} GiB`;
const percent = (value?: number | null) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const utc = (value?: string | null) => <TimeDisplay value={value} />;
const selectorText = (selector: Record<string, string> | null | undefined) =>
  Object.entries(selector ?? {}).map(([key, value]) => `${key}=${value}`).join(" ") || "—";
const fieldsText = (fields: Record<string, unknown> | null | undefined) => {
  const entries = Object.entries(fields ?? {});
  return entries.length
    ? entries.map(([key, value]) => `${key}=${typeof value === "object" && value !== null ? JSON.stringify(value) : String(value)}`).join(" · ")
    : "No extra fields recorded";
};

// Status is always text + icon, so a colour-blind or monochrome reader still
// gets the same meaning from the operations panels.
const capacityTone = (status?: string): Tone =>
  status === "ok" ? "good" : status === "warning" ? "warn" : status === "critical" ? "bad" : "neutral";
const capacityIcon = (status?: string) =>
  status === "ok" ? <CheckCircle2 size={13} /> : status === "warning" ? <AlertTriangle size={13} />
    : status === "critical" ? <XCircle size={13} /> : <Clock size={13} />;
const heartbeatTone = (status: WorkerActivity["heartbeat_status"]): Tone =>
  status === "fresh" ? "good" : status === "stale" ? "bad" : "neutral";
const heartbeatIcon = (status: WorkerActivity["heartbeat_status"]) =>
  status === "fresh" ? <CheckCircle2 size={13} /> : status === "stale" ? <AlertTriangle size={13} /> : <Clock size={13} />;
const outcomeTone = (outcome: string): Tone =>
  outcome === "pass" ? "good"
    : ["rejected", "failed", "dead_letter", "error", "unauthorized"].includes(outcome) ? "bad"
      : ["queued", "running", "protected", "degraded"].includes(outcome) ? "warn" : "neutral";
const outcomeIcon = (outcome: string) =>
  outcome === "pass" ? <CheckCircle2 size={13} />
    : outcome === "protected" ? <ShieldAlert size={13} />
      : ["rejected", "failed", "dead_letter", "error", "unauthorized"].includes(outcome) ? <XCircle size={13} />
        : <Clock size={13} />;
const resultTone = (result: string): Tone =>
  result === "pass" ? "good" : ["fail", "failed", "error"].includes(result) ? "bad" : "warn";
const eventTone = (event: string): Tone =>
  event === "capacity_recovered" || event === "capacity_ok" ? "good"
    : event === "capacity_critical" ? "bad" : event === "capacity_warning" ? "warn" : "neutral";

const scopes: RunScope[] = ["production", "acceptance", "migration", "maintenance"];
const timeframes = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const assetClasses = ["crypto", "fx", "commodity"];
// The console renders exactly the actions the API reports, so a record is
// never shown as missing merely because the browser guessed a different name,
// and an action the platform never writes cannot appear as a phantom gap.
const receiptActionsOf = (latest: Record<string, OperationsReceipt | null>): string[] => {
  const reported = Object.keys(latest);
  return reported.length ? reported : ["backup", "restore", "recovery_drill"];
};

type OperationsPageProps = {
  apiKey: string;
  health: ReadyState | null;
  metrics: Metrics | null;
  onChanged: () => void;
  onMessage: (message: string) => void;
  services: Services;
};

export function OperationsPage({ apiKey, health, metrics, onChanged, onMessage, services }: OperationsPageProps) {
  const { t } = usePreferences();
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("UI_TEST");
  const [assetClass, setAssetClass] = useState("crypto");
  const [timeframe, setTimeframe] = useState("1d");
  const [runScope, setRunScope] = useState<RunScope>("production");
  const [start, setStart] = useState("2026-01-01");
  const [end, setEnd] = useState("2026-01-03");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [validationError, setValidationError] = useState("");
  // Bumped after a write so every operations read model is re-fetched from the
  // API instead of being patched locally.
  const [refreshToken, setRefreshToken] = useState(0);

  const queue = useQuery(() => services.operations.queue(), [services, apiKey, refreshToken]);
  const worker = useQuery(() => services.operations.worker(), [services, apiKey, refreshToken]);
  const history = useQuery(() => services.operations.capacityHistory(), [services, apiKey, refreshToken]);
  const receipts = useQuery(() => services.operations.receipts(), [services, apiKey, refreshToken]);
  const audit = useQuery(() => services.operations.audit(50), [services, apiKey, refreshToken]);

  const capacity = metrics?.capacity;
  const alerts = [
    capacity && capacity.status !== "ok"
      ? { title: `Capacity ${capacity.status}`, detail: `${(capacity.free_ratio * 100).toFixed(1)}% free`, tone: capacity.status === "critical" ? "bad" as const : "warn" as const }
      : null,
    (metrics?.dead_letter_by_state?.active ?? 0) > 0
      ? { title: "Active dead letters", detail: `${metrics?.dead_letter_by_state?.active} require review`, tone: "bad" as const }
      : null,
    metrics?.operational_snapshot_status && metrics.operational_snapshot_status !== "fresh"
      ? { title: "Operational snapshot", detail: metrics.operational_snapshot_status, tone: "warn" as const }
      : null,
  ].filter(Boolean) as Array<{ title: string; detail: string; tone: "warn" | "bad" }>;

  const entries = audit.data ?? [];
  const events = history.data?.events ?? [];
  const latest = receipts.data?.latest ?? {};
  const runningJobs = worker.data?.running_jobs ?? [];
  const recentReceipts = receipts.data?.receipts ?? [];

  const auditColumns = useMemo<ColumnDef<OperationAuditEntry>[]>(() => [
    { accessorKey: "at", header: t("Time"), cell: info => <span className="mono">{utc(String(info.getValue()))}</span> },
    { accessorKey: "action", header: t("Action") },
    { accessorKey: "actor", header: "Actor", cell: info => <span className="mono" title="Non-reversible actor fingerprint; never a credential.">{String(info.getValue() ?? "—")}</span> },
    { accessorKey: "task_id", header: t("Task"), cell: info => info.getValue() ? <CopyId value={String(info.getValue())} /> : "—" },
    { accessorKey: "run_ids", header: t("Runs"), cell: ({ row }) => row.original.run_ids.length ? <>{row.original.run_ids.map(id => <CopyId key={id} value={id} />)}</> : "—" },
    { accessorKey: "run_kind", header: t("Kind"), cell: info => String(info.getValue() ?? "—") },
    { accessorKey: "run_scope", header: t("Scope"), cell: info => String(info.getValue() ?? "—") },
    { accessorKey: "dataset_id", header: t("Dataset"), cell: info => String(info.getValue() ?? "—") },
    { id: "selector", header: "Selector", cell: ({ row }) => <span className="mono">{selectorText(row.original.selector)}
      {row.original.time_range?.start && <small><TimeDisplay value={row.original.time_range.start} /> → <TimeDisplay value={row.original.time_range.end} /></small>}</span> },
    { accessorKey: "outcome", header: t("Outcome"), cell: ({ row }) => <div className="status-cell">
      <StatusBadge tone={outcomeTone(row.original.outcome)}>{outcomeIcon(row.original.outcome)}{row.original.outcome}</StatusBadge>
      {row.original.code && <span className="stage-chip">{row.original.code}</span>}
    </div> },
    { accessorKey: "message", header: t("Message"), cell: info => String(info.getValue() ?? "—") },
  ], []);

  const eventColumns = useMemo<ColumnDef<CapacityEvent>[]>(() => [
    { accessorKey: "event", header: t("Event"), cell: info => <StatusBadge tone={eventTone(String(info.getValue()))}>{String(info.getValue())}</StatusBadge> },
    { accessorKey: "created_at", header: t("Recorded"), cell: info => <span className="mono">{utc(info.getValue() as string | null)}</span> },
    { accessorKey: "free_ratio", header: t("Free ratio"), cell: info => percent(info.getValue() as number | null) },
    { accessorKey: "status", header: t("Status"), cell: info => String(info.getValue() ?? "—") },
    { id: "thresholds", header: "Thresholds", cell: ({ row }) => `warning ${percent(row.original.warning_free_ratio)} · critical ${percent(row.original.critical_free_ratio)}` },
  ], []);

  const writesProtected = health?.write_status !== "available";
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setValidationError("");
    if (!provider.trim() || !symbol.trim()) {
      setValidationError("Provider and symbol are required.");
      return;
    }
    if (start && end && start > end) {
      setValidationError("Start date must not be after end date.");
      return;
    }
    setConfirm(true);
  };

  const ingest = async () => {
    setBusy(true);
    try {
      const job: IngestJob = {
        job_id: `webui-${Date.now()}`,
        dataset_id: "provider_bars",
        run_scope: runScope,
        provider: provider.trim(),
        symbol: symbol.trim(),
        asset_class: assetClass,
        timeframe,
        start: `${start}T00:00:00Z`,
        end: `${end}T00:00:00Z`,
      };
      const result = await services.operations.ingest(job);
      onMessage(`Queued ${runScope} run ${result.run_id}`);
      setConfirm(false);
      setRefreshToken(value => value + 1);
      onChanged();
    } catch (reason) {
      onMessage(reason instanceof Error ? reason.message : "Unable to queue ingest");
    } finally {
      setBusy(false);
    }
  };

  return <div className="operations-page">
    <section className="metrics operations-metrics">
      <MetricCard label="Capacity free" value={capacity ? `${(capacity.free_ratio * 100).toFixed(1)}%` : "—"} note={`${bytes(capacity?.free_bytes)} free`} tone={capacity?.status === "critical" ? "bad" : capacity?.status === "warning" ? "warn" : undefined} />
      <MetricCard label="Queue depth" value={String(metrics?.queue_depth ?? 0)} note={`oldest ${Math.round(metrics?.queue_oldest_age_seconds ?? 0)}s`} />
      <MetricCard label="Dead letters" value={String(metrics?.dead_letter_total ?? 0)} note={`${metrics?.retry_attempts_total ?? 0} retry attempts`} />
      <MetricCard label="Temporary backups" value={String(metrics?.temporary_backup_count ?? 0)} note={`snapshot ${metrics?.operational_snapshot_status ?? "unknown"}`} />
    </section>

    <div className="operations-grid">
      <section className="panel" aria-label={t("Maintenance queue")}>
        <PanelHeading eyebrow={t("Ledger queue")} title={t("Maintenance queue")} action={<div className="header-actions">
          {queue.data && <StatusBadge tone={queue.data.queued ? "warn" : "good"}>
            {queue.data.queued ? `${queue.data.queued} queued` : "Queue empty"}</StatusBadge>}
          <button className="link-button" aria-label="Refresh maintenance queue" onClick={queue.reload}><RefreshCw size={12} /> Refresh →</button>
        </div>} />
        <PanelQueryState query={queue} emptyTitle="No queue state recorded">
          {queue.data && <>
            <div className="operations-counts">
              <OperationsCount icon={<Clock size={13} />} label={t("Queued")} value={queue.data.queued} hint={t("waiting for the worker")} />
              <OperationsCount icon={<Activity size={13} />} label={t("Running")} value={queue.data.running} hint={t("in-flight jobs")} />
              <OperationsCount icon={<CheckCircle2 size={13} />} label={t("Completed")} value={queue.data.completed} hint={t("finished jobs")} />
            </div>
            <dl className="detail-list">
              <div><dt>Oldest queued (UTC)</dt><dd>{queue.data.oldest_queued_available_at
                ? <span className="mono">{utc(queue.data.oldest_queued_available_at)}</span>
                : "No queued job waiting"}</dd></div>
              <div><dt>Job states</dt><dd className="mono">{Object.entries(queue.data.by_status ?? {}).map(([status, count]) => `${status}=${count}`).join(" ") || "—"}</dd></div>
            </dl>
            <h3 className="detail-heading"><Layers size={14} /> Runs by status</h3>
            {Object.keys(queue.data.runs_by_status ?? {}).length
              ? <div className="chip-row">{Object.entries(queue.data.runs_by_status).map(([status, count]) =>
                <span className="stage-chip" key={status}>{status} · {count}</span>)}</div>
              : <EmptyState title={t("No runs recorded yet")} detail="The ledger counts a run here once a maintenance task is queued." />}
            <p className="filter-note">Counts are read from the run ledger and job queue; this console never estimates queue depth.</p>
          </>}
        </PanelQueryState>
      </section>

      <section className="panel" aria-label={t("Worker activity")}>
        <PanelHeading eyebrow={t("Liveness")} title={t("Worker activity")} action={<div className="header-actions">
          {worker.data && <StatusBadge tone={heartbeatTone(worker.data.heartbeat_status)}>
            {heartbeatIcon(worker.data.heartbeat_status)}heartbeat {worker.data.heartbeat_status}</StatusBadge>}
          <button className="link-button" aria-label="Refresh worker activity" onClick={worker.reload}><RefreshCw size={12} /> Refresh →</button>
        </div>} />
        <PanelQueryState query={worker} emptyTitle="No worker activity recorded">
          {worker.data && <>
            <dl className="detail-list">
              <div><dt>{t("Heartbeat age")}</dt><dd>{worker.data.heartbeat_age_seconds == null ? "No heartbeat recorded" : `${worker.data.heartbeat_age_seconds.toFixed(1)} s`}</dd></div>
              <div><dt>{t("Heartbeat status")}</dt><dd><StatusBadge tone={heartbeatTone(worker.data.heartbeat_status)}>
                {heartbeatIcon(worker.data.heartbeat_status)}{worker.data.heartbeat_status}</StatusBadge></dd></div>
              <div><dt>{t("Freshness limit")}</dt><dd>{worker.data.heartbeat_limit_seconds} s</dd></div>
              <div><dt>{t("Observed")}</dt><dd className="mono">{utc(worker.data.observed_at)}</dd></div>
            </dl>
            <h3 className="detail-heading"><HeartPulse size={14} /> In-flight jobs ({worker.data.running_count})</h3>
            {runningJobs.length
              ? <ul className="receipt-list" aria-label="In-flight jobs">{runningJobs.map(job => <li key={job.job_id}>
                <span className="mono">{job.job_id}</span>
                <span>run <CopyId value={job.run_id} /></span>
                <span className="stage-chip"><Timer size={11} /> attempt {job.attempts}</span>
              </li>)}</ul>
              : <EmptyState title="No job is running" detail="The ledger reports no in-flight job for this worker right now." />}
            {worker.data.heartbeat_status === "stale" && <p className="inline-warning"><AlertTriangle size={14} />
              The heartbeat is older than {worker.data.heartbeat_limit_seconds}s, so queued work may not be picked up.</p>}
            {worker.data.heartbeat_status === "unknown" && <p className="filter-note"><Clock size={12} />
              No heartbeat has been recorded yet, so liveness is unknown rather than assumed healthy.</p>}
          </>}
        </PanelQueryState>
      </section>

      <section className="panel" aria-label="Capacity history">
        <PanelHeading eyebrow="Recorded measurement" title="Capacity history" action={<div className="header-actions">
          <button className="link-button" aria-label="Refresh capacity history" onClick={history.reload}><RefreshCw size={12} /> Refresh →</button>
        </div>} />
        <PanelQueryState query={history} emptyTitle="No capacity measurement recorded">
          {history.data && history.data.live && <>
            <h3 className="detail-heading"><HardDrive size={14} /> Live measurement</h3>
            <div className="capacity-meter"><i style={{ width: `${Math.min(100, Math.max(0, (1 - (history.data.live.free_ratio ?? 0)) * 100))}%` }} /></div>
            <dl className="detail-list">
              <div><dt>Status</dt><dd><StatusBadge tone={capacityTone(history.data.live.status)}>
                {capacityIcon(history.data.live.status)}{history.data.live.status}</StatusBadge></dd></div>
              <div><dt>Free ratio</dt><dd>{percent(history.data.live.free_ratio)}</dd></div>
              <div><dt>Warning threshold</dt><dd>{percent(history.data.live.warning_free_ratio)}</dd></div>
              <div><dt>Critical threshold</dt><dd>{percent(history.data.live.critical_free_ratio)}</dd></div>
              <div><dt>Free space</dt><dd>{bytes(history.data.live.free_bytes)} of {bytes(history.data.live.total_bytes)}</dd></div>
              <div><dt>Measurement source</dt><dd>{history.data.live.fixed_measurement ? "Pinned by the acceptance harness (fixed_measurement)" : "Reported by the API as a live measurement (fixed_measurement = false)"}</dd></div>
            </dl>
            {history.data.live.fixed_measurement && <p className="filter-note"><Pin size={12} />
              fixed_measurement: the acceptance harness pins this free-space ratio, so the value is deterministic rather than a live disk reading.</p>}
            <h3 className="detail-heading"><History size={14} /> Recorded transitions {history.data.event_count > 0 ? `(${history.data.event_count})` : ""}</h3>
            <p className="filter-note">{history.data.note ?? "Only capacity transitions the monitor recorded are listed."}
              {history.data.recorded_only ? " No history is interpolated or reconstructed by the console." : ""}</p>
            {events.length
              ? <DataTable data={events} columns={eventColumns} empty="No capacity transition recorded" />
              : <EmptyState title="No capacity transition recorded"
                detail="The monitor has not recorded a warning, critical or recovery transition, so there is no history to show." />}
          </>}
        </PanelQueryState>
      </section>

      <section className="panel">
        <PanelHeading eyebrow={t("Deployment identity")} title={t("Runtime")} />
        <dl className="detail-list"><div><dt>Deployment</dt><dd>{health?.deployment_id ? <CopyId value={health.deployment_id} /> : "—"}</dd></div><div><dt>Version</dt><dd>{health?.software_version ?? "—"}</dd></div><div><dt>Source commit</dt><dd>{health?.source_commit ? <CopyId value={health.source_commit} /> : "—"}</dd></div><div><dt>Read path</dt><dd>{health?.read_status ?? "—"}</dd></div><div><dt>Write path</dt><dd>{health?.write_status ?? "—"}</dd></div><div><dt>Worker heartbeat</dt><dd>{health?.worker_heartbeat_age_seconds == null ? "—" : `${Math.round(health.worker_heartbeat_age_seconds)}s`}</dd></div></dl>
        <p className="filter-note">Identity comes from the readiness envelope; the console never displays a commit it was not told.</p>
      </section>

      <section className="panel operations-wide" aria-label="Capacity and recovery">
        <PanelHeading eyebrow="Storage protection" title="Capacity and recovery" action={<StatusBadge tone={capacity?.status === "critical" ? "bad" : capacity?.status === "warning" ? "warn" : "good"}>{capacity?.status ?? "unknown"}</StatusBadge>} />
        {capacity ? <><div className="capacity-meter"><i style={{ width: `${Math.min(100, (1 - capacity.free_ratio) * 100)}%` }} /></div><dl className="detail-list"><div><dt>{t("Total")}</dt><dd>{bytes(capacity.total_bytes)}</dd></div><div><dt>{t("Used")}</dt><dd>{bytes(capacity.used_bytes)}</dd></div><div><dt>{t("Warning threshold")}</dt><dd>{(capacity.warning_free_ratio * 100).toFixed(0)}%</dd></div><div><dt>{t("Critical threshold")}</dt><dd>{(capacity.critical_free_ratio * 100).toFixed(0)}%</dd></div><div><dt>{t("Latest backup")}</dt><dd>{metrics?.last_successful_backup_at ? utc(metrics.last_successful_backup_at) : t("Not recorded")}</dd></div><div><dt>{t("Latest recovery drill")}</dt><dd>{metrics?.last_successful_recovery_drill_at ? utc(metrics.last_successful_recovery_drill_at) : t("Not recorded")}</dd></div></dl></> : <EmptyState title={t("Capacity unavailable")} />}

        <h3 className="detail-heading"><Package size={14} /> Latest receipt per action</h3>
        {receipts.status === "loading" && <LoadingSkeleton rows={2} />}
        {receipts.status === "error" && (receipts.permission === "unauthorized"
          ? <LockedState message={receipts.error} />
          : <ErrorState message={receipts.error ?? "Unable to load operational receipts"} onRetry={receipts.reload} />)}
        {receipts.data && receipts.data.available === false && <div className="receipt-unavailable" role="status">
          <AlertTriangle size={16} />
          <div><b>Receipt index unavailable</b>
            <p>{receipts.data.note ?? "The API reports that the receipt index is unavailable, so no backup, restore, drill, release or deployment record can be shown."}</p>
            <p>This is a capability gap, not an empty success state.</p></div>
          <button className="link-button" onClick={receipts.reload}>Retry →</button>
        </div>}
        {receipts.data && receipts.data.available && <>
          <ul className="receipt-list" aria-label="Latest receipt per action">
            {receiptActionsOf(latest).map(action => {
              const receipt: OperationsReceipt | null | undefined = latest[action];
              return <li key={action}>
                <b>{action}</b>
                {receipt ? <>
                  <StatusBadge tone={resultTone(receipt.result)}>{receipt.result}</StatusBadge>
                  <span className="filter-note">completed (UTC)</span>
                  <span className="mono">{utc(receipt.completed_at)}</span>
                  <span>deployment {receipt.deployment_id ? <CopyId value={receipt.deployment_id} /> : "—"}</span>
                  <span className="filter-note">{receipt.reference}</span>
                  <span className="filter-note">{fieldsText(receipt.fields)}</span>
                </> : <span className="filter-note">{t("No receipt recorded")}</span>}
              </li>;
            })}
          </ul>
          {recentReceipts.length > 0 && <>
            <h3 className="detail-heading"><ScrollText size={14} /> Recent receipts ({recentReceipts.length}) · completed (UTC)</h3>
            <ul className="receipt-list compact" aria-label="Recent receipts">
              {recentReceipts.map(receipt => <li key={`${receipt.action}-${receipt.reference}`}>
                <b>{receipt.action}</b>
                <StatusBadge tone={resultTone(receipt.result)}>{receipt.result}</StatusBadge>
                <span className="mono">{utc(receipt.completed_at)}</span>
                <span className="filter-note">{receipt.reference}</span>
              </li>)}
            </ul>
          </>}
          <p className="filter-note"><ShieldCheck size={12} /> Receipts are immutable records written by backup, restore, drill, release and deployment runs.</p>
        </>}
      </section>

      <section className="panel alerts-panel">
        <PanelHeading eyebrow="Aggregated signals" title="Active alerts" />
        {alerts.length ? <div className="alert-list">{alerts.map(alert => <div key={alert.title}><StatusBadge tone={alert.tone}>{alert.title}</StatusBadge><span>{alert.detail}</span></div>)}</div> : <EmptyState title="No active alerts" detail="Capacity, dead letters and operational snapshot are clear." />}
      </section>

      <section className="panel operations-wide" aria-label="Write audit trail">
        <PanelHeading eyebrow="Append-only ledger" title="Write audit trail" action={<div className="header-actions">
          <StatusBadge tone="neutral">{entries.length} record(s)</StatusBadge>
          <button className="link-button" aria-label="Refresh write audit trail" onClick={audit.reload}><RefreshCw size={12} /> Refresh →</button>
        </div>} />
        <p className="filter-note"><ShieldCheck size={12} /> The actor column is a non-reversible fingerprint of the caller, never an API key or credential. Outcomes are shown as recorded: queued, rejected and protected are distinct states.</p>
        <PanelQueryState query={audit} emptyTitle="No write operation recorded"
          emptyDetail="The trail records queued, rejected and protected writes; it stays empty until a maintenance, recovery or ingest command is issued.">
          <DataTable data={entries} columns={auditColumns} empty="No write operation recorded." />
        </PanelQueryState>
      </section>

      <section className="panel operation-command operations-wide">
        <PanelHeading eyebrow="Authorized command" title="Queue ingest" />
        <form onSubmit={submit}>
          <label>{t("Run scope")}<select aria-label={t("Run scope")} value={runScope} onChange={event => setRunScope(event.target.value as RunScope)}>{scopes.map(scope => <option key={scope} value={scope}>{scope}</option>)}</select></label>
          <label>Provider<input value={provider} onChange={event => setProvider(event.target.value)} /></label>
          <label>Symbol<input value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
          <label>Asset class<select aria-label="Asset class" value={assetClass} onChange={event => setAssetClass(event.target.value)}>{assetClasses.map(value => <option key={value}>{value}</option>)}</select></label>
          <label>Timeframe<select aria-label="Ingest timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)}>{timeframes.map(value => <option key={value}>{value}</option>)}</select></label>
          <label>Start<input type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
          <label>End<input type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
          <button className="primary-button" type="submit" disabled={writesProtected}>Review ingest</button>
        </form>
        {validationError && <p className="protected-copy" role="alert">{validationError}</p>}
        {writesProtected && <p className="protected-copy">{health ? "Capacity or readiness protection is active. Reads and recovery remain available." : "Write status is unavailable until the API reports an authorized path."}</p>}
      </section>
    </div>

    {confirm && <ConfirmDialog title="Queue ingest run?" detail={`${runScope} · ${provider} ${symbol} (${assetClass}, ${timeframe}), ${start} through ${end}. This creates an asynchronous run.`} confirmLabel="Queue ingest" busy={busy} onConfirm={() => void ingest()} onCancel={() => setConfirm(false)} />}
  </div>;
}

function LockedState({ message }: { message: string | null }) {
  const { t } = usePreferences();
  return <div className="locked-state" role="status"><Lock size={18} /><div><b>{t("Not authorized")}</b>
    <p>{message ? `${message} · ` : ""}This operations view requires a valid API key. Open Session access, enter the key and retry.</p></div></div>;
}

function PanelQueryState({ query, emptyTitle, emptyDetail, children }: {
  query: QueryResult<unknown>;
  emptyTitle: string;
  emptyDetail?: string;
  children: ReactNode;
}) {
  if (query.status === "loading") return <LoadingSkeleton rows={3} />;
  if (query.status === "error") return query.permission === "unauthorized"
    ? <LockedState message={query.error} />
    : <ErrorState message={query.error ?? "Unable to load operations data"} onRetry={query.reload} />;
  if (query.status === "empty") return <EmptyState title={emptyTitle} detail={emptyDetail} />;
  return <>{children}</>;
}

function OperationsCount({ icon, label, value, hint }: { icon: ReactNode; label: string; value: number; hint: string }) {
  return <article className="operations-count">
    <span>{icon}{label}</span>
    <b>{value}</b>
    <small>{hint}</small>
  </article>;
}
