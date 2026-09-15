import { PreferencesProvider } from "./preferences";
import { useEffect, useMemo, useState } from "react";
import "./style.css";
import "./modernization.css";
import "./preview.css";
import { AppShell, type Tab } from "./components/shell";
import { ErrorState, LoadingSkeleton } from "./components/ui";
import { createDataCenterClient, DataCenterError, type Dataset, type Finding, type MaintenanceTaskDraft, type Metrics, type ReadyState, type Run, type SchedulerView } from "./lib/api";
import { createServices } from "./services";
import { CatalogPage } from "./pages/CatalogPage";
import { ExplorerPage, type ExplorerMode } from "./pages/ExplorerPage";
import { MaintenancePage } from "./pages/MaintenancePage";
import { OperationsPage } from "./pages/OperationsPage";
import { ProductionPage } from "./pages/ProductionPage";
import { OverviewPage } from "./pages/OverviewPage";
import { QualityPage } from "./pages/QualityPage";
import { RunsPage } from "./pages/RunsPage";

export function LegacyApp() {
  const preview = import.meta.env.VITE_PREVIEW_ID
    ? { id: import.meta.env.VITE_PREVIEW_ID, mode: import.meta.env.VITE_PREVIEW_DATA_MODE,
        commit: import.meta.env.VITE_PREVIEW_COMMIT, dirty: import.meta.env.VITE_PREVIEW_DIRTY === "true" }
    : null;
  const [previewScheduler, setPreviewScheduler] = useState<SchedulerView | null>(null);
  const [explorerMode, setExplorerMode] = useState<ExplorerMode>("bars");
  const [tab, setTab] = useState<Tab>("overview"); const [apiKey, setApiKey] = useState(""); const [health, setHealth] = useState<ReadyState | null>(null); const [metrics, setMetrics] = useState<Metrics | null>(null); const [datasets, setDatasets] = useState<Dataset[]>([]); const [runs, setRuns] = useState<Run[]>([]); const [findings, setFindings] = useState<Finding[]>([]); const [loading, setLoading] = useState(true); const [loadedOnce, setLoadedOnce] = useState(false); const [error, setError] = useState(""); const [message, setMessage] = useState(""); const [refreshToken, setRefreshToken] = useState(0); const [maintenanceDraft, setMaintenanceDraft] = useState<MaintenanceTaskDraft | null>(null);
  const services = useMemo(() => createServices(apiKey), [apiKey]);
  const refresh = async () => { setLoading(true); setError(""); const client = createDataCenterClient(apiKey); try { const [ready, metricResult, registry, runList, quality] = await Promise.all([client.ready(), client.metrics(), client.datasets(), client.runs(), client.findings()]); setHealth(ready.data); setMetrics(metricResult.data); setDatasets(registry.data); setRuns(runList.data); setFindings(quality.data); setRefreshToken(value => value + 1); } catch (reason) { const requestError = reason as DataCenterError; setError(`${requestError.message}${requestError.requestId ? ` · request ${requestError.requestId}` : ""}`); } finally { setLoading(false); setLoadedOnce(true); } };
  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    if (!preview) return;
    let active = true;
    const load = () => void createDataCenterClient("").scheduler()
      .then(result => { if (active) setPreviewScheduler(result.data); })
      .catch(() => { if (active) setPreviewScheduler(null); });
    load();
    const timer = window.setInterval(load, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [preview?.id]);
  const changed = () => { void refresh(); };
  // Handing work to the maintenance workspace carries whatever the originating
  // page actually established; the workspace still revalidates before anything
  // is queued, and reports the fields its own defaults had to fill in.
  const handoff = (draft?: MaintenanceTaskDraft) => { setMaintenanceDraft(draft ?? null); setTab("maintenance"); };
  return <div className={preview ? "preview-frame" : undefined}>
    {preview && <div className="preview-banner" role="status"><b>预览 · 模拟数据</b><span>{preview.id} · {preview.commit.slice(0, 8)}{preview.dirty ? " · dirty" : ""} · {preview.mode}</span><span>调度：{previewScheduler?.dispatch_enabled ? "有效派发" : "未派发"} · 心跳 {previewScheduler?.scheduler.heartbeat_at ?? "等待中"}</span></div>}
    <AppShell tab={tab} onTab={setTab} health={health} apiKey={apiKey} onApiKey={setApiKey} onRefresh={() => void refresh()} message={message}>
    {error && <ErrorState message={error} onRetry={() => void refresh()} />}
    {/* Only the first load gates the workspace: a background refresh must not
        unmount a page and discard the operator's filters or open drawer. */}
    {!error && loading && !loadedOnce && tab !== "overview" && tab !== "maintenance" && <LoadingSkeleton rows={5} />}
    {!error && tab === "overview" && <OverviewPage datasets={datasets} runs={runs} findings={findings} health={health} metrics={metrics} loading={loading} onRuns={() => setTab("runs")} onOperations={() => setTab("operations")} onMaintenance={() => setTab("maintenance")} />}
    {!error && (loadedOnce || !loading) && tab === "datasets" && <CatalogPage apiKey={apiKey} datasets={datasets} loading={false} services={services} onMaintenance={handoff} onExplore={dataset => { setExplorerMode(dataset.dataset_id === "economic_observations" ? "economic" : "bars"); setTab("explorer"); }} />}
    {/* Maintenance owns every write path, so it stays reachable while reads are
        still loading: a locked console must be able to explain why it refuses. */}
    {!error && tab === "maintenance" && <MaintenancePage apiKey={apiKey} services={services} draft={maintenanceDraft} onMessage={setMessage} onChanged={changed} />}
    {!error && (loadedOnce || !loading) && tab === "runs" && <RunsPage services={services} refreshToken={refreshToken} onMessage={setMessage} onChanged={changed} />}
    {!error && (loadedOnce || !loading) && tab === "quality" && <QualityPage findings={findings} services={services} onMessage={setMessage} onChanged={changed} onMaintenance={handoff} />}
    {!error && (loadedOnce || !loading) && tab === "explorer" && <ExplorerPage apiKey={apiKey} initialMode={explorerMode} services={services} onMaintenance={handoff} key={explorerMode} />}
    {/* Production plans is a write workspace too, so it stays reachable while
        reads are loading for the same reason Maintenance does. */}
    {!error && tab === "production" && <ProductionPage services={services} onMessage={setMessage} onChanged={changed} />}
    {!error && (loadedOnce || !loading) && tab === "operations" && <OperationsPage apiKey={apiKey} health={health} metrics={metrics} services={services} onChanged={changed} onMessage={setMessage} />}
    </AppShell>
  </div>;
}

export function LegacyConsole() {
  return <PreferencesProvider><LegacyApp /></PreferencesProvider>;
}
