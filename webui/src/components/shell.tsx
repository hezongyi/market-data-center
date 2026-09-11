import { Activity, ChevronLeft, ChevronRight, Database, FlaskConical, Gauge, KeyRound, Search, ShieldCheck } from "lucide-react";
import { type ReactNode, useState } from "react";
import { PageHeader, StatusBadge } from "./ui";
import type { ReadyState } from "../lib/api";

export type Tab = "overview" | "datasets" | "runs" | "quality" | "explorer" | "operations";
const items: Array<{ key: Tab; label: string; icon: typeof Gauge }> = [
  { key: "overview", label: "Overview", icon: Gauge }, { key: "datasets", label: "Data catalog", icon: Database },
  { key: "runs", label: "Runs", icon: Activity }, { key: "quality", label: "Quality", icon: ShieldCheck },
  { key: "explorer", label: "Explorer", icon: Search }, { key: "operations", label: "Operations", icon: FlaskConical },
];

export function AppShell({ tab, onTab, health, apiKey, onApiKey, onRefresh, message, children }: { tab: Tab; onTab: (tab: Tab) => void; health: ReadyState | null; apiKey: string; onApiKey: (key: string) => void; onRefresh: () => void; message: string; children: ReactNode }) {
  const [accessOpen, setAccessOpen] = useState(false); const [collapsed, setCollapsed] = useState(false); const title = tab === "overview" ? "Good morning, data center" : items.find(item => item.key === tab)?.label ?? tab;
  return <div className={`app-shell${collapsed ? " shell-collapsed" : ""}`}><aside className="sidebar"><div className="brand"><span>MD</span><div><b>Market Data</b><small>Center / operations</small></div></div><button className="collapse-button" aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title={collapsed ? "Expand sidebar" : "Collapse sidebar"} onClick={() => setCollapsed(value => !value)}>{collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}</button><nav>{items.map(({ key, label, icon: Icon }) => <button aria-label={key} title={label} className={tab === key ? "selected" : ""} onClick={() => onTab(key)} key={key}><Icon size={16} /><span>{label}</span></button>)}</nav><div className="sidebar-foot"><StatusBadge tone={health?.status === "ready" ? "good" : "warn"}>API {health?.status ?? "unavailable"}</StatusBadge><small>{health?.software_version ?? "v0.2.0"} · {health?.deployment_id ?? "development"}</small><button className="sidebar-refresh" onClick={onRefresh}>Refresh data</button></div></aside><main className="main"><PageHeader eyebrow="Operations console" title={title} actions={<><StatusBadge tone={health?.capacity_status === "critical" ? "bad" : health?.capacity_status === "warning" ? "warn" : "good"}>{health?.capacity_status ?? "Checking"}</StatusBadge><button className="access-button" onClick={() => setAccessOpen(value => !value)}><KeyRound size={15} /> Session access</button></>} />{accessOpen && <div className="access-panel"><label>API key<input aria-label="API key" type="password" value={apiKey} onChange={event => onApiKey(event.target.value)} placeholder="Current session only" /></label><button onClick={() => setAccessOpen(false)}>Done</button></div>}{message && <div className="notice" role="status">{message}</div>}{children}</main></div>;
}
