import { type FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Tab = "overview" | "datasets" | "runs" | "quality" | "explorer";
type Envelope<T> = { data: T; errors: Array<{ message: string }> };
type Dataset = { dataset_id: string; schema_version: string; description: string; partitioning: string[] };
type Run = { run_id: string; dataset_id: string; status: string; job_id?: string; row_count?: number; created_at?: string; error_type?: string; retry_count?: number; retry_of?: string; error?: string };
type Finding = { severity: string; code: string; dataset_id?: string; observation_date?: string; bar_ts?: string };
type Bar = { bar_ts: string; open: number; high: number; low: number; close: number };

const dateOffset = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);

function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [apiKey, setApiKey] = useState("");
  const [health, setHealth] = useState("checking");
  const [runFilter, setRunFilter] = useState("all");
  const [retrying, setRetrying] = useState<string | null>(null);
  const [workerAge, setWorkerAge] = useState<number | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [message, setMessage] = useState("");
  const [provider, setProvider] = useState("fixture");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("1d");
  const [bars, setBars] = useState<Bar[]>([]);
  const [coverage, setCoverage] = useState<Record<string, unknown> | null>(null);
  const [start, setStart] = useState(dateOffset(-7));
  const [end, setEnd] = useState(dateOffset(0));

  const request = async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (apiKey) headers.set("X-API-Key", apiKey);
    const response = await fetch(`/api/v1${path}`, { ...init, headers });
    const payload = await response.json() as Envelope<T>;
    if (!response.ok) throw new Error(payload.errors.map((error) => error.message).join(", ") || "Request failed");
    return payload.data;
  };

  const refresh = async () => {
    try {
      const [service, registry, runList, quality] = await Promise.all([
        fetch("/api/v1/health/ready").then(response => response.json()).then(payload => payload.data as {status: string; worker_heartbeat_age_seconds: number | null}), request<Dataset[]>("/datasets"), request<Run[]>("/runs"), request<Finding[]>("/quality/findings"),
      ]);
      setHealth(service.status); setWorkerAge(service.worker_heartbeat_age_seconds); setDatasets(registry); setRuns(runList); setFindings(quality);
    } catch (error) { setHealth("unavailable"); setMessage(error instanceof Error ? error.message : "Unable to refresh data center"); }
  };

  useEffect(() => { void refresh(); }, []);
  const retryRun = async (run: Run) => {
    setRetrying(run.run_id);
    try {
      const replacement = await request<Run>(`/runs/${run.run_id}/retry`, {method: "POST"});
      setMessage(`Queued retry ${replacement.run_id}`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Retry failed"); }
    finally { setRetrying(null); }
  };
  const activeRunCount = useMemo(() => runs.filter((run) => run.status === "queued" || run.status === "running").length, [runs]);

  const loadBars = async () => {
    try {
      const query = `provider=${encodeURIComponent(provider)}&symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`;
      const [rows, currentCoverage] = await Promise.all([request<Bar[]>(`/bars?${query}`), request<Record<string, unknown>>(`/provider-bars/coverage?${query}`)]);
      setBars(rows); setCoverage(currentCoverage); setMessage("");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to load bars"); }
  };

  const queueIngest = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const run = await request<Run>("/ingest/runs", { method: "POST", body: JSON.stringify({ job_id: `webui-${Date.now()}`, provider, symbol, asset_class: provider === "yfinance" ? "equity" : "crypto", timeframe, start: `${start}T00:00:00+00:00`, end: `${end}T00:00:00+00:00` }) });
      setMessage(`Queued run ${run.run_id}`); await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to queue ingest"); }
  };

  return <div className="app-shell">
    <aside><div className="brand"><span>MD</span>Market Data Center</div><nav>{(["overview", "datasets", "runs", "quality", "explorer"] as Tab[]).map((item) => <button className={tab === item ? "selected" : ""} onClick={() => setTab(item)} key={item}>{item}</button>)}</nav><div className="api-key"><label>API key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="Optional" /></label><button className="secondary" onClick={() => void refresh()}>Refresh</button></div></aside>
    <main><header><div><p>Operations</p><h1>{tab}</h1></div><span className={health === "ready" ? "status ok" : "status"}>{health}</span></header>{message && <div className="notice">{message}</div>}
      {tab === "overview" && <><section className="metrics"><Metric label="Datasets" value={datasets.length} /><Metric label="Active runs" value={activeRunCount} /><Metric label="Open findings" value={findings.length} /><Metric label="Service" value={health} /></section><section className="panel"><h2>Recent runs</h2><RunTable runs={runs.slice(0, 8)} /></section></>}
      {tab === "datasets" && <section className="panel"><h2>Dataset registry</h2>{datasets.map((dataset) => <article className="dataset" key={dataset.dataset_id}><div><h3>{dataset.dataset_id}</h3><p>{dataset.description}</p></div><div><b>{dataset.schema_version}</b><p>{dataset.partitioning.join(" / ")}</p></div></article>)}</section>}
      {tab === "runs" && <section className="panel"><h2>Ingest runs</h2><div className="run-tools"><label>Status <select value={runFilter} onChange={event => setRunFilter(event.target.value)}>{["all", "queued", "running", "pass", "failed", "dead_letter"].map(status => <option key={status} value={status}>{status}</option>)}</select></label><button onClick={() => void refresh()}>Refresh</button><span>Worker: {workerAge === null ? "unavailable" : `${Math.round(workerAge)}s ago`}</span></div><RunTable runs={runs.filter(run => runFilter === "all" || run.status === runFilter)} retry={retryRun} retrying={retrying} /></section>}
      {tab === "quality" && <section className="panel"><h2>Quality findings</h2>{findings.length === 0 ? <p className="empty">No persisted findings.</p> : <table><thead><tr><th>Severity</th><th>Code</th><th>Dataset</th><th>Timestamp</th></tr></thead><tbody>{findings.map((finding, index) => <tr key={`${finding.code}-${index}`}><td><span className="tag error">{finding.severity}</span></td><td>{finding.code}</td><td>{finding.dataset_id || "-"}</td><td>{finding.observation_date || finding.bar_ts || "-"}</td></tr>)}</tbody></table>}</section>}
      {tab === "explorer" && <section className="explorer"><form className="panel form-panel" onSubmit={queueIngest}><h2>Provider bars</h2><label>Provider<input value={provider} onChange={(event) => setProvider(event.target.value)} /></label><label>Symbol<input value={symbol} onChange={(event) => setSymbol(event.target.value)} /></label><label>Timeframe<input value={timeframe} onChange={(event) => setTimeframe(event.target.value)} /></label><button type="button" onClick={() => void loadBars()}>Load coverage</button><hr /><label>Start<input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></label><label>End<input type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></label><button type="submit">Queue ingest</button></form><section className="panel"><h2>Coverage</h2>{coverage ? <dl>{Object.entries(coverage).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value ?? "-")}</dd></div>)}</dl> : <p className="empty">Select an instrument and load coverage.</p>}</section><section className="panel bars"><h2>Current bars</h2>{bars.length === 0 ? <p className="empty">No rows loaded.</p> : <table><thead><tr><th>Timestamp</th><th>Open</th><th>High</th><th>Low</th><th>Close</th></tr></thead><tbody>{bars.map((bar) => <tr key={bar.bar_ts}><td>{bar.bar_ts}</td><td>{bar.open}</td><td>{bar.high}</td><td>{bar.low}</td><td>{bar.close}</td></tr>)}</tbody></table>}</section></section>}
    </main>
  </div>;
}

function Metric({ label, value }: { label: string; value: string | number }) { return <article className="metric"><span>{label}</span><strong>{value}</strong></article>; }
function RunTable({ runs, retry, retrying }: { runs: Run[]; retry?: (run: Run) => Promise<void>; retrying?: string | null }) { return runs.length === 0 ? <p className="empty">No runs recorded.</p> : <div className="table-scroll"><table><thead><tr><th>Run ID</th><th>Dataset</th><th>Status</th><th>Rows</th><th>Created</th>{retry && <th>Failure / Retry</th>}</tr></thead><tbody>{runs.map((run) => <tr key={run.run_id}><td className="mono">{run.run_id}{run.retry_of && <p>Retry of {run.retry_of}</p>}</td><td>{run.dataset_id}</td><td><span className={`tag ${run.status}`}>{run.status}</span></td><td>{run.row_count ?? "-"}</td><td>{run.created_at || "-"}</td>{retry && <td>{run.error_type && <p>{run.error_type}: {run.error}</p>}{["failed", "dead_letter"].includes(run.status) && <button disabled={retrying !== null && retrying !== undefined} onClick={() => void retry(run)}>{retrying === run.run_id ? "Queuing..." : "Retry"}</button>}</td>}</tr>)}</tbody></table></div>; }

createRoot(document.getElementById("root")!).render(<App />);
