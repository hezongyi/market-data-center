import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
export type Locale = "zh-CN" | "en-US";
export type TimeMode = "Asia/Shanghai" | "UTC" | "dual";
const dictionary: Record<string, string> = { Overview: "总览", "Data catalog": "数据目录", Maintenance: "维护任务", Runs: "运行记录", Quality: "质量", Explorer: "数据浏览", Operations: "运维", "Good morning, data center": "市场数据中心", "Operations console": "运维控制台", "Refresh data": "刷新数据", "Session access": "会话访问", "API key": "API 密钥", Done: "完成", "Current session only": "仅当前会话", Capacity: "容量", ok: "正常", warning: "警告", critical: "临界", checking: "检查中", ready: "就绪", unavailable: "不可用", fresh: "最新", stale: "过期", unknown: "未知", "Recent runs": "最近运行", "Live activity": "实时活动", "Service health": "服务健康", "System signal": "系统状态", "Needs attention": "需要关注", "Degraded and failed runs": "降级与失败运行", Datasets: "数据集", "Runs (24h)": "运行（24小时）", "Failure rate (24h)": "失败率（24小时）", "Open findings": "待处理问题", "Run ID": "运行 ID", Dataset: "数据集", Status: "状态", Rows: "行数", pass: "通过", failed: "失败", running: "运行中", queued: "排队中", succeeded: "成功", paused: "暂停", "Unable to load data": "无法加载数据", "Try again": "重试", Cancel: "取消", Details: "详情" };
function read(key: string) { try { return localStorage.getItem(key); } catch { return null; } }
const Context = createContext({ locale: "zh-CN" as Locale, timeMode: "Asia/Shanghai" as TimeMode, setLocale: (_: Locale) => {}, setTimeMode: (_: TimeMode) => {} });
export function PreferencesProvider({ children }: { children: ReactNode }) {
 const [locale, setLocale] = useState<Locale>(() => read("mdc.locale") === "en-US" ? "en-US" : "zh-CN");
 const [timeMode, setTimeMode] = useState<TimeMode>(() => { const value = read("mdc.timeMode"); return value === "UTC" || value === "dual" ? value : "Asia/Shanghai"; });
 useEffect(() => { document.documentElement.lang = locale; try { localStorage.setItem("mdc.locale", locale); localStorage.setItem("mdc.timeMode", timeMode); } catch { /* Preferences remain available for this session. */ } }, [locale, timeMode]);
 return <Context.Provider value={{locale, timeMode, setLocale, setTimeMode}}>{children}</Context.Provider>;
}
export function usePreferences() { const value = useContext(Context); const additions: Record<string, string> = { "Password changed. Sign in again.": "密码已修改，请重新登录", "Initialized. Sign in.": "初始化完成，请登录", "Set the first password to enable login.": "请先设置初始密码以启用登录", "Initialize password": "初始化密码", "Signed in as": "当前登录用户", "Current password": "当前密码", "New password": "新密码", "Change password": "修改密码", Login: "登录", Logout: "退出", Password: "密码", Username: "用户名" }; return { ...value, t: (text: string) => value.locale === "zh-CN" ? dictionary[text] ?? additions[text] ?? text : text }; }
export function TimeDisplay({ value }: { value?: string | null }) {
 const { locale, timeMode } = usePreferences(); if (!value) return <>—</>; const date = new Date(value); if (Number.isNaN(date.getTime())) return <>—</>;
 const format = (zone: "UTC" | "Asia/Shanghai") => `${new Intl.DateTimeFormat(locale, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZone: zone }).format(date)} ${zone === "UTC" ? "UTC" : "UTC+8"}`;
 return <time dateTime={date.toISOString()} title={format("UTC")}>{timeMode === "dual" ? <>{format("Asia/Shanghai")}<br />{format("UTC")}</> : format(timeMode)}</time>;
}
