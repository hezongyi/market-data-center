import { type FormEvent, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { DataTable, EmptyState, ErrorState, PanelHeading, StatusBadge } from "../components/ui";
import { createDataCenterClient, type ApiMeta, type Bar, type EconomicObservation } from "../lib/api";

export type ExplorerMode = "bars" | "economic";

const barColumns: ColumnDef<Bar>[] = [
  { accessorKey: "bar_ts", header: "Timestamp", cell: info => <span className="mono">{String(info.getValue())}</span> },
  { accessorKey: "open", header: "Open" },
  { accessorKey: "high", header: "High" },
  { accessorKey: "low", header: "Low" },
  { accessorKey: "close", header: "Close" },
  { accessorKey: "volume", header: "Volume", cell: info => String(info.getValue() ?? "—") },
];
const economicColumns: ColumnDef<EconomicObservation>[] = [
  { accessorKey: "observation_date", header: "Observation" },
  { accessorKey: "value", header: "Value", cell: info => String(info.getValue() ?? "—") },
  { accessorKey: "release_ts", header: "Release", cell: info => String(info.getValue() ?? "—") },
  { accessorKey: "frequency", header: "Frequency", cell: info => String(info.getValue() ?? "—") },
  { accessorKey: "units", header: "Units", cell: info => String(info.getValue() ?? "—") },
];

export function ExplorerPage({ apiKey, initialMode = "bars" }: { apiKey: string; initialMode?: ExplorerMode }) {
  const [mode, setMode] = useState<ExplorerMode>(initialMode);
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("1d");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [seriesId, setSeriesId] = useState("PAYEMS");
  const [economicMode, setEconomicMode] = useState("current");
  const [asof, setAsof] = useState("2026-09-10T00:00:00Z");
  const [bars, setBars] = useState<Bar[]>([]);
  const [observations, setObservations] = useState<EconomicObservation[]>([]);
  const [coverage, setCoverage] = useState<Record<string, unknown> | null>(null);
  const [meta, setMeta] = useState<ApiMeta>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const reset = (next: ExplorerMode) => {
    setMode(next); setBars([]); setObservations([]); setCoverage(null); setMeta({});
    setCursor(null); setHistory([]); setError("");
  };
  const load = async (event?: FormEvent, nextCursor: string | null = null, nextHistory: Array<string | null> = []) => {
    event?.preventDefault();
    if (start && end && start > end) { setError("Start date must not be after end date."); return; }
    if (mode === "economic" && economicMode === "pit" && !asof) { setError("PIT mode requires an as-of timestamp."); return; }
    setLoading(true); setError("");
    try {
      const client = createDataCenterClient(apiKey);
      if (mode === "bars") {
        const selector = `provider=${encodeURIComponent(provider)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`;
        const window = `${start ? `&start=${encodeURIComponent(`${start}T00:00:00Z`)}` : ""}${end ? `&end=${encodeURIComponent(`${end}T23:59:59.999Z`)}` : ""}`;
        const [page, currentCoverage] = await Promise.all([client.barsPage(`${selector}${window}`, nextCursor), client.coverage(selector)]);
        setBars(page.data); setMeta(page.meta); setCoverage(currentCoverage.data);
      } else {
        const window = `${start ? `&start=${encodeURIComponent(start)}` : ""}${end ? `&end=${encodeURIComponent(end)}` : ""}`;
        const query = `provider=fred&series_id=${encodeURIComponent(seriesId)}&mode=${economicMode}${economicMode === "pit" ? `&asof_ts=${encodeURIComponent(asof)}` : ""}${window}`;
        const page = await client.economicPage(query, nextCursor);
        setObservations(page.data); setMeta(page.meta);
        setCoverage({ provider: "fred", series_id: seriesId, query_mode: page.meta.query_mode, count: page.meta.count ?? page.data.length });
      }
      setCursor(nextCursor); setHistory(nextHistory);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load data");
    } finally {
      setLoading(false);
    }
  };
  const next = () => { if (meta.next_cursor) void load(undefined, meta.next_cursor, [...history, cursor]); };
  const previous = () => { const prior = history.at(-1) ?? null; void load(undefined, prior, history.slice(0, -1)); };
  const rows = mode === "bars" ? bars : observations;

  return <div className="explorer-page">
    <section className="panel">
      <PanelHeading eyebrow="Published data" title="Data explorer" />
      <div className="segmented" role="group" aria-label="Explorer dataset">
        <button className={mode === "bars" ? "active" : ""} onClick={() => reset("bars")}>Provider bars</button>
        <button className={mode === "economic" ? "active" : ""} onClick={() => reset("economic")}>Economic</button>
      </div>
      <form className="explorer-toolbar" onSubmit={event => void load(event)}>
        {mode === "bars" ? <>
          <label>Provider<input aria-label="Provider" value={provider} onChange={event => setProvider(event.target.value)} /></label>
          <label>Symbol<input aria-label="Symbol" value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
          <label>Timeframe<input aria-label="Timeframe" value={timeframe} onChange={event => setTimeframe(event.target.value)} /></label>
        </> : <>
          <label>Series ID<input aria-label="Series ID" value={seriesId} onChange={event => setSeriesId(event.target.value)} /></label>
          <label>Query mode<select aria-label="Query mode" value={economicMode} onChange={event => setEconomicMode(event.target.value)}><option value="current">Current</option><option value="pit">Point in time</option></select></label>
          {economicMode === "pit" && <label>As-of timestamp<input aria-label="As-of timestamp" value={asof} onChange={event => setAsof(event.target.value)} /></label>}
        </>}
        <label>Start<input aria-label="Query start date" type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>End<input aria-label="Query end date" type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
        <button className="primary-button" type="submit" disabled={loading}><Search size={15} />{loading ? "Loading…" : mode === "bars" ? "Load coverage" : "Load observations"}</button>
      </form>
      {error ? <ErrorState message={error} /> : coverage ? <div className="coverage-strip">{Object.entries(coverage).slice(0, 5).map(([key, value]) => <div key={key}><small>{key}</small><b>{String(value ?? "—")}</b></div>)}</div> : <EmptyState title={mode === "bars" ? "Choose an instrument" : "Choose an economic series"} detail="Only immutable published snapshots are queried." />}
    </section>
    {coverage && <section className="panel">
      <PanelHeading eyebrow="Snapshot" title={mode === "bars" ? "Current bars" : "Economic observations"} action={<div className="meta-badges"><StatusBadge tone="neutral">{String(meta.snapshot_id ?? "no snapshot")}</StatusBadge><StatusBadge tone="neutral">{(meta.schema_versions ?? []).join(", ") || "schema unknown"}</StatusBadge></div>} />
      {mode === "bars" ? <DataTable data={bars} columns={barColumns} empty="No bars in this page." /> : <DataTable data={observations} columns={economicColumns} empty="No observations in this page." />}
      <div className="pagination"><button aria-label="Previous page" onClick={previous} disabled={!history.length}><ChevronLeft size={15} /> Previous</button><span>Page {history.length + 1} · {rows.length} rows</span><button aria-label="Next page" onClick={next} disabled={!meta.next_cursor}>Next <ChevronRight size={15} /></button></div>
    </section>}
  </div>;
}
