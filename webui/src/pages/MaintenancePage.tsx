import { TimeDisplay, usePreferences } from "../preferences";
import { useEffect, useMemo, useState } from "react";
import {
  Activity, AlertTriangle, CalendarClock, CheckCircle2, Database, FileCheck2, Hammer,
  Layers, Lock, PlayCircle, RefreshCw, ShieldAlert, Sigma, Waves, XCircle,
} from "lucide-react";
import { CopyId, DataTable, DetailDrawer, FilterBar, LoadingSkeleton, PanelHeading, StatusBadge } from "../components/ui";
import { deleteTemplate, loadTemplates, saveTemplate, type TaskTemplate } from "../lib/templates";
import { useMaintenanceMutation, useQuery, useRunTracker, runScopeOptions, type WriteState } from "../hooks";
import type { MaintenanceTaskRecord, MaintenanceTaskRequest, QueuedEnvelope, Run, RunDetail, RunKind, RunScope, TaskPreview } from "../lib/api";
import type { Services } from "../services";
import type { ColumnDef } from "@tanstack/react-table";

// The console never guesses what a run kind means: each entry states the
// dataset it writes and whether it publishes a canonical part.
const runKinds: Array<{ value: RunKind; label: string; dataset: string; hint: string; icon: typeof Hammer }> = [
  { value: "ingest", label: "Provider ingest", dataset: "provider_bars", hint: "Fetch raw bars from a registered provider", icon: Waves },
  { value: "backfill", label: "Backfill", dataset: "provider_bars", hint: "Shard a wide historical range into bounded windows", icon: Layers },
  { value: "gap_repair", label: "Gap repair", dataset: "provider_bars", hint: "Re-fetch only the intervals coverage reports as missing", icon: ShieldAlert },
  { value: "derive", label: "Derive", dataset: "market_bars", hint: "Run a registered transform recipe over an input snapshot", icon: Sigma },
  { value: "quality", label: "Quality check", dataset: "provider_bars", hint: "Verify a selector and record findings; publishes nothing", icon: FileCheck2 },
  { value: "parity", label: "Parity check", dataset: "market_bars", hint: "Verify derived rows against the raw layer that produced them", icon: Activity },
];

const stateLabel: Record<WriteState, string> = {
  draft: "Draft", validating: "Validating", confirming: "Awaiting confirmation", queued: "Queued",
  running: "Running", pass: "Passed", degraded: "Degraded", failed: "Failed",
};

const tone = (state: WriteState | string) =>
  state === "pass" ? "good" as const
    : ["failed", "dead_letter", "protected", "unauthorized"].includes(state) ? "bad" as const
      : state === "degraded" || state === "queued" || state === "running" ? "warn" as const : "neutral" as const;

const utc = (value?: string | null) => <TimeDisplay value={value} />;

const isoFromInput = (value: string) => value ? new Date(`${value}T00:00:00Z`).toISOString() : "";

export function MaintenancePage({ apiKey, services, onMessage, onChanged, draft }: {
  apiKey: string;
  services: Services;
  onMessage: (message: string) => void;
  onChanged: () => void;
  // A draft handed over from the explorer or catalog: it only fills the form,
  // the operator still validates it against the live platform before queueing.
  draft?: MaintenanceTaskRequest | null;
}) {
  const { t } = usePreferences();
  const [runKind, setRunKind] = useState<RunKind>("ingest");
  const [runScope, setRunScope] = useState<string>("production");
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("UI_TEST");
  const [timeframe, setTimeframe] = useState("1d");
  const [seriesId, setSeriesId] = useState("PAYEMS");
  const [recipeId, setRecipeId] = useState("utc-24x7-1m-to-1d-ohlcv");
  const [recipeVersion, setRecipeVersion] = useState("1");
  const [priceBasis, setPriceBasis] = useState("raw");
  const [start, setStart] = useState("2026-01-01");
  const [end, setEnd] = useState("2026-01-05");
  const [schedule, setSchedule] = useState<"manual" | "hourly" | "daily">("manual");
  const [tracked, setTracked] = useState<string[]>([]);
  const [submissions, setSubmissions] = useState<QueuedEnvelope[]>([]);
  const [templates, setTemplates] = useState<TaskTemplate[]>(() => loadTemplates());
  const [templateName, setTemplateName] = useState("");
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [taskFilter, setTaskFilter] = useState("");
  const [taskDatasetFilter, setTaskDatasetFilter] = useState("");
  const [taskKindFilter, setTaskKindFilter] = useState("");
  const [selectedRegistryTask, setSelectedRegistryTask] = useState<MaintenanceTaskRecord | null>(null);

  const [assetClass, setAssetClass] = useState("");
  const capabilities = useQuery(() => services.catalog.capabilities(), [services, apiKey]);
  const definition = runKinds.find(item => item.value === runKind)!;
  // The economic dataset is served by the fred provider only; selecting it
  // switches the form to series semantics instead of symbol semantics.
  const economic = provider === "fred";
  const datasetId = economic ? "economic_observations" : definition.dataset;
  const effectiveProvider = economic ? "fred" : provider;

  const mutation = useMaintenanceMutation(services, envelope => {
    setTracked(envelope.run_ids);
    setSubmissions(current => [envelope, ...current].slice(0, 6));
    onMessage(`Queued ${envelope.run_kind} · ${envelope.run_ids.length} run(s) · run ${envelope.run_id?.slice(0, 8) ?? "—"}`);
    onChanged();
  });
  const tracker = useRunTracker(services, tracked);
  const maintenanceTasks = useQuery(() => services.maintenance.list(), [services, apiKey, submissions.length]);
  const maintenanceColumns = useMemo<ColumnDef<MaintenanceTaskRecord>[]>(() => [
    { accessorKey: "task_id", header: t("Task ID"), cell: info => <CopyId value={String(info.getValue())} /> },
    { accessorKey: "dataset_id", header: t("Dataset") },
    { accessorKey: "run_kind", header: t("Kind") },
    { accessorKey: "status", header: t("Status"), cell: info => <StatusBadge tone={tone(String(info.getValue()))}>{String(info.getValue())}</StatusBadge> },
    { accessorKey: "recent_run_id", header: t("Recent run"), cell: info => info.getValue() ? <CopyId value={String(info.getValue())} /> : "—" },
    { accessorKey: "next_run_at", header: t("Next run"), cell: info => <TimeDisplay value={String(info.getValue() ?? "")} /> },
    { accessorKey: "recent_error", header: t("Recent error"), cell: info => String(info.getValue() ?? "—") },
    { accessorKey: "updated_at", header: t("Updated"), cell: info => <TimeDisplay value={String(info.getValue() ?? "")} /> },
    { accessorKey: "task_id", header: t("Actions"), cell: info => { const id = String(info.getValue()); const row = info.row.original; return <><button className="link-button" onClick={() => setSelectedRegistryTask(row)}>{t("Details")}</button><button className="link-button" onClick={() => void services.maintenance.updateStatus(id, row.status === "paused" ? "enabled" : "paused").then(() => maintenanceTasks.reload()).catch(error => onMessage(error instanceof Error ? error.message : "Unable to update task"))}>{row.status === "paused" ? "Enable" : "Pause"}</button></>; } },
  ], []);

  // The platform publishes which dataset each run kind can target; the console
  // disables the combinations it cannot serve instead of letting the operator
  // submit a request the planner will reject.
  const runKindMatrix = capabilities.data?.run_kinds ?? [];
  const datasetForKind = (kind: RunKind) =>
    economic ? "economic_observations" : runKinds.find(item => item.value === kind)!.dataset;
  const kindAvailable = (kind: RunKind) => {
    if (!runKindMatrix.length) return true;
    const entry = runKindMatrix.find(item => item.run_kind === kind);
    return !entry || entry.datasets.includes(datasetForKind(kind));
  };
  const providers = capabilities.data?.providers ?? [];
  const selectedProvider = providers.find(item => item.provider === effectiveProvider);
  const timeframes = useMemo(() => {
    if (!selectedProvider) return ["1d", "1m"];
    const values = runKind === "ingest" || runKind === "backfill" || runKind === "gap_repair"
      ? selectedProvider.maintenance_timeframes : selectedProvider.timeframes;
    return values.includes("*") ? ["1d", "1m"] : values;
  }, [runKind, selectedProvider]);
  const recipes = capabilities.data?.recipes ?? [];
  const recipe = recipes.find(item => item.recipe_id === recipeId && item.recipe_version === recipeVersion);

  const task: MaintenanceTaskRequest = {
    run_kind: runKind,
    run_scope: runScope as RunScope,
    dataset_id: datasetId,
    provider: effectiveProvider,
    symbol: economic ? null : symbol,
    asset_class: economic ? null : assetClass || null,
    timeframe,
    series_id: economic ? seriesId : null,
    recipe_id: definition.dataset === "market_bars" ? recipeId : null,
    recipe_version: definition.dataset === "market_bars" ? recipeVersion : null,
    price_basis: definition.dataset === "market_bars" ? priceBasis : null,
    start: isoFromInput(start),
    end: isoFromInput(end),
    schedule,
  };

  const unavailableKind = runKindMatrix.length > 0 && !kindAvailable(runKind);
  useEffect(() => {
    if (!unavailableKind) return;
    const fallback = runKinds.find(item => kindAvailable(item.value));
    if (!fallback) return;
    setRunKind(fallback.value);
    mutation.reset();
    onMessage(`${runKind} is not available for ${effectiveProvider}; switched to ${fallback.label}.`);
    // Only the availability decision may trigger the switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unavailableKind, effectiveProvider]);

  const draftKey = draft ? JSON.stringify(draft) : "";
  useEffect(() => {
    if (!draft) return;
    setRunKind(draft.run_kind);
    setRunScope(draft.run_scope);
    setProvider(draft.provider);
    if (draft.symbol) setSymbol(draft.symbol);
    if (draft.asset_class) setAssetClass(draft.asset_class);
    if (draft.timeframe) setTimeframe(draft.timeframe);
    if (draft.series_id) setSeriesId(draft.series_id);
    if (draft.recipe_id) setRecipeId(draft.recipe_id);
    if (draft.recipe_version) setRecipeVersion(draft.recipe_version);
    if (draft.price_basis) setPriceBasis(draft.price_basis);
    setStart(draft.start.slice(0, 10));
    setEnd(draft.end.slice(0, 10));
    mutation.reset();
    onMessage("Prefilled from the explorer; validate before queueing.");
    // The draft identity is the only trigger: re-running on every render would
    // discard operator edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  const review = async () => {
    onMessage("");
    await mutation.validate(task);
  };

  const applyTemplate = (name: string) => {
    setSelectedTemplate(name);
    const template = templates.find(item => item.name === name);
    if (!template) return;
    const saved = template.task;
    setRunKind(saved.run_kind);
    setRunScope(saved.run_scope);
    setProvider(saved.provider);
    if (saved.symbol) setSymbol(saved.symbol);
    if (saved.asset_class) setAssetClass(saved.asset_class);
    if (saved.timeframe) setTimeframe(saved.timeframe);
    if (saved.series_id) setSeriesId(saved.series_id);
    if (saved.recipe_id) setRecipeId(saved.recipe_id);
    if (saved.recipe_version) setRecipeVersion(saved.recipe_version);
    if (saved.price_basis) setPriceBasis(saved.price_basis);
    setStart(saved.start.slice(0, 10));
    setEnd(saved.end.slice(0, 10));
    mutation.reset();
    onMessage(`Loaded template ${name}; validate it again before queueing.`);
  };

  const capabilitiesUnavailable = capabilities.status === "error";
  const locked = mutation.permission === "unauthorized";
  const protectedWrite = mutation.permission === "protected" || mutation.preview?.write_status === "protected";

  const submissionColumns: ColumnDef<QueuedEnvelope>[] = [
    { accessorKey: "task_id", header: "Task", cell: info => <CopyId value={String(info.getValue())} /> },
    { accessorKey: "run_kind", header: t("Kind") },
    { accessorKey: "dataset_id", header: t("Dataset") },
    { accessorKey: "window_count", header: "Windows" },
    { accessorKey: "submitted_at", header: "Submitted", cell: info => utc(String(info.getValue())) },
  ];

  return <>
    <section className="panel" aria-label="Maintenance task form">
      <PanelHeading eyebrow="Data maintenance" title="Create a maintenance task"
        action={<StatusBadge tone={capabilitiesUnavailable ? "bad" : derivedWriteTone(capabilities.data?.write_status)}>
          {capabilitiesUnavailable ? "Capabilities unavailable"
            : capabilities.data?.write_status === "protected" ? "Writes protected" : "Writes available"}
        </StatusBadge>} />
      <FilterBar>
        <label>Task template<select aria-label="Task template" value={selectedTemplate}
          onChange={event => applyTemplate(event.target.value)}>
          <option value="">— none —</option>
          {templates.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
        </select></label>
        <label>Template name<input aria-label="Template name" value={templateName}
          onChange={event => setTemplateName(event.target.value)} placeholder="nightly dukascopy 1m" /></label>
        <button className="secondary-button" onClick={() => {
          setTemplates(saveTemplate(templateName, task));
          setSelectedTemplate(templateName.trim());
          setTemplateName("");
          onMessage("Template saved in this browser; it is re-validated on every use.");
        }} disabled={!templateName.trim()}>Save template</button>
        <button className="secondary-button" onClick={() => {
          if (!selectedTemplate) return;
          setTemplates(deleteTemplate(selectedTemplate));
          setSelectedTemplate("");
        }} disabled={!selectedTemplate}>Delete template</button>
        <span className="filter-note">Templates hold parameters only, never credentials, and stay in this browser.</span>
      </FilterBar>

      <div className="run-kind-grid" role="radiogroup" aria-label="Run kind">
        {runKinds.map(({ value, label, dataset, hint, icon: Icon }) => (
          <button key={value} role="radio" aria-checked={runKind === value} aria-label={label}
            disabled={!kindAvailable(value)}
            title={kindAvailable(value) ? hint : `${label} is not available for ${datasetForKind(value)}`}
            className={`run-kind${runKind === value ? " selected" : ""}`}
            onClick={() => { setRunKind(value); mutation.reset(); }}>
            <Icon size={16} /><b>{label}</b><small>{hint}</small><span className="mono">{dataset}</span>
          </button>
        ))}
      </div>

      <FilterBar>
        <label>Run scope<select aria-label="Run scope" value={runScope} onChange={event => setRunScope(event.target.value)}>
          {runScopeOptions.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select></label>
        <label>Provider<select aria-label="Task provider" value={provider}
          onChange={event => { setProvider(event.target.value); mutation.reset(); }}>
          <option value="fred">fred (economic series)</option>
          {providers.map(item => <option key={item.provider} value={item.provider}>{item.provider}</option>)}
        </select></label>
        {economic
          ? <label>Series ID<input aria-label="Task series ID" value={seriesId} onChange={event => setSeriesId(event.target.value)} /></label>
          : <>
            <label>Symbol<input aria-label="Task symbol" value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
            <label>Asset class<input aria-label="Asset class" value={assetClass} placeholder="auto"
              onChange={event => setAssetClass(event.target.value)} /></label>
          </>}
        <label>Timeframe<select aria-label="Task timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)}>
          {timeframes.map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Start (UTC)<input aria-label="Task start date" type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>End (UTC)<input aria-label="Task end date" type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
        <label>Schedule<select aria-label="Task schedule" value={schedule} onChange={event => setSchedule(event.target.value as typeof schedule)}><option value="manual">Manual</option><option value="hourly">Hourly</option><option value="daily">Daily</option></select></label>
      </FilterBar>

      {definition.dataset === "market_bars" && <FilterBar>
        <label>Recipe<select aria-label="Recipe" value={recipeId} onChange={event => setRecipeId(event.target.value)}>
          {recipes.map(item => <option key={`${item.recipe_id}@${item.recipe_version}`} value={item.recipe_id}>{item.recipe_id}</option>)}
          {!recipes.length && <option value={recipeId}>{recipeId}</option>}
        </select></label>
        <label>Recipe version<input aria-label="Recipe version" value={recipeVersion} onChange={event => setRecipeVersion(event.target.value)} /></label>
        <label>Price basis<select aria-label="Price basis" value={priceBasis} onChange={event => setPriceBasis(event.target.value)}>
          {(recipe?.price_bases.length ? recipe.price_bases : selectedProvider?.price_bases ?? ["raw"]).map(value =>
            <option key={value} value={value}>{value}</option>)}
        </select></label>
        <span className="filter-note">{recipe
          ? `${recipe.input_dataset} ${recipe.source_timeframe} → ${recipe.target_timeframe} · ${recipe.partial_bucket_policy} partial buckets`
          : "Recipe is not registered; the preview will report it."}</span>
      </FilterBar>}

      <div className="form-actions">
        <button className="primary-button" onClick={() => void review()} disabled={mutation.state === "validating" || locked}>
          {mutation.state === "validating" ? "Validating…" : "Validate and preview"}
        </button>
        <button className="secondary-button" onClick={() => { mutation.reset(); onMessage(""); }}>Clear</button>
        {locked && <span className="inline-warning" role="status"><Lock size={14} /> Write requests need a valid API key. Open Session access and try again.</span>}
        {protectedWrite && !locked && <span className="inline-warning" role="status"><ShieldAlert size={14} /> Capacity protection blocks this task.</span>}
      </div>
      {mutation.error && <div className="error-state" role="alert"><AlertTriangle size={18} />
        <div><b>{mutation.permission === "unauthorized" ? "Not authorized" : mutation.permission === "protected" ? "Write protected" : "Request rejected"}</b>
          <p>{mutation.error}</p></div></div>}
    </section>

    {mutation.preview && <PreviewPanel preview={mutation.preview} state={mutation.state} warnings={mutation.warnings}
      onConfirm={() => void mutation.submit()} onEdit={mutation.edit} submitting={mutation.state === "queued"} />}

    <section className="panel" aria-label="Maintenance task list">
      <PanelHeading eyebrow={t("Maintenance tasks")} title={t("Task list")} action={<button className="link-button" onClick={() => maintenanceTasks.reload()}>Refresh</button>} />
      <FilterBar><label>Status<select value={taskFilter} onChange={event => setTaskFilter(event.target.value)}><option value="">All</option><option value="queued">Queued</option><option value="running">Running</option><option value="paused">Paused</option><option value="enabled">Enabled</option><option value="pass">Passed</option><option value="failed">Failed</option></select></label><label>Dataset<select value={taskDatasetFilter} onChange={event => setTaskDatasetFilter(event.target.value)}><option value="">All</option>{Array.from(new Set((maintenanceTasks.data ?? []).map(task => task.dataset_id))).map(dataset => <option key={dataset} value={dataset}>{dataset}</option>)}</select></label><label>Kind<select value={taskKindFilter} onChange={event => setTaskKindFilter(event.target.value)}><option value="">All</option>{Array.from(new Set((maintenanceTasks.data ?? []).map(task => task.run_kind))).map(kind => <option key={kind} value={kind}>{kind}</option>)}</select></label></FilterBar>
      {maintenanceTasks.status === "loading" ? <LoadingSkeleton rows={3} /> : maintenanceTasks.status === "error" ? <p className="protected-copy">Unable to load maintenance tasks. Please refresh.</p> : <DataTable data={(maintenanceTasks.data ?? []).filter(task => (!taskFilter || task.status === taskFilter) && (!taskDatasetFilter || task.dataset_id === taskDatasetFilter) && (!taskKindFilter || task.run_kind === taskKindFilter))} columns={maintenanceColumns} empty="No maintenance tasks recorded." />}
    </section>
    {selectedRegistryTask && <DetailDrawer title="Maintenance task details" onClose={() => setSelectedRegistryTask(null)}>
      <dl className="detail-list"><div><dt>Task ID</dt><dd><CopyId value={selectedRegistryTask.task_id} /></dd></div><div><dt>Dataset</dt><dd>{selectedRegistryTask.dataset_id}</dd></div><div><dt>Kind</dt><dd>{selectedRegistryTask.run_kind}</dd></div><div><dt>Status</dt><dd><StatusBadge tone={tone(selectedRegistryTask.status)}>{selectedRegistryTask.status}</StatusBadge></dd></div><div><dt>Schedule</dt><dd>{selectedRegistryTask.schedule ?? "—"}</dd></div><div><dt>Next run</dt><dd><TimeDisplay value={selectedRegistryTask.next_run_at} /></dd></div><div><dt>Recent run</dt><dd>{selectedRegistryTask.recent_run_id ? <CopyId value={selectedRegistryTask.recent_run_id} /> : "—"}</dd></div><div><dt>Recent error</dt><dd>{selectedRegistryTask.recent_error ?? "—"}</dd></div></dl>
    </DetailDrawer>}
    {(submissions.length > 0 || tracked.length > 0) && <section className="panel" aria-label="Submitted tasks">
      <PanelHeading eyebrow="Live activity" title="Submitted tasks"
        action={<TrackBadge state={tracker.state} />} />
      <DataTable data={submissions} columns={submissionColumns} empty="No maintenance task submitted in this session." />
      {tracker.runs.length > 0 && <ul className="track-list" aria-label="Tracked runs">
        {tracker.runs.map(run => <TrackedRun key={run.run_id} run={run} />)}
      </ul>}
      {tracker.error && <p className="inline-warning"><AlertTriangle size={14} /> {tracker.error}</p>}
    </section>}
  </>;
}

const derivedWriteTone = (status?: string) => status === "protected" ? "bad" as const : "good" as const;

function TrackBadge({ state }: { state: WriteState }) {
  const icon = state === "pass" ? <CheckCircle2 size={13} />
    : state === "failed" ? <XCircle size={13} />
      : state === "degraded" ? <AlertTriangle size={13} />
        : <RefreshCw size={13} className={state === "running" || state === "queued" ? "spin" : undefined} />;
  return <StatusBadge tone={tone(state)}>{icon}{stateLabel[state]}</StatusBadge>;
}

function TrackedRun({ run }: { run: RunDetail }) {
  return <li className="track-row">
    <CopyId value={run.run_id} />
    <StatusBadge tone={tone(run.outcome)}>{run.outcome}</StatusBadge>
    <span className="stage-chip"><PlayCircle size={12} /> {run.stage}</span>
    <span>{run.window_count} window(s)</span>
    {run.degraded_reasons.map(reason => <span className="reason-chip" key={reason.code}>{reason.code}</span>)}
  </li>;
}

function PreviewPanel({ preview, state, warnings, submitting, onConfirm, onEdit }: {
  preview: TaskPreview;
  state: WriteState;
  warnings: Array<{ code: string; message: string }>;
  submitting: boolean;
  onConfirm: () => void;
  onEdit: () => void;
}) {
  const errors = preview.validation.errors;
  const capacity = preview.capacity;
  return <section className="panel preview-panel" aria-label="Task preview">
    <PanelHeading eyebrow="Validation preview" title="Review before submitting"
      action={<StatusBadge tone={preview.submittable ? "good" : "bad"}>
        {preview.submittable ? "Ready to submit" : errors.length ? "Blocked by validation" : "Blocked by capacity"}
      </StatusBadge>} />
    <div className="preview-grid">
      <article className="preview-card"><CalendarClock size={16} /><b>{preview.plan?.window_count ?? 0}</b>
        <span>half-open window(s)</span><small>{preview.plan?.reason ?? preview.task.run_kind} · {preview.plan?.session_profile ?? "session"}</small></article>
      <article className="preview-card"><Database size={16} /><b>{preview.task.dataset_id}</b>
        <span>{preview.task.provider} · {preview.task.timeframe ?? "—"}</span>
        <small className="mono">{preview.plan?.plan_id?.slice(0, 16) ?? "—"}</small></article>
      <article className={`preview-card${capacity.write_status === "protected" ? " protected" : ""}`}>
        {capacity.write_status === "protected" ? <ShieldAlert size={16} /> : <CheckCircle2 size={16} />}
        <b>{capacity.status}</b><span>capacity · {capacity.free_ratio == null ? "unknown" : `${(capacity.free_ratio * 100).toFixed(1)}% free`}</span>
        <small>{capacity.policy} policy · {capacity.requested_days} day(s) requested</small></article>
      <article className="preview-card"><FileCheck2 size={16} /><b>{preview.snapshot ? preview.snapshot.part_count : "—"}</b>
        <span>input part(s)</span>
        <small className="mono">{preview.snapshot?.input_snapshot_id?.slice(0, 16) ?? "no snapshot required"}</small></article>
    </div>

    {preview.coverage && <dl className="detail-list compact">
      <div><dt>Coverage readiness</dt><dd><StatusBadge tone={preview.coverage.readiness_status === "ready" ? "good" : "warn"}>
        {preview.coverage.readiness_status ?? "unknown"}</StatusBadge></dd></div>
      <div><dt>Gap count</dt><dd>{preview.coverage.gap_count ?? "—"}</dd></div>
      <div><dt>Ready intervals</dt><dd>{preview.coverage.ready_interval_count ?? 0}</dd></div>
      <div><dt>Latest complete boundary</dt><dd className="mono">{preview.coverage.latest_complete_boundary ?? "—"}</dd></div>
    </dl>}

    {errors.length > 0 && <ul className="issue-list" aria-label="Validation errors">
      {errors.map(error => <li key={`${error.field}-${error.code}`} className="issue error">
        <XCircle size={14} /><b>{error.field}</b><span>{error.message}</span><code>{error.code}</code></li>)}
    </ul>}
    {capacity.protected_reason && <ul className="issue-list" aria-label="Capacity protection">
      <li className="issue warning"><ShieldAlert size={14} /><b>capacity</b><span>{capacity.protected_reason.message}</span>
        <code>{capacity.protected_reason.code}</code></li>
    </ul>}
    {warnings.length > 0 && <ul className="issue-list" aria-label="Task warnings">
      {warnings.map(warning => <li key={warning.code} className="issue info">
        <AlertTriangle size={14} /><b>notice</b><span>{warning.message}</span><code>{warning.code}</code></li>)}
    </ul>}

    <div className="form-actions">
      <button className="primary-button" onClick={onConfirm} disabled={!preview.submittable || submitting}>
        {submitting ? "Submitting…" : "Confirm and queue"}
      </button>
      <button className="secondary-button" onClick={onEdit} disabled={submitting}>Edit parameters</button>
      <span className="filter-note">Queued work returns a run id; the receipt is only final once the worker publishes it.</span>
    </div>
  </section>;
}

export function MaintenanceTaskDrawer({ envelope, detail, loading, onClose }: {
  envelope: QueuedEnvelope | null;
  detail: RunDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  if (!envelope) return null;
  return <DetailDrawer title="Task details" onClose={onClose}>
    <dl className="detail-list">
      <div><dt>Task ID</dt><dd><CopyId value={envelope.task_id} /></dd></div>
      <div><dt>Run kind</dt><dd>{envelope.run_kind}</dd></div>
      <div><dt>Dataset</dt><dd>{envelope.dataset_id}</dd></div>
      <div><dt>Run scope</dt><dd>{envelope.run_scope}</dd></div>
      <div><dt>Runs</dt><dd>{envelope.run_ids.map(id => <CopyId key={id} value={id} />)}</dd></div>
      <div><dt>Windows</dt><dd>{envelope.window_count}</dd></div>
      <div><dt>Plan</dt><dd>{envelope.plan_id ? <CopyId value={envelope.plan_id} /> : "—"}</dd></div>
      <div><dt>Submitted</dt><dd>{utc(envelope.submitted_at)}</dd></div>
      <div><dt>Input snapshot</dt><dd>{envelope.input_snapshot_id ? <CopyId value={envelope.input_snapshot_id} /> : "—"}</dd></div>
      <div><dt>Audit id</dt><dd>{envelope.audit_id ? <CopyId value={String(envelope.audit_id)} /> : "—"}</dd></div>
    </dl>
    {loading && <LoadingSkeleton rows={3} />}
    {detail && <dl className="detail-list">
      <div><dt>Stage</dt><dd>{detail.stage}</dd></div>
      <div><dt>Outcome</dt><dd>{detail.outcome}</dd></div>
      <div><dt>Manifest</dt><dd>{detail.manifest_status}</dd></div>
      <div><dt>Findings</dt><dd>{detail.finding_count}</dd></div>
    </dl>}
  </DetailDrawer>;
}
