import { type FormEvent, useState } from "react";
import { ConfirmDialog, EmptyState, MetricCard, PanelHeading, StatusBadge } from "../components/ui";
import { createDataCenterClient, type IngestJob, type Metrics, type ReadyState, type RunScope } from "../lib/api";

const bytes = (value?: number) => value == null ? "—" : `${(value / 1024 ** 3).toFixed(1)} GiB`;

const scopes: RunScope[] = ["production", "acceptance", "migration", "maintenance"];
const timeframes = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const assetClasses = ["crypto", "fx", "commodity"];

type OperationsPageProps = {
  apiKey: string;
  health: ReadyState | null;
  metrics: Metrics | null;
  onChanged: () => void;
  onMessage: (message: string) => void;
};

export function OperationsPage({ apiKey, health, metrics, onChanged, onMessage }: OperationsPageProps) {
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
      const result = await createDataCenterClient(apiKey).ingest(job);
      onMessage(`Queued ${runScope} run ${result.data.run_id}`);
      setConfirm(false);
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
      <section className="panel">
        <PanelHeading eyebrow="Storage protection" title="Capacity and recovery" action={<StatusBadge tone={capacity?.status === "critical" ? "bad" : capacity?.status === "warning" ? "warn" : "good"}>{capacity?.status ?? "unknown"}</StatusBadge>} />
        {capacity ? <><div className="capacity-meter"><i style={{ width: `${Math.min(100, (1 - capacity.free_ratio) * 100)}%` }} /></div><dl className="detail-list"><div><dt>Total</dt><dd>{bytes(capacity.total_bytes)}</dd></div><div><dt>Used</dt><dd>{bytes(capacity.used_bytes)}</dd></div><div><dt>Warning threshold</dt><dd>{(capacity.warning_free_ratio * 100).toFixed(0)}%</dd></div><div><dt>Critical threshold</dt><dd>{(capacity.critical_free_ratio * 100).toFixed(0)}%</dd></div><div><dt>Latest backup</dt><dd>{metrics?.last_successful_backup_at ?? "Not recorded"}</dd></div><div><dt>Latest recovery drill</dt><dd>{metrics?.last_successful_recovery_drill_at ?? "Not recorded"}</dd></div></dl></> : <EmptyState title="Capacity unavailable" />}
      </section>

      <section className="panel">
        <PanelHeading eyebrow="Deployment identity" title="Runtime" />
        <dl className="detail-list"><div><dt>Deployment</dt><dd className="mono">{health?.deployment_id ?? "—"}</dd></div><div><dt>Version</dt><dd>{health?.software_version ?? "—"}</dd></div><div><dt>Source commit</dt><dd className="mono">{health?.source_commit ?? "—"}</dd></div><div><dt>Read path</dt><dd>{health?.read_status ?? "—"}</dd></div><div><dt>Write path</dt><dd>{health?.write_status ?? "—"}</dd></div><div><dt>Worker heartbeat</dt><dd>{health?.worker_heartbeat_age_seconds == null ? "—" : `${Math.round(health.worker_heartbeat_age_seconds)}s`}</dd></div></dl>
      </section>

      <section className="panel alerts-panel">
        <PanelHeading eyebrow="Aggregated signals" title="Active alerts" />
        {alerts.length ? <div className="alert-list">{alerts.map(alert => <div key={alert.title}><StatusBadge tone={alert.tone}>{alert.title}</StatusBadge><span>{alert.detail}</span></div>)}</div> : <EmptyState title="No active alerts" detail="Capacity, dead letters and operational snapshot are clear." />}
      </section>

      <section className="panel operation-command">
        <PanelHeading eyebrow="Authorized command" title="Queue ingest" />
        <form onSubmit={submit}>
          <label>Run scope<select aria-label="Run scope" value={runScope} onChange={event => setRunScope(event.target.value as RunScope)}>{scopes.map(scope => <option key={scope} value={scope}>{scope}</option>)}</select></label>
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
