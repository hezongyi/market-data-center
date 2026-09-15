import type { SchedulerView } from "../lib/api";

export type SchedulerLoadState = SchedulerView | null | undefined;

export function schedulerCardLabel(state: SchedulerLoadState): string {
  if (state === undefined) return "载入中…";
  if (state === null) return "状态不可用";
  if (state.effective_dispatch) return "有效";
  if (!state.dispatch_enabled) return "全局暂停";
  if (state.heartbeat_status === "stale") return "心跳过期";
  if (state.heartbeat_status === "unknown") return "等待心跳";
  if (!state.scheduler.instance_dispatch_enabled) return "实例未派发";
  return "未有效派发";
}

export function schedulerBannerLabel(state: SchedulerLoadState): string {
  if (state === undefined) return "调度状态载入中";
  if (state === null) return "调度状态不可用";
  const heartbeat = state.scheduler.heartbeat_at ?? "等待中";
  return `调度：${schedulerCardLabel(state)} · 心跳 ${heartbeat}`;
}
