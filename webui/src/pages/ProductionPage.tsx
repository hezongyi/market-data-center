import { useCallback, useEffect, useState } from "react";
import { CalendarClock, CirclePause, CirclePlay, RefreshCw, Trash2, Archive, Zap } from "lucide-react";
import {
  ConfirmDialog, DataTable, EmptyState, ErrorState, LoadingSkeleton, PageHeader, PanelHeading, StatusBadge,
} from "../components/ui";
import { TimeDisplay, usePreferences } from "../preferences";
import type { Capabilities, ProductionExecution, ProductionPlan, ProductionPreview, SchedulerView } from "../lib/api";
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

type DefinitionDraft = {
  provider: string; symbol: string; raw_timeframe: string; price_basis: string;
  bar_timeframes: string[]; history_start: string; schedule: string;
  interval_minutes: number; timezone: string; local_time: string;
};

const blankDefinition = (): DefinitionDraft => ({
  provider: "", symbol: "", raw_timeframe: "1m", price_basis: "",
  bar_timeframes: [], history_start: localDateTime(new Date(Date.now() - 7 * 86400_000)),
  schedule: "manual", interval_minutes: 15, timezone: "UTC", local_time: "08:00",
});

/** A datetime-local value is a wall clock, not an instant. */
const localDateTime = (value: Date) => {
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
    + `T${pad(value.getHours())}:${pad(value.getMinutes())}`;
};

/** Seed the wizard from the registry so it never offers a value the API rejects. */
const seedFromRegistry = (registry: Capabilities): DefinitionDraft => {
  const provider = registry.providers[0];
  return {
    ...blankDefinition(),
    provider: provider?.provider ?? "",
    symbol: provider?.instruments?.[0]?.symbol ?? "",
    raw_timeframe: provider?.maintenance_timeframes?.[0] ?? provider?.timeframes?.[0] ?? "1m",
    price_basis: provider?.price_bases?.[0] ?? "",
  };
};

/** Render the wizard draft as the definition the API validates. */
const definitionBody = (draft: DefinitionDraft) => ({
  provider: draft.provider,
  symbol: draft.symbol,
  raw_timeframe: draft.raw_timeframe,
  price_basis: draft.price_basis,
  bar_timeframes: draft.bar_timeframes,
  window_policy: {
    mode: "continuous",
    // A local datetime is a wall clock; the console sends the instant in UTC.
    history_start: draft.history_start ? new Date(draft.history_start).toISOString() : "",
  },
  schedule: draft.schedule === "manual" || draft.schedule === "fixed_delay"
    ? { schedule: draft.schedule, interval_seconds: draft.interval_minutes * 60 }
    : draft.schedule === "daily"
      ? { schedule: "daily", timezone: draft.timezone, local_time: draft.local_time }
      : draft.schedule === "fixed_rate"
        ? { schedule: "fixed_rate", interval_seconds: draft.interval_minutes * 60 }
        : { schedule: draft.schedule },
});

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
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [definition, setDefinition] = useState(() => blankDefinition());
  const [planName, setPlanName] = useState("");
  const [preview, setPreview] = useState<ProductionPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [steps, setSteps] = useState<Array<{ step_id: string; stage: string; state: string; block_reason: string | null; window_start: string | null; window_end: string | null }>>([]);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [registered, schedulerView, registry] = await Promise.all([
        services.production.plans({ page_size: 50 }),
        services.production.scheduler(),
        services.catalog.capabilities(),
      ]);
      setPlans(registered);
      setScheduler(schedulerView);
      setCapabilities(registry);
      // A draft with no provider cannot be validated; seed it from the registry
      // without discarding edits the operator already made.
      setDefinition(current => current.provider ? current : seedFromRegistry(registry));
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

  // The wizard only offers what the registry reports: a plan for an unapproved
  // instrument is refused by the API, so the console does not offer it at all.
  const providerOptions = capabilities?.providers ?? [];
  const selectedProvider = providerOptions.find(item => item.provider === definition.provider) ?? providerOptions[0];
  const symbolOptions = selectedProvider?.instruments ?? [];
  const derivedTimeframes = capabilities?.production?.outputs
    ?.find(item => item.dataset_id === "market_bars")?.timeframes ?? [];
  const minimumInterval = capabilities?.production?.minimum_interval_seconds ?? 300;
  const scheduleKinds = capabilities?.production?.schedule_kinds ?? ["manual", "once", "fixed_rate", "fixed_delay", "daily"];

  const update = (patch: Record<string, unknown>) => {
    setDefinition(current => ({ ...current, ...patch }));
    // Any edit invalidates a preview: showing stale validation would be worse
    // than showing none.
    setPreview(null);
  };

  const runPreview = async () => {
    setPreviewing(true);
    onMessage("");
    try {
      setPreview(await services.production.preview(definitionBody(definition)));
    } catch (reason) {
      onMessage(`preview refused: ${(reason as Error).message}`);
    } finally {
      setPreviewing(false);
    }
  };

  const save = async (desiredState: "paused" | "enabled") => {
    setBusy(`create:${desiredState}`);
    onMessage("");
    try {
      await services.production.create({
        name: planName || `${definition.provider}/${definition.symbol}`,
        definition: definitionBody(definition),
        desired_state: desiredState,
      }, idempotencyKey("create", planName || definition.symbol));
      onMessage(desiredState === "enabled" ? "Plan saved and enabled." : "Plan saved as paused.");
      setDefinition(blankDefinition());
      setPlanName("");
      setPreview(null);
      await load();
      onChanged();
    } catch (reason) {
      onMessage(`save refused: ${(reason as Error).message}`);
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
      <PanelHeading eyebrow="Wizard" title="New plan" action={
        <div className="row-actions">
          <button className="secondary-button" onClick={() => void runPreview()} disabled={previewing}>
            {previewing ? t("Previewing…") : t("Preview")}
          </button>
          <button className="secondary-button" disabled={busy === "create:paused"} onClick={() => void save("paused")}>{t("Save as paused")}</button>
          <button className="primary-button" disabled={busy === "create:enabled"} onClick={() => void save("enabled")}>{t("Save and enable")}</button>
        </div>
      } />
      <div className="form-grid">
        <label>{t("Name")}<input value={planName} onChange={event => setPlanName(event.target.value)} placeholder="EURUSD continuous" /></label>
        <label>{t("Provider")}<select aria-label={t("Provider")} value={definition.provider}
          onChange={event => {
            const provider = event.target.value;
            const first = providerOptions.find(item => item.provider === provider)?.instruments?.[0]?.symbol ?? "";
            update({ provider, symbol: first });
          }}>
          {providerOptions.map(item => <option key={item.provider} value={item.provider}>{item.provider}</option>)}
        </select></label>
        <label>{t("Symbol")}<select aria-label={t("Symbol")} value={definition.symbol}
          onChange={event => update({ symbol: event.target.value })}>
          {symbolOptions.map(item => <option key={item.symbol} value={item.symbol}>{item.symbol}</option>)}
        </select></label>
        <label>{t("Raw timeframe")}<select aria-label={t("Raw timeframe")} value={definition.raw_timeframe}
          onChange={event => update({ raw_timeframe: event.target.value })}>
          {(selectedProvider?.maintenance_timeframes ?? selectedProvider?.timeframes ?? []).map(item => <option key={item} value={item}>{item}</option>)}
        </select></label>
        <label>{t("Price basis")}<select aria-label={t("Price basis")} value={definition.price_basis}
          onChange={event => update({ price_basis: event.target.value })}>
          {(selectedProvider?.price_bases ?? []).map(item => <option key={item} value={item}>{item}</option>)}
        </select></label>
        <label>{t("Derived outputs")}<select aria-label={t("Derived outputs")} multiple size={4}
          value={definition.bar_timeframes}
          onChange={event => update({ bar_timeframes: [...event.target.selectedOptions].map(option => option.value) })}>
          {derivedTimeframes.map(item => <option key={item} value={item}>{item}</option>)}
        </select></label>
        <label>{t("History start")}<input type="datetime-local" aria-label={t("History start")}
          value={definition.history_start} onChange={event => update({ history_start: event.target.value })} /></label>
        <label>{t("Schedule")}<select aria-label={t("Schedule")} value={definition.schedule}
          onChange={event => update({ schedule: event.target.value })}>
          {scheduleKinds.map(item => <option key={item} value={item}>{item}</option>)}
        </select></label>
        {(definition.schedule === "fixed_rate" || definition.schedule === "fixed_delay") &&
          <label>{t("Interval (minutes)")}<input type="number" min={Math.ceil(minimumInterval / 60)} aria-label={t("Interval (minutes)")}
            value={definition.interval_minutes}
            onChange={event => update({ interval_minutes: Number(event.target.value) })} /></label>}
        {definition.schedule === "daily" && <>
          <label>{t("Time zone")}<input aria-label={t("Time zone")} value={definition.timezone}
            onChange={event => update({ timezone: event.target.value })} /></label>
          <label>{t("Local time")}<input aria-label={t("Local time")} value={definition.local_time}
            onChange={event => update({ local_time: event.target.value })} /></label>
        </>}
      </div>
      {preview && <div className="preview">
        {preview.validation.errors.length > 0
          ? <ul className="warnings">{preview.validation.errors.map(item => <li key={`${item.field}-${item.message}`}><b>{item.field}</b>: {item.message}</li>)}</ul>
          : <>
            <p>{t("Submittable")}: <b>{preview.submittable ? t("yes") : t("conflicts with an existing plan")}</b> · {preview.ownership_keys.length} {t("ownership key(s)")}</p>
            <p>{t("Next runs")}: {preview.schedule.next_runs.length > 0
              ? preview.schedule.next_runs.map(value => <TimeDisplay key={value} value={value} />)
              : <span className="muted">{preview.schedule.rule ?? t("no scheduled time")}</span>}</p>
            <p>{t("Dependencies")}: {preview.dependencies.map(item => item.recipe_id).join(", ") || t("none")}</p>
            {preview.conflicts.length > 0 && <ul className="warnings">
              {preview.conflicts.map(item => <li key={item.ownership_key}><b>{item.ownership_key}</b>: {t("held by")} {item.task_id}</li>)}
            </ul>}
          </>}
      </div>}
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
