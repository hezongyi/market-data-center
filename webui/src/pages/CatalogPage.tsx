import { useEffect, useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { createDataCenterClient, type BarsCoverage, type Dataset, type EconomicCoverage } from "../lib/api";
import { DataTable, DetailDrawer, EmptyState, ErrorState, FilterBar, LoadingSkeleton, PanelHeading, StatusBadge } from "../components/ui";

type CatalogCoverage = BarsCoverage | EconomicCoverage;

type CatalogPageProps = {
  apiKey: string;
  datasets: Dataset[];
  loading: boolean;
  onExplore: (dataset: Dataset) => void;
};

export function CatalogPage({ apiKey, datasets, loading, onExplore }: CatalogPageProps) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Dataset | null>(null);
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("UI_TEST");
  const [timeframe, setTimeframe] = useState("1d");
  const [seriesId, setSeriesId] = useState("PAYEMS");
  const [coverage, setCoverage] = useState<CatalogCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(false);
  const [coverageError, setCoverageError] = useState("");

  useEffect(() => {
    if (!selected) return;
    const economic = selected.dataset_id === "economic_observations";
    setProvider(economic ? "fred" : "fixture");
    setSymbol(economic ? "" : "UI_TEST");
    setSeriesId(economic ? "PAYEMS" : "");
    setTimeframe("1d");
    setCoverage(null);
    setCoverageError("");
  }, [selected]);

  const filtered = useMemo(
    () => datasets.filter(item => `${item.dataset_id} ${item.description} ${item.schema_version}`.toLowerCase().includes(query.toLowerCase())),
    [datasets, query],
  );

  const loadCoverage = async () => {
    if (!selected) return;
    setCoverageLoading(true);
    setCoverageError("");
    try {
      const client = createDataCenterClient(apiKey);
      if (selected.dataset_id === "economic_observations") {
        if (!seriesId.trim()) {
          setCoverageError("Series ID is required.");
          return;
        }
        setCoverage((await client.economicCoverage({ provider: provider.trim(), series_id: seriesId.trim() })).data);
      } else {
        if (!provider.trim() || !symbol.trim()) {
          setCoverageError("Provider and symbol are required.");
          return;
        }
        setCoverage((await client.coverage({ provider: provider.trim(), symbol: symbol.trim(), timeframe })).data);
      }
    } catch (reason) {
      setCoverageError(reason instanceof Error ? reason.message : "Unable to load coverage");
    } finally {
      setCoverageLoading(false);
    }
  };

  const columns: ColumnDef<Dataset>[] = [
    { accessorKey: "dataset_id", header: "Dataset", cell: info => <b>{String(info.getValue())}</b> },
    { accessorKey: "schema_version", header: "Schema", cell: info => <StatusBadge tone="neutral">{String(info.getValue())}</StatusBadge> },
    { accessorKey: "description", header: "Description" },
    { id: "partitions", header: "Partitioning", cell: ({ row }) => row.original.partitioning.join(" / ") },
    {
      id: "coverage",
      header: "Provider / coverage",
      enableSorting: false,
      cell: ({ row }) => {
        const current = coverage?.dataset_id === row.original.dataset_id ? coverage : null;
        return current ? <span>{current.provider} · {current.row_count.toLocaleString()} rows</span> : <span className="muted-copy">Select to query</span>;
      },
    },
    { id: "explore", header: "Explore", enableSorting: false, cell: ({ row }) => <button className="table-action" onClick={event => { event.stopPropagation(); onExplore(row.original); }}>Open Explorer</button> },
  ];

  return <section className="panel">
    <PanelHeading eyebrow="Registry" title="Data catalog" />
    <FilterBar><label>Search datasets<input aria-label="Search datasets" value={query} onChange={event => setQuery(event.target.value)} placeholder="Dataset, schema, description" /></label><span>{filtered.length} datasets</span></FilterBar>
    {loading ? <LoadingSkeleton rows={4} /> : <DataTable data={filtered} columns={columns} empty="No matching datasets." onRowClick={setSelected} />}
    {selected && <DetailDrawer title={selected.dataset_id} onClose={() => setSelected(null)}>
      <DetailList values={{ "Schema version": selected.schema_version, Description: selected.description, Partitioning: selected.partitioning.join(" / ") }} />
      <section className="catalog-coverage">
        <PanelHeading eyebrow="Published coverage" title="Provider query" />
        {selected.dataset_id === "economic_observations" ? <><label>Provider<input aria-label="Catalog provider" value={provider} onChange={event => setProvider(event.target.value)} /></label><label>Series ID<input aria-label="Catalog series ID" value={seriesId} onChange={event => setSeriesId(event.target.value)} /></label></> : <><label>Provider<input aria-label="Catalog provider" value={provider} onChange={event => setProvider(event.target.value)} /></label><label>Symbol<input aria-label="Catalog symbol" value={symbol} onChange={event => setSymbol(event.target.value)} /></label><label>Timeframe<select aria-label="Catalog timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)}><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>4h</option><option>1d</option></select></label></>}
        <button className="primary-button drawer-action" onClick={() => void loadCoverage()} disabled={coverageLoading}>{coverageLoading ? "Loading coverage…" : "Load coverage"}</button>
        {coverageError && <ErrorState message={coverageError} />}
        {coverage && <dl className="detail-list coverage-result">{Object.entries(coverage).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value == null ? "—" : typeof value === "number" ? value.toLocaleString() : String(value)}</dd></div>)}</dl>}
      </section>
      <button className="primary-button drawer-action" onClick={() => onExplore(selected)}>Open data explorer</button>
    </DetailDrawer>}
  </section>;
}

function DetailList({ values }: { values: Record<string, string> }) {
  return <dl className="detail-list">{Object.entries(values).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>;
}
