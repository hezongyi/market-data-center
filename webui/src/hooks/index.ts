/**
 * Unified console state model.
 *
 * Queries are idle | loading | success | empty | error.  Writes are draft |
 * validating | confirming | queued | running | pass | degraded | failed.
 * Permission is authorized | unauthorized | protected.  Freshness is fresh |
 * stale | unknown.  Every page renders these same states, so a degraded or
 * protected outcome can never be mistaken for a plain failure.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataCenterError, type QueuedEnvelope, type RunDetail, type RunScope } from "../lib/api";
import type { Services } from "../services";

export type QueryStatus = "idle" | "loading" | "success" | "empty" | "error";
export type PermissionState = "authorized" | "unauthorized" | "protected";
export type Freshness = "fresh" | "stale" | "unknown";
export type WriteState = "draft" | "validating" | "confirming" | "queued" | "running" | "pass" | "degraded" | "failed";

export type QueryResult<T> = {
  status: QueryStatus;
  data: T | null;
  error: string | null;
  requestId: string | null;
  permission: PermissionState;
  reload: () => void;
};

export const permissionOf = (reason: unknown): PermissionState => {
  if (reason instanceof DataCenterError) {
    if (reason.status === 401 || reason.status === 403) return "unauthorized";
    if (reason.status === 507) return "protected";
  }
  return "authorized";
};

export const messageOf = (reason: unknown): string => {
  if (reason instanceof DataCenterError) return reason.message;
  return reason instanceof Error ? reason.message : "Request failed";
};

export const requestIdOf = (reason: unknown): string | null =>
  reason instanceof DataCenterError ? reason.requestId ?? null : null;

export function freshnessOf(status: { status?: string; operational_snapshot_status?: string } | null): Freshness {
  if (!status) return "unknown";
  if (status.status === "ready" && status.operational_snapshot_status === "fresh") return "fresh";
  return status.operational_snapshot_status ? "stale" : "unknown";
}

/** Load-once query with explicit reload and stale-response protection. */
export function useQuery<T>(load: () => Promise<T>, deps: unknown[], options: { refreshToken?: number } = {}): QueryResult<T> {
  const [status, setStatus] = useState<QueryStatus>("loading");
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [permission, setPermission] = useState<PermissionState>("authorized");
  const [token, setToken] = useState(0);
  const generation = useRef(0);
  const loader = useRef(load);
  loader.current = load;

  useEffect(() => {
    const current = ++generation.current;
    let active = true;
    setStatus("loading");
    setError(null);
    loader.current()
      .then(result => {
        if (!active || current !== generation.current) return;
        setData(result);
        setPermission("authorized");
        setStatus(Array.isArray(result) && result.length === 0 ? "empty" : "success");
      })
      .catch(reason => {
        if (!active || current !== generation.current) return;
        setError(messageOf(reason));
        setRequestId(requestIdOf(reason));
        setPermission(permissionOf(reason));
        setStatus("error");
      });
    return () => { active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, token, options.refreshToken ?? 0]);

  const reload = useCallback(() => setToken(value => value + 1), []);
  return { status, data, error, requestId, permission, reload };
}

export type MaintenanceTaskInput = Parameters<Services["maintenance"]["preview"]>[0];
export type TaskPreviewResult = Awaited<ReturnType<Services["maintenance"]["preview"]>>;

export type MaintenanceMutation = {
  state: WriteState;
  preview: TaskPreviewResult | null;
  envelope: QueuedEnvelope | null;
  error: string | null;
  permission: PermissionState;
  warnings: Array<{ code: string; message: string }>;
  validate: (task: MaintenanceTaskInput) => Promise<boolean>;
  edit: () => void;
  submit: () => Promise<void>;
  reset: () => void;
};

/**
 * The maintenance write state machine.
 *
 * ``validate`` is a pure preview, ``edit`` returns to the form, ``submit``
 * queues the task, and ``queued`` is the only state a successful submission
 * returns: run progress is tracked separately so the console never invents a
 * terminal result for queued work.
 */
export function useMaintenanceMutation(
  services: Services,
  onQueued?: (envelope: QueuedEnvelope) => void,
): MaintenanceMutation {
  const [state, setState] = useState<WriteState>("draft");
  const [preview, setPreview] = useState<TaskPreviewResult | null>(null);
  const [envelope, setEnvelope] = useState<QueuedEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [permission, setPermission] = useState<PermissionState>("authorized");
  const [warnings, setWarnings] = useState<Array<{ code: string; message: string }>>([]);
  const [task, setTask] = useState<MaintenanceTaskInput | null>(null);

  const validate = useCallback(async (next: MaintenanceTaskInput) => {
    setState("validating");
    setError(null);
    setTask(next);
    try {
      const result = await services.maintenance.preview(next);
      setPreview(result);
      setWarnings(result.validation.warnings);
      setPermission(result.write_status === "protected" ? "protected" : "authorized");
      setState(result.submittable ? "confirming" : "draft");
      return result.submittable;
    } catch (reason) {
      setError(messageOf(reason));
      setPermission(permissionOf(reason));
      setState("failed");
      return false;
    }
  }, [services]);

  const submit = useCallback(async () => {
    if (!task) return;
    setState("queued");
    setError(null);
    try {
      const result = await services.maintenance.submit(task);
      setEnvelope(result);
      setWarnings(current => [...current, ...result.warnings]);
      onQueued?.(result);
    } catch (reason) {
      setError(messageOf(reason));
      setPermission(permissionOf(reason));
      setState("failed");
    }
  }, [onQueued, services, task]);

  const reset = useCallback(() => {
    setState("draft");
    setPreview(null);
    setEnvelope(null);
    setError(null);
    setPermission("authorized");
    setWarnings([]);
    setTask(null);
  }, []);

  const edit = useCallback(() => setState(preview ? "confirming" : "draft"), [preview]);

  return useMemo(() => ({
    state, preview, envelope, error, permission, warnings, validate, edit, submit, reset,
  }), [edit, envelope, error, permission, preview, reset, state, submit, validate, warnings]);
}

/** Poll every tracked run until all of them reach a terminal receipt. */
export function useRunTracker(services: Services, runIds: string[]) {
  const key = runIds.join(",");
  const [runs, setRuns] = useState<RunDetail[]>([]);
  const [state, setState] = useState<WriteState>(runIds.length ? "queued" : "draft");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runIds.length) { setRuns([]); setState("draft"); return; }
    let active = true;
    let attempts = 0;
    let timer: number | undefined;
    setState("queued");
    const tick = async () => {
      attempts += 1;
      try {
        const loaded = await Promise.all(runIds.map(id => services.maintenance.track(id)));
        if (!active) return;
        setRuns(loaded);
        const settled = loaded.every(run => services.runs.isTerminal(run));
        const failed = loaded.some(run => run.status === "failed" || run.status === "dead_letter");
        const degraded = loaded.some(run => run.outcome === "degraded");
        if (settled) {
          setState(failed ? "failed" : degraded ? "degraded" : "pass");
          return;
        }
        setState(loaded.some(run => run.status === "running") ? "running" : "queued");
        if (attempts < 90) timer = window.setTimeout(() => { void tick(); }, 1500);
      } catch (reason) {
        if (!active) return;
        setError(messageOf(reason));
        setState("failed");
      }
    };
    void tick();
    return () => { active = false; if (timer) window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, services]);

  return { runs, state, error };
}

export const runScopeOptions: Array<{ value: RunScope; label: string }> = [
  { value: "production", label: "Production" },
  { value: "maintenance", label: "Maintenance" },
  { value: "migration", label: "Migration" },
  { value: "acceptance", label: "Acceptance" },
];
