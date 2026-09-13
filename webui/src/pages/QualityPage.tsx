import { TimeDisplay, usePreferences } from "../preferences";
/**
 * Quality feedback loop (v0.4 Phase 3).
 *
 * Findings are observed defects, not job failures: the page keeps the handling
 * state, the run that reported the defect and the data position apart, and it
 * only offers a repair task when the finding plus its reporting run record
 * enough facts to bound a truthful one.
 *
 * The page queries `services.quality.findings` itself: server-side filters and
 * the opaque cursor are the source of truth, the `findings` prop is only the
 * console-wide snapshot used to seed the filter option lists.
 */
import { useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import {
  AlertTriangle, CalendarClock, CheckCircle2, ChevronLeft, ChevronRight, Database, DatabaseZap,
  Eye, FileSearch, Hammer, History, Info, Layers, Lock, RefreshCw, RotateCcw, ShieldAlert, Waves, XCircle,
} from "lucide-react";
import {
  ConfirmDialog, CopyId, DataTable, DetailDrawer, ErrorState, FilterBar, LoadingSkeleton, PanelHeading, StatusBadge,
} from "../components/ui";
import { messageOf, permissionOf, useMaintenanceMutation, useQuery, type PermissionState } from "../hooks";
import type {
  Finding, FindingState, MaintenanceTaskRequest, QueuedEnvelope, RunDetail, RunKind, TaskPreview, ValidationIssue,
} from "../lib/api";
import type { Filters, Paged, Services } from "../services";
import "./QualityPage.css";

const PAGE_SIZE = 25;

type QualityPageProps = {
  /** Initial/global snapshot; the API query below stays the source of truth. */
  findings?: Finding[];
  services: Services;
  onMessage: (message: string) => void;
  onChanged: () => void;
  onMaintenance?: () => void;
};

// -- formatting ------------------------------------------------------------

const utc = (value?: string | null) => <TimeDisplay value={value} />;

const isDay = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value);

/** A date-only value is already UTC; a timestamp is rendered in UTC. */
const utcPosition = (value?: string | null) => {
  if (!value) return "—";
  return isDay(value) ? `${value} (UTC date)` : utc(value);
};

const dayToIso = (value: string, endOfDay = false) =>
  value ? new Date(`${value}T${endOfDay ? "23:59:59" : "00:00:00"}Z`).toISOString() : undefined;

const stamp = (value: string | null | undefined) => {
  const parsed = Date.parse(value ?? "");
  return Number.isNaN(parsed) ? 0 : parsed;
};
const later = (candidate: string, anchor: string) => stamp(candidate) > stamp(anchor);

const stateOf = (finding: Finding): FindingState => finding.state ?? "open";

const observedOf = (finding: Finding) =>
  finding.bar_ts ?? finding.observation_date ?? finding.last_observed_at ?? finding.first_observed_at ?? null;

const selectorText = (selector?: Record<string, string>) =>
  selector && Object.keys(selector).length
    ? Object.entries(selector).map(([key, value]) => `${key}=${value}`).join(" ")
    : "—";

// -- status vocabulary: text + icon, never colour alone --------------------

type Tone = "good" | "warn" | "bad" | "neutral";
type Glyph = typeof AlertTriangle;

const severityTone = (severity: string): Tone =>
  ["error", "high", "critical"].includes(severity) ? "bad"
    : ["warning", "medium"].includes(severity) ? "warn" : "neutral";

const severityIcon = (severity: string): Glyph =>
  severityTone(severity) === "bad" ? XCircle : severityTone(severity) === "warn" ? AlertTriangle : Info;

const stateMeta: Record<FindingState, { tone: Tone; icon: Glyph }> = {
  open: { tone: "warn", icon: AlertTriangle },
  acknowledged: { tone: "neutral", icon: Eye },
  resolved: { tone: "good", icon: CheckCircle2 },
};

// An unrecognised state from the API is shown as unknown rather than crashing.
const stateMetaOf = (state: string) => stateMeta[state as FindingState] ?? { tone: "neutral" as Tone, icon: Info };

type OutcomeMeta = { tone: Tone; icon: Glyph; label: string; detail: string };

// The four outcomes this workspace must never blur together.
const outcomeMeta = (value: string): OutcomeMeta => {
  switch (value) {
    case "failed":
      return { tone: "bad", icon: XCircle, label: "Failed job",
        detail: "the run stopped without publishing; its terminal receipt stays immutable" };
    case "degraded":
      return { tone: "warn", icon: AlertTriangle, label: "Degraded run",
        detail: "the run finished, but the recorded outcome or its data is degraded" };
    case "coverage_not_ready":
      return { tone: "warn", icon: DatabaseZap, label: "Coverage not ready",
        detail: "a degraded dataset, not a failed job: expected timestamps are missing" };
    case "provider_gap":
      return { tone: "warn", icon: Waves, label: "Provider gap",
        detail: "the provider did not return the requested interval; the run may retry" };
    case "pass":
      return { tone: "good", icon: CheckCircle2, label: "Passed", detail: "the run published and reported no degradation" };
    case "queued":
    case "running":
      return { tone: "neutral", icon: RefreshCw, label: value === "queued" ? "Queued" : "Running",
        detail: "work is still in flight; no terminal receipt exists yet" };
    case "dead_letter":
      return { tone: "bad", icon: XCircle, label: "Dead letter", detail: "the run exhausted its retries and awaits review" };
    default:
      return { tone: "neutral", icon: Info, label: value || "Unknown", detail: "the API did not classify this outcome" };
  }
};

const outcomeLegend = ["failed", "degraded", "coverage_not_ready", "provider_gap"];

const DEGRADED_DATASET_CODES = ["coverage_not_ready", "coverage_degraded"];

const OutcomeGlyph = ({ value }: { value: string }) => {
  const meta = outcomeMeta(value);
  const Icon = meta.icon;
  return <StatusBadge tone={meta.tone}><Icon size={12} />{meta.label}</StatusBadge>;
};

// -- recorded coverage -----------------------------------------------------

type CoverageView = {
  dataset_id?: string;
  selector?: Record<string, string>;
  readiness_status?: string;
  gap_count?: number | null;
  missing_timestamp_count?: number | null;
  expected_timestamp_count?: number;
  row_count?: number;
  min_ts?: string | null;
  max_ts?: string | null;
  first_missing_ts?: string | null;
  latest_complete_boundary?: string | null;
  ready_intervals?: Array<{ start: string; end: string; semantics?: string }>;
};

/**
 * A provider_bars finding records one coverage report; a parity finding records
 * ``{derived, source}``. The finding's own dataset decides which one is shown.
 */
const coverageOf = (finding: Finding): { view: CoverageView; layer: string } | null => {
  const raw = finding.coverage as unknown as Record<string, unknown> | undefined;
  if (!raw || typeof raw !== "object") return null;
  const derived = raw.derived;
  if (derived && typeof derived === "object") {
    return { view: derived as CoverageView, layer: "derived market_bars" };
  }
  return { view: raw as CoverageView, layer: finding.dataset_id ?? "" };
};

// -- bounded repair task ---------------------------------------------------

const REPAIRABLE_CODES = ["coverage_not_ready", "coverage_degraded", "lineage_drift", "parity_missing_outputs", "source_degraded"];

type RepairWindow = { start: string; end: string; note: string };

/**
 * Bound a repair window from what the finding actually recorded: the first gap
 * coverage reports (closed by the next ready interval), otherwise the window
 * the reporting run checked. Nothing is inferred beyond those two records.
 */
const repairWindow = (coverage: CoverageView | null, run: RunDetail | null): RepairWindow | null => {
  const firstMissing = coverage?.first_missing_ts ?? null;
  if (firstMissing) {
    const nextReady = (coverage?.ready_intervals ?? [])
      .map(interval => interval.start)
      .filter(start => later(start, firstMissing))
      .sort((left, right) => stamp(left) - stamp(right))[0];
    if (nextReady) {
      return { start: firstMissing, end: nextReady,
        note: "the first gap the coverage report records, closed by the next ready interval" };
    }
  }
  const range = run?.time_range;
  if (range?.start && range?.end && later(range.end, range.start)) {
    return { start: range.start, end: range.end,
      note: firstMissing
        ? "the window the reporting run checked (the recorded gap is not closed by a later ready interval)"
        : "the window the reporting run checked" };
  }
  return null;
};

type RepairAssessment =
  | { status: "pending" }
  | { status: "available"; runKind: RunKind; task: MaintenanceTaskRequest; windowNote: string; notes: string[] }
  | { status: "blocked"; summary: string; missing: string[] };

const assessRepair = (finding: Finding, reportingRun: RunDetail | null, reportingRunPending: boolean): RepairAssessment => {
  const code = finding.code;
  const coverageInfo = coverageOf(finding);
  const coverage = coverageInfo?.view ?? null;
  const datasetId = finding.dataset_id ?? coverage?.dataset_id ?? null;
  const selector: Record<string, string> = {
    ...(reportingRun?.selector ?? {}),
    ...(coverage?.selector ?? {}),
    ...(finding.selector ?? {}),
  };
  const provider = selector.provider ?? null;
  const symbol = selector.symbol ?? null;
  const timeframe = selector.timeframe ?? null;

  const providerCoverage = datasetId === "provider_bars" && DEGRADED_DATASET_CODES.includes(code);
  const derivedParity = datasetId === "market_bars" && REPAIRABLE_CODES.includes(code);

  if (!providerCoverage && !derivedParity) {
    return {
      status: "blocked",
      summary: `No bounded repair task is defined for code ${code}${datasetId ? ` on ${datasetId}` : ""}.`,
      missing: [`a repairable code (${REPAIRABLE_CODES.join(", ")})`],
    };
  }

  if (derivedParity && !reportingRun) {
    if (reportingRunPending) return { status: "pending" };
    return {
      status: "blocked",
      summary: "The reporting run detail is required before a truthful parity re-check can be bounded.",
      missing: ["the reporting run detail (provider, symbol, recipe, price basis and window are recorded on the run)"],
    };
  }

  const missing: string[] = [];
  if (!provider) missing.push("provider");
  if (!symbol) missing.push("symbol");
  if (!timeframe) missing.push("timeframe");

  if (providerCoverage) {
    const window = repairWindow(coverage, reportingRun);
    if (!window) {
      missing.push("a recorded gap interval (first missing timestamp plus the next ready interval) or the reporting run time range");
    }
    if (missing.length || !window || !provider || !symbol || !timeframe) {
      // The reporting run may still be in flight and carry the missing window.
      if (reportingRunPending) return { status: "pending" };
      return { status: "blocked", summary: "The finding does not record enough facts to bound a gap repair.", missing };
    }
    return {
      status: "available", runKind: "gap_repair",
      task: {
        run_kind: "gap_repair", run_scope: "maintenance", dataset_id: "provider_bars",
        provider, symbol, timeframe, asset_class: null, start: window.start, end: window.end,
      },
      windowNote: window.note,
      notes: ["gap repair re-fetches only the recorded missing interval and publishes a new canonical part."],
    };
  }

  const recipeId = reportingRun?.recipe_id ?? selector.recipe_id ?? null;
  const recipeVersion = reportingRun?.recipe_version ?? selector.recipe_version ?? null;
  const priceBasis = reportingRun?.selector?.price_basis ?? selector.price_basis ?? null;
  if (!recipeId) missing.push("recipe_id");
  if (!recipeVersion) missing.push("recipe_version");
  const window = repairWindow(coverage, reportingRun);
  if (!window) missing.push("the reporting run time range or a recorded coverage gap");
  if (missing.length || !window || !provider || !symbol || !timeframe) {
    return {
      status: "blocked",
      summary: "The finding and its reporting run do not record enough facts to bound a truthful parity re-check.",
      missing,
    };
  }
  return {
    status: "available", runKind: "parity",
    task: {
      run_kind: "parity", run_scope: "maintenance", dataset_id: "market_bars",
      provider, symbol, timeframe,
      recipe_id: recipeId, recipe_version: recipeVersion, price_basis: priceBasis,
      start: window.start, end: window.end,
    },
    windowNote: window.note,
    notes: [
      ...(priceBasis ? [] : ["The reporting run does not record a price basis; the preview resolves it or reports the field as required."]),
      "parity is a read-only verification: it re-checks the derived layer against the raw layer and publishes nothing.",
    ],
  };
};

// -- page ------------------------------------------------------------------

type FilterState = {
  severity: string;
  code: string;
  dataset: string;
  state: string;
  run: string;
  from: string;
  through: string;
};

const emptyFilters: FilterState = { severity: "all", code: "all", dataset: "all", state: "all", run: "", from: "", through: "" };
const findingStates: FindingState[] = ["open", "acknowledged", "resolved"];

const unique = (values: Array<string | undefined | null>) =>
  [...new Set(values.filter((value): value is string => Boolean(value)))].sort();

const noticesOf = (data: Paged<Finding> | null): string[] => {
  if (!data) return [];
  const notices = [...data.warnings];
  const meta = data.meta;
  if (meta["recorded_only"] === true) notices.push("Recorded-only view: the API did not report a live measurement.");
  if (meta["stale"] === true) notices.push("The API reports this page as stale; reload before acting on it.");
  const note = meta["note"];
  if (typeof note === "string" && note) notices.push(note);
  return notices;
};

export function QualityPage({ findings: initialFindings, services, onMessage, onChanged, onMaintenance }: QualityPageProps) {
  const { t } = usePreferences();
  const [filters, setFilters] = useState<FilterState>(emptyFilters);
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selected, setSelected] = useState<Finding | null>(null);

  const apiFilters = useMemo<Filters>(() => ({
    severity: filters.severity === "all" ? undefined : filters.severity,
    code: filters.code === "all" ? undefined : filters.code,
    dataset_id: filters.dataset === "all" ? undefined : filters.dataset,
    state: filters.state === "all" ? undefined : filters.state as FindingState,
    run_id: filters.run.trim() || undefined,
    observed_from: filters.from ? dayToIso(filters.from) : undefined,
    observed_to: filters.through ? dayToIso(filters.through, true) : undefined,
  }), [filters]);
  const filterKey = JSON.stringify(apiFilters);

  const query = useQuery(() => services.quality.findings(apiFilters, cursor, PAGE_SIZE), [services, filterKey, cursor]);
  const items = query.data?.items ?? [];
  const page = query.data?.page ?? null;
  const notices = noticesOf(query.data);
  const stateCounts = (query.data?.meta["state_counts"] as Record<string, number> | undefined) ?? null;

  const optionSource = [...(initialFindings ?? []), ...items];
  const severities = unique(optionSource.map(item => item.severity));
  const codes = unique(optionSource.map(item => item.code));
  const datasets = unique(optionSource.map(item => item.dataset_id));

  // Any filter change restarts paging: the cursor is bound to the filter set.
  const setFilter = (key: keyof FilterState, value: string) => {
    setCursorStack([]);
    setCursor(null);
    setPageIndex(0);
    setFilters(current => ({ ...current, [key]: value }));
  };

  const nextPage = () => {
    if (!page?.has_more) return;
    setCursorStack(stack => [...stack, cursor]);
    setCursor(page.next_cursor ?? null);
    setPageIndex(index => index + 1);
  };

  const previousPage = () => {
    const stack = [...cursorStack];
    const previous = stack.pop() ?? null;
    setCursorStack(stack);
    setCursor(previous);
    setPageIndex(index => Math.max(0, index - 1));
  };

  const restartPaging = () => {
    setCursorStack([]);
    setCursor(null);
    setPageIndex(0);
  };

  const columns = useMemo<ColumnDef<Finding>[]>(() => [
    { accessorKey: "severity", header: t("Severity"), cell: ({ row }) => {
      const severity = row.original.severity;
      const Icon = severityIcon(severity);
      return <StatusBadge tone={severityTone(severity)}><Icon size={12} />{severity}</StatusBadge>;
    } },
    { accessorKey: "code", header: t("Code"), cell: info => <span className="mono">{String(info.getValue())}</span> },
    { accessorKey: "dataset_id", header: t("Dataset"), cell: info => String(info.getValue() ?? "—") },
    { id: "observed", header: t("Observed"), cell: ({ row }) => utcPosition(observedOf(row.original)) },
    { accessorKey: "run_id", header: t("Run"), cell: info => {
      const runId = info.getValue() as string | undefined;
      return runId ? <CopyId value={runId} /> : <span className="filter-note">not recorded</span>;
    } },
    { id: "state", header: t("State"), cell: ({ row }) => {
      const state = stateOf(row.original);
      const meta = stateMetaOf(state);
      const Icon = meta.icon;
      return <StatusBadge tone={meta.tone}><Icon size={12} />{state}</StatusBadge>;
    } },
    { accessorKey: "occurrence_count", header: "Occurrences", cell: ({ row }) => {
      const count = row.original.occurrence_count ?? 1;
      return <span title={`first ${row.original.first_observed_at} · last ${row.original.last_observed_at}`}>{count}</span>;
    } },
  ], []);

  return <>
    <section className="panel quality-page" aria-label="Quality findings">
      <PanelHeading eyebrow="Quality feedback" title="Quality findings"
        action={<div className="header-actions">
          {stateCounts && <span className="filter-note" aria-label="Handling state counts">
            {findingStates.map(state => `${stateCounts[state] ?? 0} ${state}`).join(" · ")}
          </span>}
          <StatusBadge tone="neutral">{page ? `${page.count} on this page` : "—"}</StatusBadge>
          <button className="link-button" onClick={query.reload}>Refresh →</button>
        </div>} />

      <FilterBar>
        <label>Severity<select aria-label="Severity" value={filters.severity}
          onChange={event => setFilter("severity", event.target.value)}>
          <option value="all">all</option>
          {severities.map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Finding code<select aria-label="Finding code" value={filters.code}
          onChange={event => setFilter("code", event.target.value)}>
          <option value="all">all</option>
          {codes.map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Quality dataset<select aria-label="Quality dataset" value={filters.dataset}
          onChange={event => setFilter("dataset", event.target.value)}>
          <option value="all">all</option>
          {datasets.map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Finding state<select aria-label="Finding state" value={filters.state}
          onChange={event => setFilter("state", event.target.value)}>
          <option value="all">all</option>
          {findingStates.map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <label>Finding run<input aria-label="Finding run" value={filters.run} placeholder="run id"
          onChange={event => setFilter("run", event.target.value)} /></label>
        <label>Finding from date<input aria-label="Finding from date" type="date" value={filters.from}
          onChange={event => setFilter("from", event.target.value)} /></label>
        <label>Finding through date<input aria-label="Finding through date" type="date" value={filters.through}
          onChange={event => setFilter("through", event.target.value)} /></label>
        <span className="filter-note">Filter options come from the findings this console has loaded; the API exposes no distinct-value endpoint.</span>
      </FilterBar>

      {notices.length > 0 && <ul className="issue-list" aria-label="Quality notices">
        {notices.map(notice => <li key={notice} className="issue info">
          <Info size={14} /><b>notice</b><span>{notice}</span></li>)}
      </ul>}

      {query.status === "loading" && <LoadingSkeleton rows={5} />}
      {query.status === "error" && (query.permission === "unauthorized"
        ? <div className="locked-state" role="status"><Lock size={18} /><div><b>A valid API key is required</b>
          <p>{query.error} Open Session access with a valid API key, then reload this workspace.</p></div></div>
        : <div>
          <ErrorState message={query.error ?? "Unable to load quality findings"} onRetry={query.reload} />
          <div className="form-actions">
            <button className="secondary-button" onClick={restartPaging}><RotateCcw size={14} /> Restart paging</button>
            <span className="filter-note">A cursor is bound to its filter set; restarting clears the expired cursor.</span>
          </div>
        </div>)}
      {(query.status === "success" || query.status === "empty") && <>
        <DataTable data={items} columns={columns} empty="No quality findings."
          onRowClick={row => setSelected(row)} />
        <div className="pager">
          <button className="secondary-button" aria-label="Previous page" onClick={previousPage} disabled={!cursorStack.length}>
            <ChevronLeft size={14} /> Previous
          </button>
          <span>{`Page ${pageIndex + 1}${page ? ` · ${page.count} finding(s)` : ""}`}</span>
          <button className="secondary-button" aria-label="Next page" onClick={nextPage} disabled={!page?.has_more}>
            Next <ChevronRight size={14} />
          </button>
          <span className="filter-note">Cursor paging is bound to these filters; changing a filter restarts at page 1.</span>
        </div>
      </>}

      <h3 className="detail-heading">Run outcomes in this workspace</h3>
      <ul className="issue-list" aria-label="Run outcome vocabulary">
        {outcomeLegend.map(key => {
          const meta = outcomeMeta(key);
          const Icon = meta.icon;
          return <li key={key} className={`issue ${meta.tone === "bad" ? "error" : "warning"}`}>
            <Icon size={14} /><b>{meta.label}</b><span>({key}) — {meta.detail}</span>
          </li>;
        })}
      </ul>
    </section>

    {selected && <FindingDrawer key={selected.finding_id ?? selected.code} finding={selected}
      services={services} onClose={() => setSelected(null)} onMessage={onMessage} onChanged={onChanged}
      onReload={query.reload} onMaintenance={onMaintenance}
      onPatch={patch => setSelected(current => current ? { ...current, ...patch } : current)} />}
  </>;
}

// -- detail drawer ---------------------------------------------------------

function FindingDrawer({ finding, services, onClose, onMessage, onChanged, onReload, onMaintenance, onPatch }: {
  finding: Finding;
  services: Services;
  onClose: () => void;
  onMessage: (message: string) => void;
  onChanged: () => void;
  onReload: () => void;
  onMaintenance?: () => void;
  onPatch: (patch: Partial<Finding>) => void;
}) {
  const [actionError, setActionError] = useState("");
  const [actionPermission, setActionPermission] = useState<PermissionState>("authorized");
  const [busyState, setBusyState] = useState<FindingState | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(finding.run_id ?? finding.last_run_id ?? null);

  const runQuery = useQuery(() => activeRunId ? services.runs.detail(activeRunId) : Promise.resolve(null),
    [services, activeRunId]);
  const reportingRun = activeRunId && activeRunId === finding.run_id ? runQuery.data : null;
  const reportingRunPending = Boolean(finding.run_id) && activeRunId === finding.run_id && runQuery.status === "loading";
  const reportingRunError = activeRunId === finding.run_id && runQuery.status === "error" ? runQuery.error : null;

  const state = stateOf(finding);
  const coverage = coverageOf(finding);

  const applyState = async (next: FindingState, body: { dataset_id?: string; resolved_by_run_id?: string }): Promise<boolean> => {
    const findingId = finding.finding_id;
    if (!findingId) return false;
    setBusyState(next);
    setActionError("");
    try {
      const record = await services.quality.setState(findingId, next, body);
      const resolvedBy = record.resolved_by_run_id;
      onPatch({
        state: next,
        resolved_by_run_id: typeof resolvedBy === "string" ? resolvedBy : body.resolved_by_run_id ?? finding.resolved_by_run_id ?? null,
        state_updated_at: typeof record.updated_at === "string" ? record.updated_at : finding.state_updated_at,
      });
      onMessage(`${next} · ${findingId}`);
      onReload();
      onChanged();
      return true;
    } catch (reason) {
      setActionPermission(permissionOf(reason));
      setActionError(messageOf(reason));
      onMessage(messageOf(reason));
      return false;
    } finally {
      setBusyState(null);
    }
  };

  const openRun = (runId: string | null) => {
    if (!runId) return;
    if (activeRunId === runId) runQuery.reload();
    else setActiveRunId(runId);
  };

  const meta = stateMetaOf(state);
  const StateIcon = meta.icon;
  const SeverityGlyph = severityIcon(finding.severity);

  return <DetailDrawer title={finding.finding_id ?? finding.code} onClose={onClose}>
    <div className="detail-status">
      <StatusBadge tone={severityTone(finding.severity)}><SeverityGlyph size={12} />{finding.severity}</StatusBadge>
      <StatusBadge tone={meta.tone}><StateIcon size={12} />{state}</StatusBadge>
      <span className="stage-chip">{finding.code}</span>
    </div>

    {DEGRADED_DATASET_CODES.includes(finding.code) && <ul className="issue-list" aria-label="Finding classification">
      <li className="issue warning"><DatabaseZap size={14} /><b>degraded dataset</b>
        <span>{finding.code} reports incomplete data for {finding.dataset_id ?? "this dataset"} — a degraded dataset, not a failed job.</span></li>
    </ul>}

    <dl className="detail-list">
      <div><dt>Finding id</dt><dd>{finding.finding_id ? <CopyId value={finding.finding_id} /> : "—"}</dd></div>
      <div><dt>Severity</dt><dd>{finding.severity}</dd></div>
      <div><dt>Code</dt><dd className="mono">{finding.code}</dd></div>
      <div><dt>Dataset</dt><dd>{finding.dataset_id ?? "—"}</dd></div>
      <div><dt>Message</dt><dd>{finding.message ?? "—"}</dd></div>
      <div><dt>Selector</dt><dd className="mono">{selectorText(finding.selector ?? coverage?.view.selector)}</dd></div>
      <div><dt>Bar timestamp (UTC)</dt><dd>{utcPosition(finding.bar_ts)}</dd></div>
      <div><dt>Observation date (UTC)</dt><dd>{utcPosition(finding.observation_date)}</dd></div>
      <div><dt>Occurrences</dt><dd>{finding.occurrence_count ?? 1}</dd></div>
      <div><dt>First observed</dt><dd>{utc(finding.first_observed_at)}</dd></div>
      <div><dt>Last observed</dt><dd>{utc(finding.last_observed_at)}</dd></div>
      <div><dt>Handling state</dt><dd>{state}{finding.state_updated_at ? <> · updated {utc(finding.state_updated_at)}</> : ""}</dd></div>
      {finding.resolved_by_run_id && <div><dt>Resolved by run</dt><dd><CopyId value={finding.resolved_by_run_id} /></dd></div>}
    </dl>

    {coverage && <>
      <h3 className="detail-heading"><Database size={14} /> Recorded coverage{coverage.layer ? ` · ${coverage.layer}` : ""}</h3>
      <dl className="detail-list">
        <div><dt>Readiness</dt><dd><StatusBadge tone={coverage.view.readiness_status === "ready" ? "good" : "warn"}>
          {coverage.view.readiness_status ?? "unknown"}</StatusBadge></dd></div>
        <div><dt>Gap count</dt><dd>{coverage.view.gap_count ?? "—"}</dd></div>
        <div><dt>Missing timestamps</dt><dd>{coverage.view.missing_timestamp_count ?? "—"}</dd></div>
        <div><dt>Expected timestamps</dt><dd>{coverage.view.expected_timestamp_count ?? "—"}</dd></div>
        <div><dt>Rows recorded</dt><dd>{coverage.view.row_count ?? "—"}</dd></div>
        <div><dt>Latest complete boundary</dt><dd className="mono">{coverage.view.latest_complete_boundary ?? "—"}</dd></div>
      </dl>
      <p className="filter-note">Ready intervals ({(coverage.view.ready_intervals ?? []).length})</p>
      {(coverage.view.ready_intervals ?? []).length > 0
        ? <ul className="issue-list" aria-label="Ready intervals">
          {(coverage.view.ready_intervals ?? []).slice(0, 8).map(interval => <li key={`${interval.start}-${interval.end}`} className="issue info">
            <Layers size={13} /><span className="mono"><TimeDisplay value={interval.start} /> → <TimeDisplay value={interval.end} /></span>
            <code>{interval.semantics ?? "half-open"}</code>
          </li>)}
          {(coverage.view.ready_intervals ?? []).length > 8 && <li className="issue info">
            <Info size={13} /><span className="mono">… {(coverage.view.ready_intervals ?? []).length - 8} more recorded interval(s)</span></li>}
        </ul>
        : <p className="filter-note">The recorded coverage report carries no ready intervals.</p>}
    </>}

    <h3 className="detail-heading"><History size={14} /> Run linkage</h3>
    <dl className="detail-list">
      <div><dt>Reporting run</dt><dd>{finding.run_id ? <CopyId value={finding.run_id} /> : "not recorded"}</dd></div>
      <div><dt>Last observing run</dt><dd>{finding.last_run_id ? <CopyId value={finding.last_run_id} /> : "not recorded"}</dd></div>
    </dl>
    <div className="form-actions">
      <button className="secondary-button" aria-label="Open run" disabled={!finding.run_id}
        onClick={() => openRun(finding.run_id ?? null)}><FileSearch size={14} /> Open run</button>
      {finding.last_run_id && finding.last_run_id !== finding.run_id && <button className="secondary-button"
        onClick={() => openRun(finding.last_run_id ?? null)}>Open last observing run</button>}
    </div>
    {!finding.run_id && <p className="filter-note">This finding records no reporting run, so no run detail can be opened.</p>}

    {activeRunId && <div className="quality-run">
      {runQuery.status === "loading" && <LoadingSkeleton rows={3} />}
      {runQuery.status === "error" && (runQuery.permission === "unauthorized"
        ? <div className="locked-state" role="status"><Lock size={18} /><div><b>A valid API key is required</b>
          <p>{runQuery.error} Run detail stays locked until an authorized key is supplied.</p></div></div>
        : <ErrorState message={runQuery.error ?? "Unable to load the run detail"} onRetry={runQuery.reload} />)}
      {runQuery.data && <>
        <div className="detail-status">
          <OutcomeGlyph value={runQuery.data.outcome} />
          <span className="stage-chip"><Layers size={12} /> stage {runQuery.data.stage}</span>
          {runQuery.data.terminal && <span className="stage-chip"><CheckCircle2 size={12} /> terminal receipt</span>}
        </div>
        <dl className="detail-list">
          <div><dt>Run id</dt><dd><CopyId value={runQuery.data.run_id} /></dd></div>
          <div><dt>Stage</dt><dd>{runQuery.data.stage}</dd></div>
          <div><dt>Outcome</dt><dd>{runQuery.data.outcome}</dd></div>
          <div><dt>Manifest status</dt><dd>{runQuery.data.manifest_status}</dd></div>
          <div><dt>Windows</dt><dd>{runQuery.data.window_count}</dd></div>
          <div><dt>Findings</dt><dd>{runQuery.data.finding_count}</dd></div>
          <div><dt>Run kind</dt><dd>{runQuery.data.run_kind ?? "ingest"}</dd></div>
          <div><dt>Run scope</dt><dd>{runQuery.data.run_scope ?? "—"}</dd></div>
          <div><dt>Created</dt><dd>{utc(runQuery.data.created_at)}</dd></div>
          <div><dt>Terminal</dt><dd>{runQuery.data.terminal ? "yes — receipts are immutable" : "no — still in flight"}</dd></div>
        </dl>
        {runQuery.data.degraded_reasons.length > 0 && <ul className="issue-list" aria-label="Degraded reasons">
          {runQuery.data.degraded_reasons.map(reason => {
            const reasonMeta = outcomeMeta(reason.code);
            const ReasonIcon = reasonMeta.icon;
            return <li key={`${reason.code}-${reason.message}`} className="issue warning">
              <ReasonIcon size={14} /><b>{reason.code}</b><span>{reason.message} — {reasonMeta.detail}</span>
              <code>{reason.source}</code></li>;
          })}
        </ul>}
        <p className="filter-note"><RefreshCw size={12} /> A run receipt is never rewritten by the console, and neither is the finding that referenced it.</p>
      </>}
    </div>}

    <h3 className="detail-heading"><Eye size={14} /> Handling state</h3>
    <ul className="issue-list" aria-label="Handling state notes">
      <li className="issue info"><Info size={14} /><b>additive</b>
        <span>Acknowledging records that an operator has seen this finding. It does not fix the underlying data and never changes the run receipt that reported it.</span></li>
    </ul>
    <div className="quality-actions">
      <button className="secondary-button" aria-label="Acknowledge finding" disabled={busyState !== null}
        onClick={() => void applyState("acknowledged", { dataset_id: finding.dataset_id })}><Eye size={14} /> Acknowledge finding</button>
      <button className="secondary-button" aria-label="Mark resolved" disabled={busyState !== null}
        onClick={() => void applyState("resolved", { dataset_id: finding.dataset_id })}><CheckCircle2 size={14} /> Mark resolved</button>
      <button className="secondary-button" aria-label="Reopen finding" disabled={busyState !== null}
        onClick={() => void applyState("open", { dataset_id: finding.dataset_id })}><RotateCcw size={14} /> Reopen finding</button>
    </div>
    {busyState && <p className="filter-note" role="status">Recording {busyState}…</p>}
    {actionError && (actionPermission === "unauthorized"
      ? <div className="locked-state" role="status"><Lock size={18} /><div><b>A valid API key is required</b>
        <p>{actionError} Finding state changes are authorized writes; open Session access and try again.</p></div></div>
      : <div className="error-state" role="alert"><AlertTriangle size={18} />
        <div><b>{actionPermission === "protected" ? "Write protected" : "Request rejected"}</b><p>{actionError}</p></div></div>)}

    <RepairSection finding={finding} services={services} reportingRun={reportingRun}
      reportingRunPending={reportingRunPending} reportingRunError={reportingRunError}
      onMessage={onMessage} onChanged={onChanged} onMaintenance={onMaintenance}
      onApplyState={applyState} />
  </DetailDrawer>;
}

// -- repair task -----------------------------------------------------------

function RepairSection({ finding, services, reportingRun, reportingRunPending, reportingRunError, onMessage, onChanged, onMaintenance, onApplyState }: {
  finding: Finding;
  services: Services;
  reportingRun: RunDetail | null;
  reportingRunPending: boolean;
  reportingRunError: string | null;
  onMessage: (message: string) => void;
  onChanged: () => void;
  onMaintenance?: () => void;
  onApplyState: (state: FindingState, body: { dataset_id?: string; resolved_by_run_id?: string }) => Promise<boolean>;
}) {
  const [linked, setLinked] = useState<string | null>(null);
  const [confirmEnvelope, setConfirmEnvelope] = useState<QueuedEnvelope | null>(null);
  const [dismissed, setDismissed] = useState(false);

  const mutation = useMaintenanceMutation(services, envelope => {
    onMessage(`Queued ${envelope.run_kind} · ${envelope.run_ids.length} run(s) · run ${envelope.run_id?.slice(0, 8) ?? "—"}`);
  });

  const assessment = useMemo(
    () => assessRepair(finding, reportingRun, reportingRunPending),
    [finding, reportingRun, reportingRunPending],
  );

  const handOff = () => {
    if (onMaintenance) onMaintenance();
    else onMessage("Open the Maintenance workspace to plan a bounded task for this finding.");
  };

  const startPreview = async () => {
    if (assessment.status !== "available") return;
    onMessage("");
    await mutation.validate(assessment.task);
  };

  const envelope = mutation.envelope;
  const queuedRunIds = envelope?.run_ids ?? [];
  const linkableRunId = queuedRunIds[0] ?? envelope?.run_id ?? null;

  const linkRun = async () => {
    if (!confirmEnvelope) return;
    const runId = confirmEnvelope.run_ids[0] ?? confirmEnvelope.run_id;
    setConfirmEnvelope(null);
    if (!runId) return;
    const applied = await onApplyState("resolved", { resolved_by_run_id: runId });
    if (applied) setLinked(runId);
  };

  return <>
    <h3 className="detail-heading"><Hammer size={14} /> Create repair task</h3>

    {assessment.status === "blocked" && <>
      <ul className="issue-list" aria-label="Repair task unavailable">
        <li className="issue warning"><AlertTriangle size={14} /><b>blocked</b><span>{assessment.summary}</span></li>
        {assessment.missing.length > 0 && <li className="issue info"><Info size={14} /><b>missing</b>
          <span>{assessment.missing.join(", ")}. Nothing is inferred to fill the gap.</span></li>}
      </ul>
      <div className="form-actions">
        <button className="primary-button" aria-label="Create repair task" disabled><Hammer size={14} /> Create repair task</button>
        <button className="secondary-button" onClick={handOff}>Open maintenance workspace</button>
      </div>
    </>}

    {assessment.status === "pending" && <>
      <p className="filter-note" role="status">
        <RefreshCw size={12} className="spin" /> Waiting for the reporting run detail before bounding a repair task…
      </p>
      <div className="form-actions">
        <button className="primary-button" aria-label="Create repair task" disabled><Hammer size={14} /> Create repair task</button>
      </div>
    </>}

    {reportingRunError && <p className="inline-warning"><AlertTriangle size={14} /> The reporting run detail could not be loaded: {reportingRunError}</p>}

    {assessment.status === "available" && <>
      <dl className="detail-list compact">
        <div><dt>Run kind</dt><dd>{assessment.runKind}</dd></div>
        <div><dt>Run scope</dt><dd>{assessment.task.run_scope}</dd></div>
        <div><dt>Dataset</dt><dd>{assessment.task.dataset_id}</dd></div>
        <div><dt>Window (UTC, half-open)</dt><dd className="mono"><TimeDisplay value={assessment.task.start} /> → <TimeDisplay value={assessment.task.end} /></dd></div>
      </dl>
      <ul className="issue-list" aria-label="Repair task notes">
        <li className="issue info"><Info size={14} /><b>window</b><span>{assessment.windowNote}.</span></li>
        {assessment.notes.map(note => <li key={note} className="issue info"><Info size={14} /><b>note</b><span>{note}</span></li>)}
      </ul>
      <div className="form-actions">
        <button className="primary-button" aria-label="Create repair task" disabled={mutation.state === "validating"}
          onClick={() => void startPreview()}><Hammer size={14} /> Create repair task</button>
        <button className="secondary-button" onClick={() => { mutation.reset(); onMessage(""); }}>Clear</button>
        {mutation.state === "validating" && <span className="filter-note" role="status"><RefreshCw size={12} className="spin" /> Validating the task…</span>}
      </div>
      {mutation.permission === "unauthorized" && <div className="locked-state" role="status"><Lock size={18} /><div>
        <b>A valid API key is required</b><p>{mutation.error ?? "Repair tasks are authorized writes."} Open Session access with a valid API key and try again.</p></div></div>}
      {mutation.error && mutation.permission !== "unauthorized" && <div className="error-state" role="alert"><AlertTriangle size={18} />
        <div><b>{mutation.permission === "protected" ? "Write protected" : "Request rejected"}</b><p>{mutation.error}</p></div></div>}
    </>}

    {mutation.preview && <RepairPreview preview={mutation.preview} warnings={mutation.warnings}
      submitting={mutation.state === "queued"} onConfirm={() => void mutation.submit()} onEdit={mutation.edit} />}

    {envelope && <div className="quality-queued">
      <ul className="issue-list" aria-label="Queued repair task">
        <li className="issue info"><CheckCircle2 size={14} /><b>queued</b>
          <span>Task <CopyId value={envelope.task_id} /> queued {envelope.window_count} window(s) as {queuedRunIds.length ? queuedRunIds.map(id => <CopyId key={id} value={id} />) : "—"}.</span></li>
      </ul>
      {linkableRunId && (linked
        ? <p className="filter-note"><CheckCircle2 size={12} /> Run <span className="mono">{linked}</span> is recorded as resolving this finding.</p>
        : <div className="form-actions">
          <button className="secondary-button" onClick={() => setConfirmEnvelope(envelope)}>Link run to finding</button>
          <button className="secondary-button" onClick={() => { setDismissed(true); onChanged(); }}>Not now</button>
          <span className="filter-note">Linking records the run as the resolution; the run outcome still decides whether the data is repaired.</span>
        </div>)}
      {dismissed && !linked && <p className="filter-note">The finding stays in its current handling state until a run is linked.</p>}
    </div>}

    {confirmEnvelope && <ConfirmDialog title="Record this run as resolving the finding?"
      detail="The selected run will be linked to this finding and the finding marked resolved. The state change is additive; the run receipt is not modified."
      confirmLabel="Mark resolved by run" onConfirm={() => void linkRun()} onCancel={() => setConfirmEnvelope(null)} />}
  </>;
}

function RepairPreview({ preview, warnings, submitting, onConfirm, onEdit }: {
  preview: TaskPreview;
  warnings: Array<{ code: string; message: string }>;
  submitting: boolean;
  onConfirm: () => void;
  onEdit: () => void;
}) {
  const errors: ValidationIssue[] = preview.validation.errors;
  const capacity = preview.capacity;
  return <section className="panel preview-panel quality-repair" aria-label="Repair task preview">
    <PanelHeading eyebrow="Validation preview" title="Review before submitting"
      action={<StatusBadge tone={preview.submittable ? "good" : "bad"}>
        {preview.submittable ? "Ready to submit" : errors.length ? "Blocked by validation" : "Blocked by capacity"}
      </StatusBadge>} />
    <div className="preview-grid">
      <article className="preview-card"><CalendarClock size={16} /><b>{preview.plan?.window_count ?? 0}</b>
        <span>half-open window(s)</span><small>{preview.plan?.reason ?? preview.task.run_kind} · {preview.plan?.semantics ?? "half-open"}</small></article>
      <article className="preview-card"><Database size={16} /><b>{preview.task.dataset_id}</b>
        <span>{preview.task.provider} · {preview.task.timeframe ?? "—"}</span>
        <small className="mono">{preview.plan?.plan_id?.slice(0, 16) ?? "—"}</small></article>
      <article className={`preview-card${capacity.write_status === "protected" ? " protected" : ""}`}>
        {capacity.write_status === "protected" ? <ShieldAlert size={16} /> : <CheckCircle2 size={16} />}
        <b>{capacity.status}</b><span>capacity · {capacity.free_ratio == null ? "unknown" : `${(capacity.free_ratio * 100).toFixed(1)}% free`}</span>
        <small>{capacity.policy} policy · {capacity.requested_days} day(s) requested</small></article>
      <article className="preview-card"><CalendarClock size={16} /><b>{preview.task.run_scope}</b>
        <span>run scope · {preview.snapshot ? `${preview.snapshot.part_count} input part(s)` : "no snapshot required"}</span>
        <small className="mono">{preview.snapshot?.input_snapshot_id?.slice(0, 16) ?? "—"}</small></article>
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
      <span className="filter-note">Queued work returns a run id; the repair is only final once the worker publishes it.</span>
    </div>
  </section>;
}
