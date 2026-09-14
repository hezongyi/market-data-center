import { TimeDisplay, usePreferences } from "../preferences";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, ChevronLeft, ChevronRight, CircleSlash, Hammer, Lock, Search } from "lucide-react";
import { CopyId, DataTable, EmptyState, ErrorState, LoadingSkeleton, PanelHeading, StatusBadge } from "../components/ui";
import { useQuery } from "../hooks";
import type {
  ApiMeta, Bar, BarsQuery, CoverageReport, EconomicCoverage, EconomicObservation, EconomicQuery,
  EconomicQueryMode, MaintenanceTaskRequest, MarketBarsCoverageReport, MarketBarsQuery, RecipeInfo,
} from "../lib/api";
import { createServices, type Services } from "../services";
import "./ExplorerPage.css";

export type ExplorerMode = "bars" | "market" | "economic";

type ExplorerRequest =
  | { mode: "bars"; provider: string; symbol: string; timeframe: string; start: string; end: string; pageSize: number }
  | { mode: "market"; provider: string; symbol: string; timeframe: string; priceBasis: string; recipeId: string; recipeVersion: string; start: string; end: string; pageSize: number }
  | { mode: "economic"; provider: string; seriesId: string; queryMode: EconomicQueryMode; asof: string; start: string; end: string; pageSize: number };

type ExplorerResult = {
  key: string;
  mode: ExplorerMode;
  bars: Bar[];
  observations: EconomicObservation[];
  meta: ApiMeta;
  warnings: string[];
  barsCoverage: CoverageReport | null;
  marketCoverage: MarketBarsCoverageReport | null;
  economicCoverage: EconomicCoverage | null;
};

type CoverageView = {
  kind: ExplorerMode;
  scope: string | null;
  readiness: string | null;
  summaryOnly: boolean;
  // Why the API did not compute the governance fields (issue #85); null when it did.
  detailUnavailable: string | null;
  gapCount: number | null;
  readyIntervals: Array<{ start: string; end: string; semantics: string }>;
  rowCount: number | null;
  minTs: string | null;
  maxTs: string | null;
  recipeId: string | null;
  recipeVersion: string | null;
  recipeStatus: string | null;
  priceBasis: string | null;
  inputSnapshotIds: string[];
  qualityStatus: string | null;
  latestCompleteBoundary: string | null;
};

const barColumns: ColumnDef<Bar>[] = [
  { accessorKey: "bar_ts", header: "Timestamp", cell: info => <TimeDisplay value={String(info.getValue())} /> },
  { accessorKey: "open", header: "Open" },
  { accessorKey: "high", header: "High" },
  { accessorKey: "low", header: "Low" },
  { accessorKey: "close", header: "Close" },
  { accessorKey: "volume", header: "Volume", cell: info => String(info.getValue() ?? "—") },
];
const economicColumns: ColumnDef<EconomicObservation>[] = [
  { accessorKey: "observation_date", header: "Observation" },
  { accessorKey: "value", header: "Value", cell: info => String(info.getValue() ?? "—") },
  { accessorKey: "release_ts", header: "Release", cell: info => <TimeDisplay value={String(info.getValue() ?? "")} /> },
  { accessorKey: "frequency", header: "Frequency", cell: info => String(info.getValue() ?? "—") },
  { accessorKey: "units", header: "Units", cell: info => String(info.getValue() ?? "—") },
];

const utc = (value?: string | null) => <TimeDisplay value={value} />;
const dayStart = (value: string) => value ? new Date(`${value}T00:00:00Z`).toISOString() : undefined;
const dayEnd = (value: string) => value ? new Date(`${value}T23:59:59.999Z`).toISOString() : undefined;

export function ExplorerPage({ apiKey, initialMode = "bars", services, onMaintenance }: {
  apiKey: string;
  initialMode?: ExplorerMode;
  services?: Services;
  onMaintenance?: (draft?: MaintenanceTaskRequest) => void;
}) {
  const { t } = usePreferences();
  const fallbackServices = useMemo(() => createServices(apiKey), [apiKey]);
  const svc = services ?? fallbackServices;

  const [mode, setMode] = useState<ExplorerMode>(initialMode);
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("1d");
  const [marketTimeframe, setMarketTimeframe] = useState("");
  const [priceBasis, setPriceBasis] = useState("");
  const [recipeId, setRecipeId] = useState("");
  const [recipeVersion, setRecipeVersion] = useState("1");
  const [seriesId, setSeriesId] = useState("PAYEMS");
  const [economicMode, setEconomicMode] = useState<EconomicQueryMode>("current");
  const [asof, setAsof] = useState("2026-09-10T00:00:00Z");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [pageSize, setPageSize] = useState(1000);

  const [formError, setFormError] = useState("");
  const [taskNotice, setTaskNotice] = useState("");
  const [request, setRequest] = useState<ExplorerRequest | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nonce, setNonce] = useState(0);

  // Recipe, price basis and target timeframe are only ever taken from the
  // capability read model; when it is unavailable the fields stay free text.
  const capabilities = useQuery(() => svc.catalog.capabilities(), [svc]);
  const recipes = useMemo(
    () => (capabilities.data?.recipes ?? []).filter(recipe => recipe.output_dataset === "market_bars"),
    [capabilities.data],
  );
  const selectedRecipe: RecipeInfo | undefined =
    recipes.find(recipe => recipe.recipe_id === recipeId && recipe.recipe_version === recipeVersion);
  const providerCapability = capabilities.data?.providers.find(item => item.provider === provider.trim());
  // A recipe may leave its price bases unrestricted; the provider capability
  // then publishes the values that can actually be read, and only when neither
  // publishes one does the field fall back to explicit free text.
  const recipePriceBases = selectedRecipe?.price_bases ?? [];
  const priceBases = useMemo(
    () => recipePriceBases.length ? recipePriceBases : providerCapability?.price_bases ?? [],
    [recipePriceBases, providerCapability],
  );

  useEffect(() => {
    const first = recipes[0];
    if (!first || recipeId) return;
    setRecipeId(first.recipe_id);
    setRecipeVersion(first.recipe_version);
    setMarketTimeframe(current => current || first.target_timeframe);
  }, [recipes, recipeId]);

  useEffect(() => {
    if (priceBasis || !priceBases.length) return;
    setPriceBasis(priceBases[0]);
  }, [priceBasis, priceBases]);

  const loadRequest = async (active: ExplorerRequest, nextCursor: string | null, key: string): Promise<ExplorerResult> => {
    if (active.mode === "bars") {
      const query: BarsQuery = {
        provider: active.provider,
        symbol: active.symbol,
        timeframe: active.timeframe,
        ...(active.start ? { start: dayStart(active.start) } : {}),
        ...(active.end ? { end: dayEnd(active.end) } : {}),
      };
      const [page, coverage] = await Promise.all([
        svc.query.bars(query, nextCursor, active.pageSize),
        svc.catalog.providerCoverage({
          provider: active.provider, symbol: active.symbol, timeframe: active.timeframe,
          ...(active.start && active.end ? { start: dayStart(active.start), end: dayEnd(active.end) } : {}),
        }),
      ]);
      return {
        key, mode: active.mode, bars: page.items, observations: [], meta: page.meta, warnings: page.warnings,
        barsCoverage: coverage, marketCoverage: null, economicCoverage: null,
      };
    }

    if (active.mode === "market") {
      const query: MarketBarsQuery = {
        provider: active.provider,
        symbol: active.symbol,
        timeframe: active.timeframe,
        price_basis: active.priceBasis,
        recipe_id: active.recipeId,
        recipe_version: active.recipeVersion,
        ...(active.start ? { start: dayStart(active.start) } : {}),
        ...(active.end ? { end: dayEnd(active.end) } : {}),
      };
      const [page, coverage] = await Promise.all([
        svc.query.marketBars(query, nextCursor, active.pageSize),
        svc.catalog.marketCoverage({
          provider: active.provider, symbol: active.symbol, timeframe: active.timeframe,
          price_basis: active.priceBasis, recipe_id: active.recipeId, recipe_version: active.recipeVersion,
          ...(active.start && active.end ? { start: dayStart(active.start), end: dayEnd(active.end) } : {}),
        }),
      ]);
      return {
        key, mode: active.mode, bars: page.items, observations: [], meta: page.meta, warnings: page.warnings,
        barsCoverage: null, marketCoverage: coverage as MarketBarsCoverageReport, economicCoverage: null,
      };
    }

    const query: EconomicQuery = {
      provider: active.provider,
      series_id: active.seriesId,
      mode: active.queryMode,
      ...(active.start ? { start: active.start } : {}),
      ...(active.end ? { end: active.end } : {}),
      ...(active.queryMode === "pit" ? { asof_ts: active.asof } : {}),
    };
    const [page, coverage] = await Promise.all([
      svc.query.economic(query, nextCursor, active.pageSize),
      svc.catalog.economicCoverage({ provider: active.provider, series_id: active.seriesId }),
    ]);
    return {
      key, mode: active.mode, bars: [], observations: page.items, meta: page.meta, warnings: page.warnings,
      barsCoverage: null, marketCoverage: null, economicCoverage: coverage,
    };
  };

  const requestKey = request ? `${JSON.stringify(request)}|${cursor ?? ""}|${nonce}` : "";
  const page = useQuery(
    () => request ? loadRequest(request, cursor, requestKey) : Promise.resolve(null),
    [svc, requestKey],
  );
  // A response only describes the exact request it answered; anything older is
  // dropped instead of being shown next to newer filters.
  const result = page.data && page.data.key === requestKey ? page.data : null;

  const reset = (next: ExplorerMode) => {
    setMode(next);
    setRequest(null);
    setCursor(null);
    setHistory([]);
    setFormError("");
    setTaskNotice("");
  };

  const validate = (): string => {
    if (start && end && start > end) return "Start date must not be after end date.";
    if (mode === "economic" && economicMode === "pit" && !asof) return "PIT mode requires an as-of timestamp.";
    if (mode === "economic" && !seriesId.trim()) return "Series ID is required.";
    if (mode !== "economic" && (!provider.trim() || !symbol.trim())) return "Provider and symbol are required.";
    if (mode === "bars" && !timeframe.trim()) return "Timeframe is required.";
    if (mode === "market") {
      if (!marketTimeframe.trim()) return "Timeframe is required for a market bars query.";
      if (!recipeId.trim() || !recipeVersion.trim()) return "Recipe and recipe version are required for a market bars query.";
      if (!priceBasis.trim()) return "Price basis is required for a market bars query; the platform never assumes one.";
    }
    return "";
  };

  const buildRequest = (): ExplorerRequest =>
    mode === "bars"
      ? { mode, provider: provider.trim(), symbol: symbol.trim(), timeframe: timeframe.trim(), start, end, pageSize }
      : mode === "market"
        ? {
          mode, provider: provider.trim(), symbol: symbol.trim(), timeframe: marketTimeframe.trim(),
          priceBasis: priceBasis.trim(), recipeId: recipeId.trim(), recipeVersion: recipeVersion.trim(), start, end, pageSize,
        }
        : { mode, provider: "fred", seriesId: seriesId.trim(), queryMode: economicMode, asof, start, end, pageSize };

  const load = (event?: FormEvent) => {
    event?.preventDefault();
    const error = validate();
    if (error) { setFormError(error); return; }
    setFormError("");
    setTaskNotice("");
    setHistory([]);
    setCursor(null);
    setNonce(value => value + 1);
    setRequest(buildRequest());
  };

  const next = () => {
    const nextCursor = result?.meta.next_cursor ?? null;
    if (!nextCursor) return;
    setHistory(current => [...current, cursor]);
    setCursor(nextCursor);
  };
  const previous = () => {
    const prior = history.at(-1) ?? null;
    setHistory(current => current.slice(0, -1));
    setCursor(prior);
  };

  const view = result ? coverageView(result) : null;
  const plan = useMemo(() => buildPlan(request, view), [request, view]);
  const rows: Array<Bar | EconomicObservation> = result ? (result.mode === "economic" ? result.observations : result.bars) : [];
  const loading = !!request && page.status === "loading" && !result;

  const requestTask = () => {
    // Hand the exact selector and half-open window to the maintenance
    // workspace so the operator never retypes what is already on screen; the
    // workspace still revalidates coverage and capacity before queueing.
    const draft = request ? buildTaskDraft(request, view, plan.kind) : null;
    if (!draft) {
      setTaskNotice(plan.window
        ? `No bounded window is on screen, so ${plan.kind || "a maintenance"} task cannot be prefilled for ${plan.selector}. Set Query start date and Query end date, or load coverage that publishes an observed window.`
        : `No selector is on screen yet. Load coverage or run a query before planning a maintenance task.`);
      return;
    }
    if (onMaintenance) { onMaintenance(draft); return; }
    setTaskNotice(`Maintenance is not wired into this view. Open the Maintenance workspace and plan ${plan.kind} for ${plan.selector} over ${plan.window}.`);
  };

  const submitLabel = mode === "bars" ? "Load coverage" : mode === "market" ? "Load market bars" : "Load observations";

  return <div className="explorer-page">
    <section className="panel" aria-label="Query">
      <PanelHeading eyebrow={t("Published data")} title={t("Data explorer")}
        action={<StatusBadge tone={capabilities.status === "error" ? "warn" : "neutral"}>
          {mode === "market" ? "Derived market_bars" : mode === "bars" ? "Raw provider_bars" : "Economic observations"}
        </StatusBadge>} />
      <div className="segmented" role="group" aria-label="Explorer dataset">
        <button className={mode === "bars" ? "active" : ""} onClick={() => reset("bars")}>{t("Provider bars")}</button>
        <button className={mode === "market" ? "active" : ""} onClick={() => reset("market")}>{t("Market bars")}</button>
        <button className={mode === "economic" ? "active" : ""} onClick={() => reset("economic")}>{t("Economic")}</button>
      </div>

      <form className="explorer-toolbar" onSubmit={event => load(event)}>
        {mode === "economic" ? <>
          <label>{t("Series ID")}<input aria-label={t("Series ID")} value={seriesId} onChange={event => setSeriesId(event.target.value)} /></label>
          <label>{t("Query mode")}<select aria-label={t("Query mode")} value={economicMode} onChange={event => setEconomicMode(event.target.value as EconomicQueryMode)}><option value="current">{t("Current")}</option><option value="pit">{t("Point in time")}</option></select></label>
          {economicMode === "pit" && <label>{t("As-of timestamp")}<input aria-label={t("As-of timestamp")} value={asof} onChange={event => setAsof(event.target.value)} /></label>}
        </> : <>
          <label>Provider<input aria-label="Provider" value={provider} onChange={event => setProvider(event.target.value)} /></label>
          <label>Symbol<input aria-label="Symbol" value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
          {mode === "bars"
            ? <label>Timeframe<input aria-label="Timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)} /></label>
            : <>
              <label>Timeframe<input aria-label="Timeframe" value={marketTimeframe} onChange={event => setMarketTimeframe(event.target.value)} /></label>
              {recipes.length
                ? <label>Recipe<select aria-label="Recipe" value={recipeId} onChange={event => {
                  const nextRecipe = recipes.find(recipe => recipe.recipe_id === event.target.value);
                  setRecipeId(event.target.value);
                  if (nextRecipe) {
                    setRecipeVersion(nextRecipe.recipe_version);
                    setMarketTimeframe(nextRecipe.target_timeframe);
                    setPriceBasis(nextRecipe.price_bases[0] ?? providerCapability?.price_bases[0] ?? "");
                  }
                }}>
                  {!recipeId && <option value="">{t("Select a published recipe")}</option>}
                  {recipes.map(recipe => <option key={`${recipe.recipe_id}@${recipe.recipe_version}`} value={recipe.recipe_id}>{recipe.recipe_id}</option>)}
                </select></label>
                : <label>Recipe<input aria-label="Recipe" value={recipeId} onChange={event => setRecipeId(event.target.value)} placeholder="utc-24x7-1m-to-1d-ohlcv" /></label>}
              <label>Recipe version<input aria-label="Recipe version" value={recipeVersion} onChange={event => setRecipeVersion(event.target.value)} /></label>
              {priceBases.length
                ? <label>Price basis<select aria-label="Price basis" value={priceBasis} onChange={event => setPriceBasis(event.target.value)}>
                  {!priceBases.includes(priceBasis) && <option value={priceBasis}>{priceBasis || "Select a published price basis"}</option>}
                  {priceBases.map(basis => <option key={basis} value={basis}>{basis}</option>)}
                </select></label>
                : <label>Price basis<input aria-label="Price basis" value={priceBasis} onChange={event => setPriceBasis(event.target.value)} placeholder="raw" /></label>}
            </>}
        </>}
        <label>Start<input aria-label="Query start date" type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>End<input aria-label="Query end date" type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
        <label>Page size<select aria-label="Page size" value={pageSize} onChange={event => setPageSize(Number(event.target.value))}><option value={1000}>1,000</option><option value={500}>500</option><option value={100}>100</option><option value={25}>25</option><option value={2}>2</option></select></label>
        <button className="primary-button" type="submit" disabled={loading}><Search size={15} />{loading ? "Loading…" : submitLabel}</button>
        {mode === "market" && <span className="selector-hint">
          <CircleSlash size={12} />
          {capabilities.status === "error"
            ? "Capability read model unavailable: recipe, version and price basis must be entered explicitly and are validated by the API."
            : selectedRecipe
              ? `${selectedRecipe.input_dataset} ${selectedRecipe.source_timeframe} → ${selectedRecipe.target_timeframe} · ${selectedRecipe.partial_bucket_policy} partial buckets · ${selectedRecipe.materialization} · price bases ${recipePriceBases.length ? "published by the recipe" : priceBases.length ? "published by the provider capability" : "not published"}`
              : "Select a published recipe; the console never invents a recipe id or price basis."}
        </span>}
        {mode === "market" && capabilities.status !== "error" && !priceBases.length && <span className="selector-hint">
          <AlertTriangle size={12} /> Neither the recipe nor the provider capability publishes a price basis for this selector, so it must be entered explicitly; the API validates the value.
        </span>}
      </form>

      {formError ? <ErrorState message={formError} />
        : result && view ? <CoverageStrip view={view} />
          : loading ? <LoadingSkeleton rows={2} />
            : page.status === "error" ? (page.permission === "unauthorized"
              ? <div className="locked-state" role="status"><Lock size={18} /><div><b>{t("Not authorized")}</b><p>{page.error}</p></div></div>
              : <ErrorState message={page.error ?? "Unable to load data"} onRetry={page.reload} />)
              : <EmptyState title={mode === "bars" ? "Choose an instrument" : mode === "market" ? "Choose a derived selector" : "Choose an economic series"}
                detail="Only immutable published snapshots are queried." />}
    </section>

    {result && <section className="panel" aria-label={t("Query result")}>
      <PanelHeading eyebrow="Snapshot"
        title={result.mode === "bars" ? "Provider bars" : result.mode === "market" ? "Market bars" : "Economic observations"}
        action={<div className="meta-badges">
          {result.meta.snapshot_id ? <CopyId value={String(result.meta.snapshot_id)} /> : "no snapshot"}
          <StatusBadge tone="neutral">{(result.meta.schema_versions ?? []).join(", ") || "schema unknown"}</StatusBadge>
          <StatusBadge tone="neutral">{result.meta.count ?? rows.length} row(s) in page</StatusBadge>
        </div>} />

      {result.warnings.length > 0 && <ul className="issue-list" aria-label={t("Query warnings")}>
        {result.warnings.map(warning => <li className="issue warning" key={warning}>
          <AlertTriangle size={14} /><b>query warning</b>
          <span>{warning === "unbounded_query" ? "Unbounded query: keep cursor paging or narrow the window." : warning}</span>
          <code>{warning}</code>
        </li>)}
      </ul>}

      {result.mode === "economic"
        ? <DataTable data={result.observations} columns={economicColumns} empty="No observations in this page." />
        : <DataTable data={result.bars} columns={barColumns} empty="No bars in this page." />}

      <div className="pagination">
        <button aria-label={t("Previous page")} onClick={previous} disabled={!history.length}><ChevronLeft size={15} /> Previous</button>
        <span>Page {history.length + 1} · {rows.length} rows</span>
        <button aria-label={t("Next page")} onClick={next} disabled={!result.meta.next_cursor}>Next <ChevronRight size={15} /></button>
      </div>
      <p className="filter-note">Cursor paging is bound to this selector and page size; changing a filter restarts at page 1.</p>
    </section>}

    {result && view && <section className="panel" aria-label={t("Coverage")}>
      <PanelHeading eyebrow="Coverage" title={t("Ready intervals and gaps")}
        action={<StatusBadge tone={view.summaryOnly ? "warn" : view.readiness === "ready" ? "good" : view.readiness ? "bad" : "neutral"}>
          {view.summaryOnly ? "Readiness unknown (summary only)"
            : view.readiness ?? (view.detailUnavailable ? "Readiness not computed" : "Readiness not published")}
        </StatusBadge>} />

      {result.mode === "economic" && <p className="filter-note"><AlertTriangle size={12} /> The economic coverage endpoint publishes physical coverage only: no readiness status, ready intervals or gap count exist for this selector.</p>}

      <dl className="detail-list">
        <div><dt>{t("Coverage scope")}</dt><dd>{view.scope
          ?? <Unavailable label={view.detailUnavailable ? "Scope not computed" : "Scope not published"} />}</dd></div>
        <div><dt>{t("Readiness status")}</dt><dd>{view.summaryOnly
          ? `unknown · summary-only response`
          : view.readiness
            ?? <Unavailable label={view.detailUnavailable ? "Readiness not computed" : "Readiness not published"} />}</dd></div>
        <div><dt>{t("Gap count")}</dt><dd>{view.gapCount == null
          ? <Unavailable label={view.detailUnavailable ? "Not computed by this response" : "Not evaluated by this response"} />
          : view.gapCount}</dd></div>
        <div><dt>{t("Ready intervals")}</dt><dd>{view.readyIntervals.length}</dd></div>
        <div><dt>{t("Rows in selector")}</dt><dd>{view.rowCount == null ? "—" : view.rowCount.toLocaleString()}</dd></div>
        <div><dt>{view.kind === "economic" ? "Observation window" : "Observed window (UTC)"}</dt><dd className="mono">
          {view.kind === "economic"
            ? view.minTs ? <>{utc(view.minTs)} → {utc(view.maxTs)}</> : "—"
            : view.minTs ? <>{utc(view.minTs)} → {utc(view.maxTs)}</> : "—"}</dd></div>
        {view.kind === "market" && <>
          <div><dt>Recipe</dt><dd className="mono">{view.recipeId ? `${view.recipeId}@${view.recipeVersion}` : "—"}</dd></div>
          <div><dt>Recipe status</dt><dd>{view.recipeStatus ?? <Unavailable label="Not published" />}</dd></div>
          <div><dt>Price basis</dt><dd>{view.priceBasis ?? "—"}</dd></div>
          <div><dt>{t("Input snapshots")}</dt><dd className="mono">{view.inputSnapshotIds.length ? view.inputSnapshotIds.map(id => <CopyId key={id} value={id} />) : "none published"}</dd></div>
        </>}
        {view.qualityStatus && <div><dt>Quality status</dt><dd>{view.qualityStatus}</dd></div>}
        {view.latestCompleteBoundary && <div><dt>{t("Latest complete boundary")}</dt><dd className="mono">{utc(view.latestCompleteBoundary)}</dd></div>}
      </dl>

      {view.readyIntervals.length > 0 && <ul className="coverage-intervals" aria-label={t("Ready intervals")}>
        {view.readyIntervals.map((interval, index) => <li key={`${interval.start}-${index}`}>
          <StatusBadge tone="good">ready</StatusBadge>
          <span className="mono">{utc(interval.start)} → {utc(interval.end)}</span>
          <span className="filter-note">{interval.semantics} interval</span>
        </li>)}
      </ul>}

      {(view.summaryOnly || view.detailUnavailable) && <p className="filter-note" role="status"><AlertTriangle size={12} />{" "}
        {view.detailUnavailable
          ? `Detailed coverage was not computed: ${view.detailUnavailable}.`
          : "Summary-only coverage: the API returned coverage_scope=summary with readiness unknown, so this console claims no per-interval readiness for the selector."}</p>}

      <div className="coverage-actions">
        <button className="primary-button" onClick={requestTask}><Hammer size={14} /> Create task from coverage</button>
        <span className="filter-note">{plan.note}</span>
      </div>
      {taskNotice && <p className="inline-warning" role="status"><AlertTriangle size={14} /> {taskNotice}</p>}
    </section>}
  </div>;
}

const Unavailable = ({ label }: { label: string }) =>
  <span className="unavailable"><CircleSlash size={11} /> {label}</span>;

function CoverageStrip({ view }: { view: CoverageView }) {
  const entries: Array<[string, string]> = view.kind === "economic"
    ? [["Scope", view.scope ?? "summary"], ["Rows", view.rowCount?.toLocaleString() ?? "—"],
      ["First", view.minTs ?? "—"], ["Last", view.maxTs ?? "—"], ["Readiness", "not published"]]
    : [["Scope", view.scope ?? "not published"], ["Readiness", view.summaryOnly ? "unknown (summary)" : view.readiness ?? "not published"],
      ["Gap count", view.gapCount == null ? "not evaluated" : String(view.gapCount)],
      ["Ready intervals", String(view.readyIntervals.length)], ["Rows", view.rowCount?.toLocaleString() ?? "—"]];
  return <div className="coverage-strip">
    {entries.map(([label, value]) => <div key={label}><small>{label}</small><b>{value}</b></div>)}
  </div>;
}

function coverageView(result: ExplorerResult): CoverageView | null {
  if (result.mode === "bars" && result.barsCoverage) {
    const report = result.barsCoverage;
    return {
      kind: "bars",
      scope: report.coverage_scope ?? null,
      readiness: report.readiness_status ?? null,
      summaryOnly: report.coverage_scope === "summary" || report.readiness_status === "unknown",
      detailUnavailable: report.coverage_detail_unavailable ?? null,
      gapCount: report.gap_count === undefined ? null : report.gap_count,
      readyIntervals: report.ready_intervals ?? [],
      rowCount: report.row_count ?? null,
      minTs: report.min_ts ?? null,
      maxTs: report.max_ts ?? null,
      recipeId: null, recipeVersion: null, recipeStatus: null, priceBasis: null,
      inputSnapshotIds: [],
      qualityStatus: report.quality_status ?? null,
      latestCompleteBoundary: report.latest_complete_boundary ?? null,
    };
  }
  if (result.mode === "market" && result.marketCoverage) {
    const report = result.marketCoverage;
    return {
      kind: "market",
      scope: report.coverage_scope ?? null,
      readiness: report.readiness_status ?? null,
      summaryOnly: report.coverage_scope === "summary" || report.readiness_status === "unknown",
      detailUnavailable: null,
      gapCount: report.gap_count === undefined ? null : report.gap_count,
      readyIntervals: report.ready_intervals ?? [],
      rowCount: report.row_count ?? null,
      minTs: report.min_ts ?? null,
      maxTs: report.max_ts ?? null,
      recipeId: report.recipe_id ?? null,
      recipeVersion: report.recipe_version ?? null,
      recipeStatus: report.recipe_status ?? null,
      priceBasis: report.price_basis ?? null,
      inputSnapshotIds: report.input_snapshot_ids ?? [],
      qualityStatus: report.quality_status ?? null,
      latestCompleteBoundary: report.latest_complete_boundary ?? null,
    };
  }
  if (result.mode === "economic" && result.economicCoverage) {
    const report = result.economicCoverage;
    return {
      kind: "economic",
      scope: null,
      readiness: null,
      summaryOnly: false,
      detailUnavailable: null,
      gapCount: null,
      readyIntervals: [],
      rowCount: report.row_count ?? null,
      minTs: report.min_date ?? null,
      maxTs: report.max_date ?? null,
      recipeId: null, recipeVersion: null, recipeStatus: null, priceBasis: null,
      inputSnapshotIds: [],
      qualityStatus: null,
      latestCompleteBoundary: null,
    };
  }
  return null;
}

function buildTaskDraft(request: ExplorerRequest, view: CoverageView | null, kind: string): MaintenanceTaskRequest | null {
  const start = request.start ? dayStart(request.start) : view?.minTs ?? undefined;
  const end = request.end ? dayEnd(request.end) : view?.maxTs ?? undefined;
  if (!start || !end) return null;
  const base = {
    run_kind: (kind || "backfill") as MaintenanceTaskRequest["run_kind"],
    run_scope: "maintenance" as MaintenanceTaskRequest["run_scope"],
    start, end,
  };
  if (request.mode === "economic") {
    return { ...base, dataset_id: "economic_observations", provider: "fred", series_id: request.seriesId };
  }
  if (request.mode === "market") {
    return { ...base, dataset_id: "market_bars", provider: request.provider, symbol: request.symbol,
             timeframe: request.timeframe, recipe_id: request.recipeId, recipe_version: request.recipeVersion,
             price_basis: request.priceBasis };
  }
  return { ...base, dataset_id: "provider_bars", provider: request.provider, symbol: request.symbol,
           timeframe: request.timeframe };
}

function buildPlan(request: ExplorerRequest | null, view: CoverageView | null) {
  if (!request || !view) return { kind: "", selector: "", window: "", note: "" };
  const kind = request.mode === "market" ? "derive" : request.mode === "economic" ? "ingest"
    : (view.gapCount ?? 0) > 0 ? "gap_repair" : "backfill";
  const selector = request.mode === "economic"
    ? `${request.provider} · series ${request.seriesId}`
    : `${request.provider} · ${request.symbol} · ${request.timeframe}${request.mode === "market" ? ` · ${request.priceBasis}` : ""}`;
  const window = request.start && request.end
    ? `${request.start} → ${request.end} (UTC days)`
    : view.minTs && view.maxTs
      ? `${view.minTs} → ${view.maxTs} (observed coverage)`
      : "";
  if (!window) {
    return { kind, selector, window,
      note: `No bounded window is on screen. Set Query start date and Query end date, or load coverage that publishes an observed window, before planning a ${kind} task.` };
  }
  const caveats = [
    view.summaryOnly
      ? "Readiness is unknown for this selector (summary-only coverage), so the plan uses the observed range."
      : (view.gapCount ?? 0) > 0 ? `Coverage reports ${view.gapCount} gap(s), so the plan is a gap repair.` : "",
    "The Maintenance workspace revalidates coverage and capacity before queueing.",
  ].filter(Boolean);
  return { kind, selector, window, note: `Would plan ${kind} for ${selector} over ${window}. ${caveats.join(" ")}` };
}
