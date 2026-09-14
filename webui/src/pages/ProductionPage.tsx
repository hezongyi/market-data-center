import { useCallback, useEffect, useState } from "react";
import { CalendarClock, CirclePause, CirclePlay, RefreshCw, Trash2, Archive, Zap } from "lucide-react";
import {
  ConfirmDialog, DataTable, EmptyState, ErrorState, LoadingSkeleton, PageHeader, PanelHeading, StatusBadge,
} from "../components/ui";
import { TimeDisplay, usePreferences } from "../preferences";
import type { ProductionExecution, ProductionPlan, SchedulerView } from "../lib/api";
import type { Services } from "../services";
import type { ColumnDef } from "@tanstack/react-table";

// The console mirrors the plan contract the API reports: it never infers a plan
// state from a run, and the blocked reasons come from the scheduler view.
const healthTone = (health: string | null, state: string) =>
  state === "archived" ? "neutral" as const
    : health === "healthy" ? "good" as const
      : health === "config_drift" || health === "blocked" ? "bad" as const
        : ["lagging", "attention", "paused"].includes(health ?? "") || state === "paused" ? "warn" as const
          : "neutral" as const;

const outputs = (plan: ProductionPlan) => {
  const payload = plan.payload as { raw_timeframe?: string; bar_timeframes?: string[]; price_basis?: string };
  return [payload.raw_timeframe ?? "1m", ...(payload.bar_timeframes ?? [])].join(", ");
};

const idempotencyKey = (command: string, taskId: string) =>
  `ui-${command}-${taskId}-${Date.now()}-${Math.random().toString(16).slice(2)}`;

export function ProductionPage({ services, onMessage, onChanged }: {
  services: Services;
  onMessage: (message: string) => void;
  onChanged: () => void;
}) {
  const { t } = usePreferences();
  const [plans, setPlans] = useState<ProductionPlan[]>([]);
  const [scheduler, setScheduler] = useState<SchedulerView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [confirming, setConfirming] = useState<{ plan: ProductionPlan; command: string } | null>(null);
  const [selected, setSelected] = useState<ProductionPlan | null>(null);
  const [steps, setSteps] = useState<Array<{ step_id: string; stage: string; state: string; block_reason: string | null; window_start: string | null; window_end: string | null }>>([]);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [registered, schedulerView] = await Promise.all([
        services.production.plans({ page_size: 50 }),
        services.production.scheduler(),
      ]);
      setPlans(registered);
      setScheduler(schedulerView);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }, [services]);

  useEffect(() => { void load(); }, [load]);

  const act = async (plan: ProductionPlan, command: string) => {
    setBusy(`${plan.task_id}:${command}`);
    onMessage("");
    try {
      await services.production.act(plan.task_id, command, idempotencyKey(command, plan.task_id), {
        expected_version: plan.definition_version,
      });
      onMessage(`${command} accepted for ${plan.name}.`);
      await load();
      onChanged();
    } catch (reason) {
      onMessage(`${command} refused: ${(reason as Error).message}`);
    } finally {
      setBusy("");
      setConfirming(null);
    }
  };

  const openSteps = async (plan: ProductionPlan) => {
    setSelected(plan);
    setSteps([]);
    const execution = plan.current_execution ?? plan.executions?.[0];
    if (!execution) return;
    try {
      setSteps(await services.production.steps(execution.execution_id));
    } catch (reason) {
      onMessage(`steps unavailable: ${(reason as Error).message}`);
    }
  };

  const retry = async (execution: ProductionExecution) => {
    setBusy(execution.execution_id);
    try {
      const result = await services.production.retry(execution.execution_id, idempotencyKey("retry", execution.execution_id));
      onMessage(`retry planned ${String(result.planned_steps ?? 0)} step(s) as a linked round.`);
      await load();
      onChanged();
    } catch (reason) {
      onMessage(`retry refused: ${(reason as Error).message}`);
    } finally {
      setBusy("");
    }
  };

  const dispatch = async (command: "pause_dispatch" | "resume_dispatch") => {
    setBusy(command);
    try {
      await services.production.dispatch(command);
      await load();
      onMessage(command === "pause_dispatch" ? "Dispatch paused." : "Dispatch resumed.");
    } catch (reason) {
      onMessage(`${command} refused: ${(reason as Error).message}`);
    } finally {
      setBusy("");
    }
  };

  const columns: ColumnDef<ProductionPlan>[] = [
    {
      header: t("Plan"),
      accessorKey: "name",
      cell: info => <div className="cell-stack"><b>{info.row.original.name}</b><small>{info.row.original.task_id.slice(0, 8)} · v{info.row.original.definition_version}</small></div>,
    },
    {
      header: t("Selector"),
      cell: info => <div className="cell-stack"><span>{info.row.original.provider ?? "—"} / {info.row.original.symbol ?? "—"}</span><small>{outputs(info.row.original)}</small></div>,
    },
    {
      header: t("State"),
      cell: info => <StatusBadge tone={healthTone(info.row.original.health, info.row.original.desired_state)}>
        {info.row.original.health ?? info.row.original.desired_state}
      </StatusBadge>,
    },
    {
      header: t("Next run"),
      cell: info => info.row.original.next_run_at
        ? <TimeDisplay value={info.row.original.next_run_at} />
        : <span className="muted">{t(info.row.original.schedule?.kind === "fixed_delay" ? "after completion" : "not scheduled")}</span>,
    },
    {
      header: t("Current round"),
      cell: info => {
        const execution = info.row.original.current_execution;
        return execution
          ? <div className="cell-stack"><span>{execution.state}</span><small>{execution.trigger_source}</small></div>
          : <span className="muted">{t("idle")}</span>;
      },
    },
    {
      header: t("Actions"),
      cell: info => {
        const plan = info.row.original;
        const key = `${plan.task_id}:`;
        return <div className="row-actions">
          <button className="icon-button" title={t("Details")} aria-label={`${t("Details")} ${plan.name}`} onClick={() => void openSteps(plan)}>
            <CalendarClock size={16} />
          </button>
          {plan.desired_state === "enabled"
            ? <button className="icon-button" title={t("Pause")} aria-label={`${t("Pause")} ${plan.name}`} disabled={busy === `${key}pause`} onClick={() => void act(plan, "pause")}><CirclePause size={16} /></button>
            : <button className="icon-button" title={t("Resume")} aria-label={`${t("Resume")} ${plan.name}`} disabled={busy === `${key}resume`} onClick={() => void act(plan, "resume")}><CirclePlay size={16} /></button>}
          <button className="icon-button" title={t("Run now")} aria-label={`${t("Run now")} ${plan.name}`} disabled={busy === `${key}run_now`} onClick={() => void act(plan, "run_now")}><Zap size={16} /></button>
          <button className="icon-button" title={t("Archive")} aria-label={`${t("Archive")} ${plan.name}`} disabled={busy === `${key}archive`} onClick={() => setConfirming({ plan, command: "archive" })}><Archive size={16} /></button>
          <button className="icon-button danger" title={t("Delete")} aria-label={`${t("Delete")} ${plan.name}`} disabled={busy === `${key}delete`} onClick={() => setConfirming({ plan, command: "delete" })}><Trash2 size={16} /></button>
        </div>;
      },
    },
  ];

  return <>
    <PageHeader eyebrow="Production" title="Production plans" actions={
      <button className="secondary-button" onClick={() => void load()} disabled={loading}>
        <RefreshCw size={16} /> {t("Refresh")}
      </button>
    } />
    <section className="panel">
      <PanelHeading eyebrow="Scheduler" title="Unified dispatch" action={
        <div className="row-actions">
          {scheduler?.dispatch_enabled
            ? <button className="secondary-button" disabled={busy === "pause_dispatch"} onClick={() => void dispatch("pause_dispatch")}>{t("Pause dispatch")}</button>
            : <button className="primary-button" disabled={busy === "resume_dispatch"} onClick={() => void dispatch("resume_dispatch")}>{t("Resume dispatch")}</button>}
        </div>
      } />
      {scheduler
        ? <div className="metric-grid">
          <article className="metric"><span>{t("Dispatch")}</span><strong>{scheduler.dispatch_enabled ? t("enabled") : t("paused")}</strong><small>{scheduler.scheduler.instance_id ?? t("no instance")}</small></article>
          <article className="metric"><span>{t("Heartbeat")}</span><strong>{scheduler.scheduler.heartbeat_at ? <TimeDisplay value={scheduler.scheduler.heartbeat_at} /> : t("never")}</strong><small>{scheduler.scheduler.lease ? `${t("lease")} ${scheduler.scheduler.lease.owner_id}` : t("no lease")}</small></article>
          <article className={`metric${scheduler.due_now ? " metric-warn" : ""}`}><span>{t("Due now")}</span><strong>{scheduler.due_now}</strong><small>{scheduler.oldest_due_at ? <TimeDisplay value={scheduler.oldest_due_at} /> : t("nothing overdue")}</small></article>
          <article className="metric"><span>{t("Plans")}</span><strong>{Object.values(scheduler.plans_by_state).reduce((total, count) => total + count, 0)}</strong><small>{Object.entries(scheduler.plans_by_state).map(([state, count]) => `${count} ${state}`).join(" · ") || t("none")}</small></article>
        </div>
        : <LoadingSkeleton rows={1} />}
    </section>
    <section className="panel">
      <PanelHeading eyebrow="Registry" title="Registered plans" />
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {!error && loading && <LoadingSkeleton rows={3} />}
      {!error && !loading && plans.length === 0 && <EmptyState title="No production plans yet" detail="Plans are created through POST /production/tasks; the guide describes the definition fields." />}
      {!error && !loading && plans.length > 0 && <DataTable data={plans} columns={columns} />}
    </section>
    {selected && <section className="panel">
      <PanelHeading eyebrow="Round" title={`${selected.name} · ${selected.current_execution?.state ?? selected.executions?.[0]?.state ?? t("no round")}`} action={
        <div className="row-actions">
          {selected.executions?.some(execution => ["completed", "failed", "skipped"].includes(execution.state)) &&
            <button className="secondary-button" disabled={busy === selected.executions[0]?.execution_id}
                    onClick={() => void retry(selected.executions![0])}>{t("Retry last round")}</button>}
          <button className="secondary-button" onClick={() => setSelected(null)}>{t("Close")}</button>
        </div>
      } />
      <ol className="step-list">
        {steps.map(step => <li key={step.step_id}>
          <StatusBadge tone={step.state === "completed" ? "good" : step.state === "failed" ? "bad" : step.state === "blocked" ? "bad" : "warn"}>{step.state}</StatusBadge>
          <b>{step.stage}</b>
          <small>{step.window_start ?? "—"} → {step.window_end ?? "—"}</small>
          {step.block_reason && <small>{step.block_reason}</small>}
        </li>)}
        {steps.length === 0 && <li><span className="muted">{t("No persisted steps for this round.")}</span></li>}
      </ol>
    </section>}
    {confirming && <ConfirmDialog
      title={confirming.command === "delete" ? t("Delete this plan?") : t("Archive this plan?")}
      detail={confirming.command === "delete"
        ? t("Deletes the plan definition only: published data, runs, receipts and history are retained.")
        : t("Archiving stops the plan and releases its output ownership; history is retained.")}
      confirmLabel={confirming.command === "delete" ? t("Delete") : t("Archive")}
      busy={Boolean(busy)}
      onConfirm={() => void act(confirming.plan, confirming.command)}
      onCancel={() => setConfirming(null)} />}
  </>;
}
