import { type FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { createDataCenterClient, DataCenterError, type Bar, type Dataset, type Finding, type ReadyState, type Run } from "./lib/api";

type Tab = "overview" | "datasets" | "runs" | "quality" | "explorer";
const dateOffset = (days: number) => new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);

function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [apiKey, setApiKey] = useState("");
  const [health, setHealth] = useState<ReadyState | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [runFilter, setRunFilter] = useState("all");
  const [retrying, setRetrying] = useState<string | null>(null);
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("1d");
  const [start, setStart] = useState(dateOffset(-7));
  const [end, setEnd] = useState(dateOffset(0));
  const [bars, setBars] = useState<Bar[]>([]);
  const [coverage, setCoverage] = useState<Record<string, unknown> | null>(null);

  const refresh = async () => {
    setLoading(true);
    const client = createDataCenterClient(apiKey);
    try {
      const [ready, registry, runList, quality] = await Promise.all([client.ready(), client.datasets(), client.runs(), client.findings()]);
      setHealth(ready.data); setDatasets(registry.data); setRuns(runList.data); setFindings(quality.data);
    } catch (error) {
      setHealth(null); setMessage(error instanceof DataCenterError ? error.message : "Unable to refresh data center");
    } finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []);

  const retryRun = async (run: Run) => {
    setRetrying(run.run_id);
    try { const result = await createDataCenterClient(apiKey).retry(run.run_id); await refresh(); setMessage(`Queued retry ${result.data.run_id}`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Retry failed"); }
    finally { setRetrying(null); }
  };
  const queueIngest = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const result = await createDataCenterClient(apiKey).ingest({ job_id: `webui-${Date.now()}`, provider, symbol, asset_class: provider === "yfinance" ? "equity" : "crypto", timeframe, run_scope: "acceptance", start: `${start}T00:00:00+00:00`, end: `${end}T00:00:00+00:00` });
      await refresh(); setMessage(`Queued run ${result.data.run_id}`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to queue ingest"); }
  };
  const loadBars = async () => {
    try {
      const query = `provider=${encodeURIComponent(provider)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`;
      const client = createDataCenterClient(apiKey); const [rows, currentCoverage] = await Promise.all([client.bars(query), client.coverage(query)]);
      setBars(rows); setCoverage(currentCoverage.data);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to load bars"); }
  };
  const active = useMemo(() => runs.filter(run => ["queued", "running"].includes(run.status)).length, [runs]);
  const status = health?.status === "ready" ? "Ready" : health ? health.status : "Unavailable";
  const filteredRuns = runs.filter(run => runFilter === "all" || run.status === runFilter);
  const nav = (item: Tab) => <button aria-label={item} className={tab === item ? "selected" : ""} onClick={() => setTab(item)} key={item}>{item}</button>;

  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span>MD</span><div><b>Market Data</b><small>Center / operations</small></div></div><nav>{(["overview", "datasets", "runs", "quality", "explorer"] as Tab[]).map(nav)}</nav><div className="sidebar-foot"><Status tone={health?.capacity_status === "critical" ? "bad" : health?.capacity_status === "warning" ? "warn" : "good"}>API {status}</Status><small>{health?.software_version ?? "v0.2.0"} · {health?.deployment_id ?? "development"}</small><label>API key<input aria-label="API key" type="password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder="Optional" /></label><button className="sidebar-refresh" onClick={() => void refresh()}>Refresh data</button></div></aside>
    <main className="main"><header className="page-header"><div><small>Operations overview</small><h1>{tab === "overview" ? "Good morning, data center" : tab}</h1></div><div className="header-meta"><Status tone={health?.status === "ready" ? "good" : "warn"}>{status}</Status><button className="avatar" aria-label="Account">HZ</button></div></header>{message && <div className="notice">{message}</div>}
      {tab === "overview" && <Overview datasets={datasets} runs={runs} findings={findings} active={active} health={health} loading={loading} onRuns={() => setTab("runs")} />}
      {tab === "datasets" && <section className="panel"><Heading eyebrow="Registry" title="Dataset catalog" />{loading ? <Empty text="Loading…" /> : datasets.length === 0 ? <Empty text="No datasets published." /> : datasets.map(dataset => <article className="dataset" key={dataset.dataset_id}><div><h3>{dataset.dataset_id}</h3><p>{dataset.description}</p></div><div><b>{dataset.schema_version}</b><p>{dataset.partitioning.join(" / ")}</p></div></article>)}</section>}
      {tab === "runs" && <section className="panel"><Heading eyebrow="Live activity" title="Ingest runs" action={<button className="link-button" onClick={() => void refresh()}>Refresh →</button>} /><div className="run-tools"><label>Status<select aria-label="Status" value={runFilter} onChange={event => setRunFilter(event.target.value)}>{["all", "queued", "running", "pass", "failed", "dead_letter"].map(value => <option key={value}>{value}</option>)}</select></label><span className="worker-state">Worker: {health?.worker_heartbeat_age_seconds == null ? "unavailable" : `${Math.round(health.worker_heartbeat_age_seconds)}s ago`}</span></div>{loading ? <Empty text="Loading…" /> : <RunTable runs={filteredRuns} retry={retryRun} retrying={retrying} />}</section>}
      {tab === "quality" && <section className="panel"><Heading eyebrow="System signal" title="Quality findings" />{findings.length === 0 ? <Empty text="No persisted findings." /> : <table><thead><tr><th>Severity</th><th>Code</th><th>Dataset</th><th>Timestamp</th></tr></thead><tbody>{findings.map((finding, index) => <tr key={`${finding.code}-${index}`}><td><Status tone="bad">{finding.severity}</Status></td><td>{finding.code}</td><td>{finding.dataset_id || "-"}</td><td>{finding.observation_date || finding.bar_ts || "-"}</td></tr>)}</tbody></table>}</section>}
      {tab === "explorer" && <Explorer provider={provider} setProvider={setProvider} symbol={symbol} setSymbol={setSymbol} timeframe={timeframe} setTimeframe={setTimeframe} start={start} setStart={setStart} end={end} setEnd={setEnd} loadBars={loadBars} queueIngest={queueIngest} bars={bars} coverage={coverage} />}
    </main>
  </div>;
}

function Overview({ datasets, runs, findings, active, health, loading, onRuns }: { datasets: Dataset[]; runs: Run[]; findings: Finding[]; active: number; health: ReadyState | null; loading: boolean; onRuns: () => void }) { return <><div className="capacity-banner"><Status tone={health?.capacity_status === "critical" ? "bad" : "warn"}>Capacity {health?.capacity_status ?? "checking"}</Status><span>{health?.capacity_status === "critical" ? "New writes are protected while reads remain available." : "Free space and ingest policy are monitored continuously."}</span><button onClick={onRuns}>Review activity →</button></div><section className="metrics"><Metric label="Datasets" value={loading ? "—" : `${datasets.length}`} note="published schemas" /><Metric label="Active runs" value={loading ? "—" : `${active}`} note="worker queue" /><Metric label="Service" value={health?.status === "ready" ? "Ready" : "—"} note={health?.read_status ?? "checking readiness"} /><Metric label="Open findings" value={loading ? "—" : `${findings.length}`} note={findings.length ? "review required" : "no persisted findings"} /></section><div className="overview-grid"><section className="panel"><Heading eyebrow="Live activity" title="Recent runs" action={<button className="link-button" onClick={onRuns}>View all →</button>} /><RunTable runs={runs.slice(0, 5)} /></section><section className="panel health-panel"><Heading eyebrow="System signal" title="Service health" action={<Status tone={health?.status === "ready" ? "good" : "warn"}>{health?.status === "ready" ? "Healthy" : "Checking"}</Status>} /><div className="health-score"><strong>{health?.status === "ready" ? "99.98%" : "—"}</strong><span>availability · current deployment</span></div><div className="health-bars">{Array.from({ length: 12 }, (_, i) => <i key={i} />)}</div><dl><div><dt>Read path</dt><dd>{health?.read_status ?? "—"}</dd></div><div><dt>Write path</dt><dd>{health?.write_status ?? "—"}</dd></div><div><dt>Source commit</dt><dd>{health?.source_commit ? `${health.source_commit.slice(0, 8)}…` : "—"}</dd></div></dl></section></div></>; }

function Explorer({ provider, setProvider, symbol, setSymbol, timeframe, setTimeframe, start, setStart, end, setEnd, loadBars, queueIngest, bars, coverage }: { provider: string; setProvider: (v: string) => void; symbol: string; setSymbol: (v: string) => void; timeframe: string; setTimeframe: (v: string) => void; start: string; setStart: (v: string) => void; end: string; setEnd: (v: string) => void; loadBars: () => Promise<void>; queueIngest: (e: FormEvent) => void; bars: Bar[]; coverage: Record<string, unknown> | null }) { return <section className="explorer"><form className="panel form-panel" onSubmit={queueIngest}><Heading eyebrow="Data explorer" title="Provider bars" /><label>Provider<input value={provider} onChange={event => setProvider(event.target.value)} /></label><label>Symbol<input value={symbol} onChange={event => setSymbol(event.target.value)} /></label><label>Timeframe<input value={timeframe} onChange={event => setTimeframe(event.target.value)} /></label><button type="button" onClick={() => void loadBars()}>Load coverage</button><hr /><label>Start<input type="date" value={start} onChange={event => setStart(event.target.value)} /></label><label>End<input type="date" value={end} onChange={event => setEnd(event.target.value)} /></label><button type="submit">Queue ingest</button></form><section className="panel"><Heading eyebrow="Snapshot" title="Coverage" />{coverage ? <dl>{Object.entries(coverage).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value ?? "-")}</dd></div>)}</dl> : <Empty text="Select an instrument and load coverage." />}</section><section className="panel bars"><Heading eyebrow="Published data" title="Current bars" />{bars.length === 0 ? <Empty text="No rows loaded." /> : <table><thead><tr><th>Timestamp</th><th>Open</th><th>High</th><th>Low</th><th>Close</th></tr></thead><tbody>{bars.map(bar => <tr key={bar.bar_ts}><td>{bar.bar_ts}</td><td>{bar.open}</td><td>{bar.high}</td><td>{bar.low}</td><td>{bar.close}</td></tr>)}</tbody></table>}</section></section>; }

function Heading({ eyebrow, title, action }: { eyebrow: string; title: string; action?: React.ReactNode }) { return <div className="panel-heading"><div><small>{eyebrow}</small><h2>{title}</h2></div>{action}</div>; }
function Metric({ label, value, note }: { label: string; value: string; note: string }) { return <article className="metric"><span>{label}</span><strong>{value}</strong><small>{note}</small></article>; }
function Status({ children, tone }: { children: React.ReactNode; tone: "good" | "warn" | "bad" }) { return <span className={`status ${tone} ok`}><i />{children}</span>; }
function Empty({ text }: { text: string }) { return <p className="empty">{text}</p>; }
function RunTable({ runs, retry, retrying }: { runs: Run[]; retry?: (run: Run) => Promise<void>; retrying?: string | null }) { if (runs.length === 0) return <Empty text="No runs recorded." />; return <div className="table-scroll"><table><thead><tr><th>Run ID</th><th>Dataset</th><th>Status</th><th>Rows</th><th>Created</th>{retry && <th>Failure / Retry</th>}</tr></thead><tbody>{runs.map(run => <tr key={run.run_id}><td className="mono">{run.run_id}{run.retry_of && <small>Retry of {run.retry_of}</small>}</td><td>{run.dataset_id}</td><td><Status tone={run.status === "pass" ? "good" : ["failed", "dead_letter"].includes(run.status) ? "bad" : "warn"}>{run.status}</Status></td><td>{run.row_count ?? "-"}</td><td>{run.created_at || "-"}</td>{retry && <td>{run.error_type && <small className="error-copy">{run.error_type}: {run.error}</small>}{["failed", "dead_letter"].includes(run.status) && <button className="table-action" disabled={retrying !== null && retrying !== undefined} onClick={() => void retry(run)}>{retrying === run.run_id ? "Queuing…" : "Retry"}</button>}</td>}</tr>)}</tbody></table></div>; }

createRoot(document.getElementById("root")!).render(<App />);
