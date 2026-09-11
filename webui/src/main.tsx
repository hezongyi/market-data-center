import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import "./modernization.css";
import { AppShell, type Tab } from "./components/shell";
import { ErrorState, LoadingSkeleton } from "./components/ui";
import { createDataCenterClient, DataCenterError, type Dataset, type Finding, type Metrics, type ReadyState, type Run } from "./lib/api";
import { CatalogPage } from "./pages/CatalogPage";
import { ExplorerPage, type ExplorerMode } from "./pages/ExplorerPage";
import { OperationsPage } from "./pages/OperationsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { QualityPage } from "./pages/QualityPage";
import { RunsPage } from "./pages/RunsPage";

function App() {
  const [explorerMode, setExplorerMode] = useState<ExplorerMode>("bars");
  const [tab, setTab] = useState<Tab>("overview"); const [apiKey, setApiKey] = useState(""); const [health, setHealth] = useState<ReadyState | null>(null); const [metrics, setMetrics] = useState<Metrics | null>(null); const [datasets, setDatasets] = useState<Dataset[]>([]); const [runs, setRuns] = useState<Run[]>([]); const [findings, setFindings] = useState<Finding[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [message, setMessage] = useState(""); const [refreshToken, setRefreshToken] = useState(0);
  const refresh = async () => { setLoading(true); setError(""); const client = createDataCenterClient(apiKey); try { const [ready, metricResult, registry, runList, quality] = await Promise.all([client.ready(), client.metrics(), client.datasets(), client.runs(), client.findings()]); setHealth(ready.data); setMetrics(metricResult.data); setDatasets(registry.data); setRuns(runList.data); setFindings(quality.data); setRefreshToken(value => value + 1); } catch (reason) { const requestError = reason as DataCenterError; setHealth(null); setError(`${requestError.message}${requestError.requestId ? ` · request ${requestError.requestId}` : ""}`); } finally { setLoading(false); } };
  useEffect(() => { void refresh(); }, []);
  const changed = () => { void refresh(); };
  return <AppShell tab={tab} onTab={setTab} health={health} apiKey={apiKey} onApiKey={setApiKey} onRefresh={() => void refresh()} message={message}>
    {error && <ErrorState message={error} onRetry={() => void refresh()} />}
    {!error && loading && tab !== "overview" && <LoadingSkeleton rows={5} />}
    {!error && tab === "overview" && <OverviewPage datasets={datasets} runs={runs} findings={findings} health={health} metrics={metrics} loading={loading} onRuns={() => setTab("runs")} onOperations={() => setTab("operations")} />}
    {!error && !loading && tab === "datasets" && <CatalogPage datasets={datasets} loading={false} onExplore={dataset => { setExplorerMode(dataset.dataset_id === "economic_observations" ? "economic" : "bars"); setTab("explorer"); }} />}
    {!error && !loading && tab === "runs" && <RunsPage apiKey={apiKey} refreshToken={refreshToken} onChanged={changed} onMessage={setMessage} />}
    {!error && !loading && tab === "quality" && <QualityPage findings={findings} />}
    {!error && !loading && tab === "explorer" && <ExplorerPage apiKey={apiKey} initialMode={explorerMode} key={explorerMode} />}
    {!error && !loading && tab === "operations" && <OperationsPage apiKey={apiKey} health={health} metrics={metrics} onChanged={changed} onMessage={setMessage} />}
  </AppShell>;
}

createRoot(document.getElementById("root")!).render(<App />);
