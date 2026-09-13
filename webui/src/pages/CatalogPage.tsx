import { useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import {
  AlertTriangle, ArrowRight, BadgeCheck, Boxes, CircleSlash, GitMerge, Hammer, Layers, Lock,
  Ruler, Search, Sigma, Table2, Waves,
} from "lucide-react";
import { DataTable, DetailDrawer, EmptyState, ErrorState, FilterBar, LoadingSkeleton, PanelHeading, StatusBadge } from "../components/ui";
import { useQuery } from "../hooks";
import type { CoverageReport, Dataset, EconomicCoverage, ProviderCapability, RecipeInfo } from "../lib/api";
import { createServices, type Services } from "../services";
import "./CatalogPage.css";

// Dataset definitions arrive through the capabilities read model.  The legacy
// ``datasets`` prop is still supported, but it cannot describe kind, quality,
// query modes or materialization, and the console says so instead of guessing.
type CatalogDataset = Dataset & {
  kind?: string;
  quality_profile?: string;
  query_modes?: string[];
  materialization_policy?: string;
  retention_policy?: Record<string, unknown>;
};

type CatalogPageProps = {
  apiKey: string;
  datasets: Dataset[];
  loading: boolean;
  onExplore: (dataset: Dataset) => void;
  services?: Services;
  onMaintenance?: () => void;
};

type ProbeResult = CoverageReport | EconomicCoverage;

const kindLabel = (dataset: CatalogDataset) => {
  if (dataset.kind === "derived") return "derived · recipe output";
  if (dataset.dataset_id === "economic_observations") return "raw · economic series";
  if (dataset.kind === "raw") return "raw · provider feed";
  return "";
};

const KindChip = ({ dataset }: { dataset: CatalogDataset }) => {
  const label = kindLabel(dataset);
  if (!label) return <span className="catalog-chip unavailable"><CircleSlash size={11} />Kind not published</span>;
  const Icon = dataset.kind === "derived" ? Sigma : dataset.dataset_id === "economic_observations" ? Table2 : Waves;
  return <span className={`catalog-chip ${dataset.kind}`}><Icon size={11} />{label}</span>;
};

const Unavailable = ({ label = "Not published by the API" }: { label?: string }) =>
  <span className="catalog-chip unavailable"><CircleSlash size={11} />{label}</span>;

const utc = (value?: string | null) =>
  value ? new Date(value).toLocaleString("en-GB", { timeZone: "UTC", hour12: false }) : "—";

const dayStart = (value: string) => value ? new Date(`${value}T00:00:00Z`).toISOString() : undefined;
const dayEnd = (value: string) => value ? new Date(`${value}T23:59:59.999Z`).toISOString() : undefined;

export function CatalogPage({ apiKey, datasets, loading, onExplore, services, onMaintenance }: CatalogPageProps) {
  // The page prefers the services handed down by the shell and only builds its
  // own domain services when it is mounted standalone.
  const fallbackServices = useMemo(() => createServices(apiKey), [apiKey]);
  const svc = services ?? fallbackServices;

  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CatalogDataset | null>(null);
  const [taskRequest, setTaskRequest] = useState("");

  const capabilities = useQuery(() => svc.catalog.capabilities(), [svc]);

  const registry = useMemo<CatalogDataset[]>(() => {
    const published = capabilities.data?.datasets as CatalogDataset[] | undefined;
    return published && published.length ? published : datasets;
  }, [capabilities.data, datasets]);

  const recipes = capabilities.data?.recipes ?? [];
  const providers = capabilities.data?.providers ?? [];
  const runKinds = capabilities.data?.run_kinds ?? [];

  const writeKindsFor = (datasetId: string) =>
    runKinds.filter(entry => entry.datasets.includes(datasetId)).map(entry => entry.run_kind);

  const filtered = useMemo(
    () => registry.filter(item => `${item.dataset_id} ${item.description} ${item.schema_version} ${item.kind ?? ""}`
      .toLowerCase().includes(query.toLowerCase())),
    [registry, query],
  );

  const requestMaintenance = (dataset: CatalogDataset) => {
    setTaskRequest(dataset.dataset_id);
    onMaintenance?.();
  };

  const columns: ColumnDef<CatalogDataset>[] = [
    { accessorKey: "dataset_id", header: "Dataset", cell: info => <b>{String(info.getValue())}</b> },
    { id: "kind", header: "Kind", cell: ({ row }) => <KindChip dataset={row.original} /> },
    { accessorKey: "schema_version", header: "Schema", cell: info => <StatusBadge tone="neutral">{String(info.getValue() ?? "—")}</StatusBadge> },
    { id: "partitioning", header: "Partitioning", cell: ({ row }) => row.original.partitioning?.length
      ? <span className="mono">{row.original.partitioning.join(" / ")}</span> : <Unavailable /> },
    { id: "quality", header: "Quality profile", cell: ({ row }) => row.original.quality_profile
      ? <span className="catalog-chip">profile · {row.original.quality_profile}</span> : <Unavailable /> },
    { id: "modes", header: "Query modes", cell: ({ row }) => row.original.query_modes?.length
      ? <span className="chip-row">{row.original.query_modes.map(mode =>
        <span className="catalog-chip" key={mode}><Search size={11} />{mode}</span>)}</span>
      : <Unavailable /> },
    { id: "materialization", header: "Materialization", cell: ({ row }) => row.original.materialization_policy
      ? <span className="catalog-chip"><Layers size={11} />{row.original.materialization_policy}</span> : <Unavailable /> },
    { id: "recipes", header: "Producing recipes", enableSorting: false, cell: ({ row }) => {
      const producing = recipes.filter(recipe => recipe.output_dataset === row.original.dataset_id);
      if (row.original.kind === "raw" && !producing.length) {
        return <span className="filter-note">Raw layer · persisted by ingest, not by a recipe.</span>;
      }
      return producing.length
        ? <span className="chip-row">{producing.map(recipe =>
          <span className="catalog-chip derived" key={`${recipe.recipe_id}@${recipe.recipe_version}`}>
            <Sigma size={11} />{recipe.recipe_id}@{recipe.recipe_version}</span>)}</span>
        : <Unavailable label="No registered recipe produces this dataset" />;
    } },
    { id: "actions", header: "Actions", enableSorting: false, cell: ({ row }) => {
      const kinds = writeKindsFor(row.original.dataset_id);
      return <div className="row-actions">
        <button className="table-action" onClick={event => { event.stopPropagation(); onExplore(row.original); }}>Open Explorer</button>
        {kinds.length > 0 && <button className="table-action" onClick={event => {
          event.stopPropagation();
          requestMaintenance(row.original);
        }}>Create maintenance task</button>}
      </div>;
    } },
  ];

  const capabilitiesUnavailable = capabilities.status === "error";
  const capabilitiesProtected = capabilities.data?.write_status === "protected";

  return <div className="catalog-page">
    <section className="panel" aria-label="Data catalog">
      <PanelHeading eyebrow="Registry" title="Data asset catalog"
        action={<div className="header-actions">
          <StatusBadge tone={capabilitiesUnavailable ? "bad" : capabilitiesProtected ? "warn" : "good"}>
            {capabilitiesUnavailable ? "Read model unavailable" : capabilitiesProtected ? "Writes protected" : "Registry published"}
          </StatusBadge>
        </div>} />
      <FilterBar>
        <label>Search datasets<input aria-label="Search datasets" value={query} onChange={event => setQuery(event.target.value)} placeholder="Dataset, schema, kind, description" /></label>
        <span>{filtered.length} dataset(s) · raw and derived layers are labelled separately</span>
      </FilterBar>

      {loading ? <LoadingSkeleton rows={4} /> : <DataTable data={filtered} columns={columns} empty="No matching datasets." onRowClick={setSelected} />}

      {capabilities.status === "loading" && <LoadingSkeleton rows={2} />}
      {capabilities.status === "error" && (capabilities.permission === "unauthorized"
        ? <div className="locked-state" role="status"><Lock size={18} /><div><b>Not authorized</b>
          <p>{capabilities.error} Dataset rows fall back to the legacy registry, which cannot describe kind, quality or lineage.</p></div></div>
        : <ErrorState message={`${capabilities.error ?? "Unable to load capabilities"}. Dataset rows fall back to the legacy registry, which cannot describe kind, quality or lineage.`} onRetry={capabilities.reload} />)}

      {taskRequest && !onMaintenance && <p className="inline-warning" role="status">
        <Hammer size={14} /> Maintenance is not wired into this view. Open the Maintenance workspace and pick {taskRequest} there to plan the task.
      </p>}
    </section>

    <section className="panel" aria-label="Raw to derived lineage">
      <PanelHeading eyebrow="Lineage" title="Raw → derived recipes"
        action={<StatusBadge tone={recipes.length ? "good" : "warn"}>{recipes.length ? `${recipes.length} registered` : "None published"}</StatusBadge>} />
      {capabilities.status === "loading" && <LoadingSkeleton rows={2} />}
      {capabilities.status === "error" && <EmptyState title="Recipe registry unavailable."
        detail="The capabilities read model did not answer, so no recipe relation is shown. Nothing is inferred locally." />}
      {capabilities.status !== "loading" && capabilities.status !== "error" && (recipes.length
        ? <ul className="lineage-list">{recipes.map(recipe => <LineageRow key={`${recipe.recipe_id}@${recipe.recipe_version}`} recipe={recipe} />)}</ul>
        : <EmptyState title="No registered recipes."
          detail="The platform publishes no transform recipe, so no raw → derived relation exists yet." />)}
    </section>

    <section className="panel" aria-label="Provider capability">
      <PanelHeading eyebrow="Capability read model" title="Provider capability"
        action={<StatusBadge tone={providers.length ? "good" : "warn"}>{providers.length ? `${providers.length} provider(s)` : "None published"}</StatusBadge>} />
      {capabilities.status === "loading" && <LoadingSkeleton rows={3} />}
      {capabilities.status === "error" && <EmptyState title="Provider capabilities unavailable."
        detail="Timeframes, price bases, windows and instruments are not rendered because the API did not publish them." />}
      {capabilities.status !== "loading" && capabilities.status !== "error" && (providers.length
        ? providers.map(provider => <ProviderCard key={provider.provider} provider={provider} />)
        : <EmptyState title="No provider capability published."
          detail="Registering a provider in the platform registry is what makes its selectors selectable." />)}
    </section>

    {selected && <DetailDrawer title={selected.dataset_id} onClose={() => setSelected(null)}>
      <DatasetDetail key={selected.dataset_id} dataset={selected} recipes={recipes} runKinds={runKinds}
        services={svc} onExplore={onExplore} onMaintenance={onMaintenance ? () => { setTaskRequest(selected.dataset_id); onMaintenance(); } : undefined} />
    </DetailDrawer>}
  </div>;
}

function LineageRow({ recipe }: { recipe: RecipeInfo }) {
  return <li className="lineage-row">
    <div className="lineage-io">
      <span className="catalog-chip raw">input · {recipe.input_dataset}</span>
      <ArrowRight size={13} />
      <span className="catalog-chip derived">output · {recipe.output_dataset}</span>
      <span className="mono">{recipe.recipe_id}@{recipe.recipe_version}</span>
    </div>
    <div className="lineage-meta">
      <span>{recipe.source_timeframe} → {recipe.target_timeframe} · {recipe.session_profile} session · {recipe.materialization}</span>
      <span className="chip-row">
        {recipe.price_bases.length
          ? recipe.price_bases.map(basis => <span className="catalog-chip" key={basis}><Ruler size={11} />price basis · {basis}</span>)
          : <Unavailable label="Price bases not published" />}
        <span className="catalog-chip">partial buckets · {recipe.partial_bucket_policy}</span>
        <span className="catalog-chip">missing input · {recipe.missing_input_policy}</span>
      </span>
    </div>
  </li>;
}

function ProviderCard({ provider }: { provider: ProviderCapability }) {
  const approved = provider.instruments?.filter(instrument => instrument.approved) ?? [];
  const instrumentColumns: ColumnDef<ProviderCapability["instruments"][number]>[] = [
    { accessorKey: "symbol", header: "Symbol", cell: info => <span className="mono">{String(info.getValue())}</span> },
    { accessorKey: "asset_class", header: "Asset class" },
    { accessorKey: "currency", header: "Currency" },
    { accessorKey: "session_profile", header: "Session profile", cell: info => <span className="mono">{String(info.getValue())}</span> },
    { id: "approved", header: "Approval", enableSorting: false, cell: ({ row }) => row.original.approved
      ? <span className="catalog-chip"><BadgeCheck size={11} />approved</span>
      : <span className="catalog-chip unavailable"><CircleSlash size={11} />not approved</span> },
  ];
  return <article className="provider-card">
    <div className="provider-head">
      <b className="mono">{provider.provider}</b>
      <StatusBadge tone={provider.instruments?.length ? "good" : "warn"}>
        {provider.instruments?.length ? `${approved.length} approved instrument(s)` : "No instruments"}
      </StatusBadge>
      <span className="filter-note">max window {provider.max_window_days ?? "—"} day(s)</span>
    </div>
    <dl className="provider-facts">
      <div><dt>Asset classes</dt><dd>{provider.asset_classes?.length
        ? <span className="chip-row">{provider.asset_classes.map(value => <span className="catalog-chip" key={value}><Boxes size={11} />{value}</span>)}</span>
        : <Unavailable />}</dd></div>
      <div><dt>Timeframes</dt><dd>{provider.timeframes?.length
        ? <span className="chip-row">{provider.timeframes.map(value => <span className="catalog-chip" key={value}>{value}</span>)}</span>
        : <Unavailable />}</dd></div>
      <div><dt>Maintenance timeframes</dt><dd>{provider.maintenance_timeframes?.length
        ? <span className="chip-row">{provider.maintenance_timeframes.map(value => <span className="catalog-chip" key={value}>{value}</span>)}</span>
        : <Unavailable />}</dd></div>
      <div><dt>Price bases</dt><dd>{provider.price_bases?.length
        ? <span className="chip-row">{provider.price_bases.map(value => <span className="catalog-chip" key={value}><Ruler size={11} />{value}</span>)}</span>
        : <Unavailable />}</dd></div>
      <div><dt>Max window days</dt><dd>{provider.max_window_days == null ? <Unavailable /> : String(provider.max_window_days)}</dd></div>
      <div><dt>Session profile</dt><dd>{provider.session_profile
        ? <span className="mono">{provider.session_profile}</span> : <Unavailable />}</dd></div>
    </dl>
    {approved.length > 0
      ? <DataTable data={provider.instruments} columns={instrumentColumns} empty="No instruments registered." />
      : <p className="filter-note"><CircleSlash size={12} /> No approved instrument is published for this provider; selectors cannot be validated from capability.</p>}
  </article>;
}

function DatasetDetail({ dataset, recipes, runKinds, services, onExplore, onMaintenance }: {
  dataset: CatalogDataset;
  recipes: RecipeInfo[];
  runKinds: Array<{ run_kind: string; datasets: string[] }>;
  services: Services;
  onExplore: (dataset: Dataset) => void;
  onMaintenance?: () => void;
}) {
  const producing = recipes.filter(recipe => recipe.output_dataset === dataset.dataset_id);
  const kinds = runKinds.filter(entry => entry.datasets.includes(dataset.dataset_id));
  return <>
    <dl className="detail-list">
      <div><dt>Kind</dt><dd><KindChip dataset={dataset} /></dd></div>
      <div><dt>Schema version</dt><dd className="mono">{dataset.schema_version}</dd></div>
      <div><dt>Description</dt><dd>{dataset.description}</dd></div>
      <div><dt>Partitioning</dt><dd className="mono">{dataset.partitioning?.join(" / ") || "—"}</dd></div>
      <div><dt>Quality profile</dt><dd>{dataset.quality_profile ? `profile · ${dataset.quality_profile}` : <Unavailable />}</dd></div>
      <div><dt>Query modes</dt><dd>{dataset.query_modes?.length ? dataset.query_modes.join(" · ") : <Unavailable />}</dd></div>
      <div><dt>Materialization</dt><dd>{dataset.materialization_policy ?? <Unavailable />}</dd></div>
      <div><dt>Writable by</dt><dd>{kinds.length ? kinds.map(entry => entry.run_kind).join(" · ") : <Unavailable label="No maintenance run kind targets this dataset" />}</dd></div>
    </dl>

    <section className="drawer-section">
      <h3><GitMerge size={14} /> Raw → derived relation</h3>
      {dataset.kind === "derived"
        ? (producing.length
          ? <ul className="lineage-list">{producing.map(recipe => <LineageRow key={`${recipe.recipe_id}@${recipe.recipe_version}`} recipe={recipe} />)}</ul>
          : <p className="filter-note">No registered recipe produces this dataset.</p>)
        : <p className="filter-note">{dataset.kind === "raw"
          ? "Raw layer: this dataset is persisted by ingest, not produced by a recipe."
          : "Kind is not published, so the lineage relation cannot be resolved."}</p>}
    </section>

    <CoverageProbe dataset={dataset} services={services} />

    <div className="form-actions">
      <button className="primary-button drawer-action" onClick={() => onExplore(dataset)}>Open data explorer</button>
      {onMaintenance && kinds.length > 0 && <button className="secondary-button" onClick={onMaintenance}>
        <Hammer size={14} /> Create maintenance task
      </button>}
    </div>
  </>;
}

function CoverageProbe({ dataset, services }: { dataset: CatalogDataset; services: Services }) {
  const economic = dataset.dataset_id === "economic_observations";
  const [provider, setProvider] = useState(economic ? "fred" : "fixture");
  const [symbol, setSymbol] = useState(economic ? "" : "UI_TEST");
  const [seriesId, setSeriesId] = useState(economic ? "PAYEMS" : "");
  const [timeframe, setTimeframe] = useState("1d");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [submitted, setSubmitted] = useState<{ key: string; nonce: number } | null>(null);

  const probe = useQuery<ProbeResult | null>(() => {
    if (!submitted) return Promise.resolve(null);
    if (economic) return services.catalog.economicCoverage({ provider: provider.trim() || "fred", series_id: seriesId.trim() });
    const window = start && end
      ? { start: dayStart(start)!, end: dayEnd(end)! }
      : {};
    return services.catalog.providerCoverage({ provider: provider.trim(), symbol: symbol.trim(), timeframe, ...window });
  }, [services, submitted?.key ?? "", submitted?.nonce ?? 0]);

  const load = () => {
    const key = JSON.stringify({ provider, symbol, seriesId, timeframe, start, end });
    setSubmitted(current => ({ key, nonce: (current?.nonce ?? 0) + 1 }));
  };

  const report = probe.data as CoverageReport | null;
  const summaryOnly = !!report && (report.coverage_scope === "summary" || report.readiness_status === "unknown");
  const probeLoading = !!submitted && probe.status === "loading";

  return <section className="drawer-section">
    <h3><Search size={14} /> Published coverage</h3>
    <div className="probe-form">
      <label>Provider<input aria-label="Catalog provider" value={provider} onChange={event => setProvider(event.target.value)} /></label>
      {economic
        ? <label>Series ID<input aria-label="Catalog series ID" value={seriesId} onChange={event => setSeriesId(event.target.value)} /></label>
        : <>
          <label>Symbol<input aria-label="Catalog symbol" value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
          <label>Timeframe<input aria-label="Catalog timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)} /></label>
        </>}
      {!economic && <>
        <label>Coverage start date<input aria-label="Catalog coverage start date" type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>Coverage end date<input aria-label="Catalog coverage end date" type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
      </>}
    </div>
    <button className="secondary-button drawer-action" onClick={load} disabled={probeLoading}>
      {probeLoading ? "Loading coverage…" : "Load coverage"}
    </button>
    {!economic && !(start && end) && <p className="filter-note"><AlertTriangle size={12} /> Set both coverage dates to let the API return ready intervals and gap counts instead of a physical summary.</p>}
    {probe.status === "error" && (probe.permission === "unauthorized"
      ? <div className="locked-state" role="status"><Lock size={16} /><div><b>Not authorized</b><p>{probe.error}</p></div></div>
      : <ErrorState message={probe.error ?? "Unable to load coverage"} onRetry={probe.reload} />)}
    {probe.data && (economic
      ? <dl className="detail-list compact">
        <div><dt>Rows</dt><dd>{(probe.data as EconomicCoverage).row_count.toLocaleString()}</dd></div>
        <div><dt>First observation</dt><dd className="mono">{(probe.data as EconomicCoverage).min_date ?? "—"}</dd></div>
        <div><dt>Last observation</dt><dd className="mono">{(probe.data as EconomicCoverage).max_date ?? "—"}</dd></div>
      </dl>
      : <dl className="detail-list compact">
        <div><dt>Coverage scope</dt><dd>{report?.coverage_scope ?? <Unavailable label="Scope not published" />}</dd></div>
        <div><dt>Readiness</dt><dd>{report?.readiness_status
          ? <StatusBadge tone={summaryOnly ? "warn" : report.readiness_status === "ready" ? "good" : "bad"}>
            {summaryOnly ? "unknown (summary only)" : report.readiness_status}</StatusBadge>
          : <Unavailable label="Readiness not published" />}</dd></div>
        <div><dt>Rows</dt><dd>{report?.row_count?.toLocaleString() ?? "—"}</dd></div>
        <div><dt>Gap count</dt><dd>{report?.gap_count == null ? <Unavailable label="Not evaluated" /> : report.gap_count}</dd></div>
        <div><dt>Ready intervals</dt><dd>{report?.ready_intervals?.length ?? 0}</dd></div>
        <div><dt>First / last bar</dt><dd className="mono">{report?.min_ts ? `${utc(report.min_ts)} → ${utc(report.max_ts)}` : "—"}</dd></div>
        {(report?.ready_intervals?.length ?? 0) > 0 && <div><dt>Ready interval list</dt><dd className="mono">
          {report?.ready_intervals?.map(interval => `${utc(interval.start)} → ${utc(interval.end)} (${interval.semantics})`).join(" · ")}
        </dd></div>}
      </dl>)}
    {summaryOnly && <p className="filter-note"><AlertTriangle size={12} /> Summary-only coverage: the API returned coverage_scope=summary with readiness unknown, so no per-interval readiness is claimed here.</p>}
  </section>;
}
