import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";
import {
  ArrowLeft,
  CalendarClock,
  ChevronRight,
  CircleAlert,
  CircleCheckBig,
  CirclePlay,
  Copy,
  Database,
  ListChecks,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
} from "lucide-react";
import type {
  Bar,
  Capabilities,
  ProductionPlan,
  ProductionPreview,
  ProductionStep,
} from "../lib/api";
import { schedulerCardLabel } from "./scheduler-status";
import { createServices } from "../services";
import { Badge } from "../components/shadcn/badge";
import { Button } from "../components/shadcn/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/shadcn/card";
import { Input } from "../components/shadcn/input";
import { Label } from "../components/shadcn/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/shadcn/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "../components/shadcn/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/shadcn/table";

const services = createServices("");
const createIdempotencySuffix = () =>
  `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const datetimeLocal = (date: Date) => {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
};
const completeTradingWindow = () => {
  const end = new Date();
  end.setUTCHours(0, 0, 0, 0);
  const start = new Date(end.getTime() - 86400_000);
  while (start.getUTCDay() === 0 || start.getUTCDay() === 6) {
    start.setUTCDate(start.getUTCDate() - 1);
    end.setUTCDate(end.getUTCDate() - 1);
  }
  return { start: datetimeLocal(start), end: datetimeLocal(end) };
};
type Draft = {
  name: string;
  provider: string;
  symbol: string;
  rawTimeframe: string;
  priceBasis: string;
  windowMode: string;
  historyStart: string;
  historyEnd: string;
  schedule: string;
  intervalMinutes: number;
  derived5m: boolean;
};
const emptyDraft = (): Draft => {
  const window = completeTradingWindow();
  return {
    name: "",
    provider: "",
    symbol: "",
    rawTimeframe: "",
    priceBasis: "",
    windowMode: "fixed",
    historyStart: window.start,
    historyEnd: window.end,
    schedule: "manual",
    intervalMinutes: 15,
    derived5m: true,
  };
};
const definitionOf = (draft: Draft) => ({
  provider: draft.provider,
  symbol: draft.symbol,
  raw_timeframe: draft.rawTimeframe,
  price_basis: draft.priceBasis,
  bar_timeframes: draft.derived5m ? ["5m"] : [],
  window_policy: {
    mode: draft.windowMode,
    ...(draft.windowMode === "fixed"
      ? {
          start: draft.historyStart ? new Date(draft.historyStart).toISOString() : "",
          end: draft.historyEnd ? new Date(draft.historyEnd).toISOString() : "",
        }
      : {
          history_start: draft.historyStart
            ? new Date(draft.historyStart).toISOString()
            : "",
        }),
  },
  schedule:
    draft.schedule === "manual"
      ? { schedule: "manual", interval_seconds: draft.intervalMinutes * 60 }
      : {
          schedule: "fixed_delay",
          interval_seconds: draft.intervalMinutes * 60,
        },
});
const stateVariant = (plan: ProductionPlan) =>
  plan.health === "blocked" || plan.health === "config_drift"
    ? ("destructive" as const)
    : plan.desired_state === "enabled"
      ? ("default" as const)
      : ("secondary" as const);
const outputOf = (plan: ProductionPlan) => {
  const payload = plan.payload as {
    raw_timeframe?: string;
    bar_timeframes?: string[];
  };
  return [
    payload.raw_timeframe ?? "不可用",
    ...(payload.bar_timeframes ?? []),
  ].join(" → ");
};
const formatTime = (value: string | null | undefined) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "UTC",
      }).format(new Date(value)) + " UTC"
    : "—";
const formatPrice = (value: number) =>
  new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 }).format(value);
const executionStateLabel = (state: string) => ({
  pending: "待处理",
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
  pausing: "暂停中",
  paused: "已暂停",
}[state] ?? `未知状态（${state}）`);
const executionOutcomeLabel = (outcome: string | null) => outcome ? ({
  pass: "通过",
  failed: "失败",
  degraded: "降级",
  skipped: "已跳过",
}[outcome] ?? `未知结果（${outcome}）`) : "—";

function CopyRequestButton({ request }: { request: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() =>
        void navigator.clipboard.writeText(request).then(() => setCopied(true))
      }
    >
      <Copy data-icon="inline-start" />
      {copied ? "已复制" : "复制 HTTP 请求"}
    </Button>
  );
}

function CopyValueButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      title={value}
      variant="ghost"
      size="sm"
      onClick={() =>
        void navigator.clipboard.writeText(value).then(() => setCopied(true))
      }
    >
      <span className="mono">{value.slice(0, 12)}</span>
      <Copy data-icon="inline-end" />
      <span className="sr-only">{copied ? "已复制" : `复制${label}`}</span>
    </Button>
  );
}

function BarsTable({ rows }: { rows: Bar[] }) {
  if (!rows.length)
    return <p className="m-0 text-sm text-muted-foreground">尚未发布数据。</p>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>时间（UTC）</TableHead>
          <TableHead>开</TableHead>
          <TableHead>高</TableHead>
          <TableHead>低</TableHead>
          <TableHead>收</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.slice(-5).map((row) => (
          <TableRow key={row.bar_ts}>
            <TableCell className="mono text-xs">{formatTime(row.bar_ts)}</TableCell>
            <TableCell>{formatPrice(row.open)}</TableCell>
            <TableCell>{formatPrice(row.high)}</TableCell>
            <TableCell>{formatPrice(row.low)}</TableCell>
            <TableCell>{formatPrice(row.close)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function ErrorNotice({ error }: { error: unknown }) {
  return (
    <div
      role="alert"
      className="flex gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive"
    >
      <CircleAlert className="size-4 shrink-0" />
      <span>{error instanceof Error ? error.message : "请求失败"}</span>
    </div>
  );
}

function ChoiceSelect({
  value,
  onValueChange,
  options,
  placeholder,
  ariaLabel,
  id,
  disabled,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
  placeholder: string;
  ariaLabel: string;
  id?: string;
  disabled?: boolean;
}) {
  return (
    <Select
      value={value || undefined}
      onValueChange={onValueChange}
      disabled={disabled}
    >
      <SelectTrigger id={id} aria-label={ariaLabel}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function CreateTaskSheet({
  open,
  onOpenChange,
  capabilities,
  capabilitiesLoading,
  capabilitiesError,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  capabilities?: Capabilities;
  capabilitiesLoading: boolean;
  capabilitiesError: unknown;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [draft, setDraft] = useState(emptyDraft);
  const [preview, setPreview] = useState<ProductionPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [createKey, setCreateKey] = useState(
    () => `ui-create-${createIdempotencySuffix()}`,
  );
  const providers = capabilities?.providers ?? [];
  const selectedProvider =
    providers.find((item) => item.provider === draft.provider) ?? providers[0];
  const provider = draft.provider || selectedProvider?.provider || "";
  const rawTimeframe =
    draft.rawTimeframe || selectedProvider?.maintenance_timeframes?.[0] || "";
  const priceBasis = draft.priceBasis || selectedProvider?.price_bases?.[0] || "";
  const canDerive5m =
    capabilities?.recipes.some(
      (recipe) =>
        recipe.input_dataset === "provider_bars" &&
        recipe.output_dataset === "market_bars" &&
        recipe.source_timeframe === rawTimeframe &&
        recipe.target_timeframe === "5m" &&
        (!recipe.providers.length || recipe.providers.includes(provider)) &&
        (!recipe.price_bases.length || recipe.price_bases.includes(priceBasis)),
    ) ?? false;
  const effectiveDraft = (): Draft => ({
    ...draft,
    provider: draft.provider || selectedProvider?.provider || "",
    symbol: draft.symbol || selectedProvider?.instruments?.[0]?.symbol || "",
    rawTimeframe:
      draft.rawTimeframe || selectedProvider?.maintenance_timeframes?.[0] || "",
    priceBasis: draft.priceBasis || selectedProvider?.price_bases?.[0] || "",
    derived5m: draft.derived5m && canDerive5m,
  });
  const update = (patch: Partial<Draft>) => {
    setDraft((value) => ({ ...value, ...patch }));
    setPreview(null);
    setError(null);
  };
  const previewMutation = useMutation({
    mutationFn: () =>
      services.production.preview(definitionOf(effectiveDraft())),
    onSuccess: setPreview,
    onError: setError,
  });
  const createMutation = useMutation({
    mutationFn: () =>
      services.production.create(
        {
          name:
            draft.name ||
            `${effectiveDraft().provider}/${effectiveDraft().symbol}`,
          definition: definitionOf(effectiveDraft()),
          desired_state: "paused",
        },
        createKey,
      ),
    onSuccess: async (plan) => {
      await queryClient.invalidateQueries({ queryKey: ["production-plans"] });
      onOpenChange(false);
      setDraft(emptyDraft());
      setCreateKey(`ui-create-${createIdempotencySuffix()}`);
      setPreview(null);
      void navigate({
        to: "/tasks/$taskId",
        params: { taskId: plan.task_id },
        search: { q: "", health: "" },
      });
    },
    onError: setError,
  });
  const instruments = selectedProvider?.instruments ?? [];
  return (
    <Sheet
      open={open}
      onOpenChange={(value) => {
        onOpenChange(value);
        if (!value && !createMutation.isPending) {
          setPreview(null);
          setError(null);
        }
      }}
    >
      <SheetContent className="sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>创建数据任务</SheetTitle>
          <SheetDescription>
            选项来自 capabilities API。保存后在详情页启用并手动运行，查看原始
            1m 与派生 5m 数据。
          </SheetDescription>
        </SheetHeader>
        <div className="grid flex-1 gap-5 overflow-y-auto px-6 py-4">
          {capabilitiesLoading && (
            <div className="text-sm text-muted-foreground">
              正在载入可用能力…
            </div>
          )}
          {capabilitiesError != null && (
            <ErrorNotice error={capabilitiesError} />
          )}
          <div className="field">
            <Label htmlFor="task-name">任务名称</Label>
            <Input
              id="task-name"
              placeholder="例如 EURUSD 1m → 5m"
              value={draft.name}
              onChange={(e) => update({ name: e.target.value })}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="field">
              <Label htmlFor="provider">数据源</Label>
              <ChoiceSelect
                id="provider"
                ariaLabel="数据源"
                value={provider}
                placeholder="选择数据源"
                options={providers.map((item) => ({
                  value: item.provider,
                  label: item.provider,
                }))}
                onValueChange={(value) => {
                  const next = providers.find(
                    (item) => item.provider === value,
                  );
                  update({
                    provider: value,
                    symbol: next?.instruments?.[0]?.symbol ?? "",
                    rawTimeframe: next?.maintenance_timeframes?.[0] ?? "",
                    priceBasis: next?.price_bases?.[0] ?? "",
                  });
                }}
              />
            </div>
            <div className="field">
              <Label htmlFor="symbol">品种</Label>
              <ChoiceSelect
                id="symbol"
                ariaLabel="品种"
                value={draft.symbol || instruments[0]?.symbol || ""}
                placeholder="无可用品种"
                disabled={instruments.length === 0}
                options={instruments.map((item) => ({
                  value: item.symbol,
                  label: item.symbol,
                }))}
                onValueChange={(value) => update({ symbol: value })}
              />
              {instruments.length === 0 && (
                <p className="m-0 text-xs text-muted-foreground">
                  当前 fixture capabilities 未注册品种；不能手输绕过能力校验。P2
                  接入 EURUSD 后可选。
                </p>
              )}
            </div>
            <div className="field">
              <Label htmlFor="basis">价格基准</Label>
              <ChoiceSelect
                id="basis"
                ariaLabel="价格基准"
                value={
                  draft.priceBasis || selectedProvider?.price_bases?.[0] || ""
                }
                placeholder="选择价格基准"
                options={(selectedProvider?.price_bases ?? []).map((item) => ({
                  value: item,
                  label: item,
                }))}
                onValueChange={(value) => update({ priceBasis: value })}
              />
            </div>
            <div className="field">
              <Label htmlFor="raw-timeframe">原始周期</Label>
              <ChoiceSelect
                id="raw-timeframe"
                ariaLabel="原始周期"
                value={
                  draft.rawTimeframe ||
                  selectedProvider?.maintenance_timeframes?.[0] ||
                  ""
                }
                placeholder="选择原始周期"
                options={(
                  selectedProvider?.maintenance_timeframes ??
                  selectedProvider?.timeframes ??
                  []
                ).map((item) => ({ value: item, label: item }))}
                onValueChange={(value) => update({ rawTimeframe: value })}
              />
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="field">
              <Label htmlFor="window-mode">数据窗口</Label>
              <ChoiceSelect
                id="window-mode"
                ariaLabel="数据窗口"
                value={draft.windowMode}
                placeholder="选择窗口"
                options={[
                  { value: "fixed", label: "固定窗口" },
                  { value: "continuous", label: "从历史起点持续维护" },
                ]}
                onValueChange={(value) => update({ windowMode: value })}
              />
            </div>
            <div className="field">
              <Label htmlFor="history-start">
                {draft.windowMode === "fixed" ? "窗口开始" : "历史起点"}
              </Label>
              <Input
                id="history-start"
                type="datetime-local"
                value={draft.historyStart}
                onChange={(e) => update({ historyStart: e.target.value })}
              />
            </div>
            {draft.windowMode === "fixed" && (
              <div className="field sm:col-start-2">
                <Label htmlFor="history-end">窗口结束</Label>
                <Input
                  id="history-end"
                  type="datetime-local"
                  value={draft.historyEnd}
                  onChange={(e) => update({ historyEnd: e.target.value })}
                />
              </div>
            )}
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="field">
              <Label htmlFor="schedule">维护方式</Label>
              <ChoiceSelect
                id="schedule"
                ariaLabel="维护方式"
                value={draft.schedule}
                placeholder="选择维护方式"
                options={[
                  { value: "manual", label: "手动" },
                  { value: "fixed_delay", label: "完成后间隔" },
                ]}
                onValueChange={(value) => update({ schedule: value })}
              />
            </div>
            <div className="field">
              <Label htmlFor="interval">间隔（分钟）</Label>
              <Input
                id="interval"
                type="number"
                min={5}
                value={draft.intervalMinutes}
                onChange={(e) =>
                  update({ intervalMinutes: Number(e.target.value) })
                }
              />
            </div>
          </div>
          <label className="flex items-center gap-3 rounded-lg border p-3 text-sm">
            <input
              type="checkbox"
              checked={draft.derived5m}
              disabled={!canDerive5m}
              onChange={(e) => update({ derived5m: e.target.checked })}
            />
            派生 5m 数据
          </label>
          {error != null && <ErrorNotice error={error} />}
          {preview && (
            <div
              className={`rounded-lg border p-4 text-sm ${preview.submittable ? "border-emerald-300 bg-emerald-50 text-emerald-900" : "border-destructive/30 bg-destructive/5"}`}
            >
              <strong>
                {preview.submittable ? "校验通过，可以保存" : "暂不能保存"}
              </strong>
              {preview.validation.errors.map((item) => (
                <p key={`${item.field}:${item.message}`} className="mb-0">
                  <span className="mono">{item.field}</span>：{item.message}
                </p>
              ))}
              {preview.conflicts.length > 0 && (
                <p className="mb-0">已有任务占用相同数据身份。</p>
              )}
            </div>
          )}
        </div>
        <SheetFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          {preview?.submittable ? (
            <Button
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending}
            >
              {createMutation.isPending ? "保存中…" : "保存为暂停"}
            </Button>
          ) : (
            <Button
              onClick={() => previewMutation.mutate()}
              disabled={
                previewMutation.isPending ||
                instruments.length === 0 ||
                capabilitiesLoading
              }
            >
              {previewMutation.isPending ? "校验中…" : "校验任务"}
            </Button>
          )}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

export function TasksPage() {
  const search = useSearch({ from: "/tasks" });
  const navigate = useNavigate({ from: "/tasks" });
  const [open, setOpen] = useState(false);
  const plans = useQuery({
    queryKey: ["production-plans", search.health],
    queryFn: () =>
      services.production.plans({
        page_size: 50,
        ...(search.health ? { health: search.health } : {}),
      }),
  });
  const capabilities = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => services.catalog.capabilities(),
  });
  const scheduler = useQuery({
    queryKey: ["scheduler"],
    queryFn: () => services.production.scheduler(),
    refetchInterval: 5000,
  });
  const rows = useMemo(
    () =>
      (plans.data ?? []).filter(
        (plan) =>
          !search.q ||
          `${plan.name} ${plan.task_id} ${plan.provider} ${plan.symbol}`
            .toLowerCase()
            .includes(search.q.toLowerCase()),
      ),
    [plans.data, search.q],
  );
  return (
    <>
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-1 text-sm text-muted-foreground">数据维护 / 任务</p>
          <h1 className="m-0 text-2xl font-bold tracking-tight">数据任务</h1>
          <p className="mb-0 mt-2 text-sm text-muted-foreground">
            维护原始与派生市场数据；任务状态不等同于运行或数据完整性。
          </p>
        </div>
        <Button onClick={() => setOpen(true)}>
          <Plus />
          创建任务
        </Button>
      </header>
      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <Card className="gap-2 py-4">
          <CardHeader className="px-4">
            <CardDescription>任务总数</CardDescription>
            <CardTitle className="text-2xl">
              {plans.data?.length ?? "—"}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card className="gap-2 py-4">
          <CardHeader className="px-4">
            <CardDescription>调度派发</CardDescription>
            <CardTitle className="text-lg">
              {schedulerCardLabel(
                scheduler.isLoading
                  ? undefined
                  : scheduler.error
                    ? null
                    : scheduler.data,
              )}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card className="gap-2 py-4">
          <CardHeader className="px-4">
            <CardDescription>当前到期</CardDescription>
            <CardTitle className="text-2xl">
              {scheduler.data?.due_now ?? "—"}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle>维护计划</CardTitle>
              <CardDescription className="mt-2">
                搜索与筛选保存在 URL；点击一行进入可刷新详情。
              </CardDescription>
            </div>
            <Button
              variant="outline"
              size="icon"
              aria-label="刷新任务"
              onClick={() => void plans.refetch()}
            >
              <RefreshCw className={plans.isFetching ? "animate-spin" : ""} />
            </Button>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <div className="relative min-w-56 flex-1">
              <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
              <Input
                aria-label="搜索任务"
                className="pl-9"
                placeholder="搜索名称、品种或 ID…"
                value={search.q}
                onChange={(e) =>
                  void navigate({
                    search: (old) => ({ ...old, q: e.target.value }),
                    replace: true,
                  })
                }
              />
            </div>
            <div className="min-w-44">
              <ChoiceSelect
                ariaLabel="健康筛选"
                value={search.health || "all"}
                placeholder="全部健康状态"
                options={[
                  { value: "all", label: "全部健康状态" },
                  { value: "healthy", label: "健康" },
                  { value: "attention", label: "需关注" },
                  { value: "blocked", label: "阻塞" },
                  { value: "config_drift", label: "配置漂移" },
                ]}
                onValueChange={(value) =>
                  void navigate({
                    search: (old) => ({
                      ...old,
                      health: value === "all" ? "" : value,
                    }),
                    replace: true,
                  })
                }
              />
            </div>
          </div>
        </CardHeader>
        <CardContent className="px-0">
          {plans.isLoading ? (
            <div className="grid place-items-center py-16 text-sm text-muted-foreground">
              <LoaderCircle className="mb-2 animate-spin" />
              正在载入任务…
            </div>
          ) : plans.error ? (
            <div className="px-6">
              <ErrorNotice error={plans.error} />
            </div>
          ) : rows.length === 0 ? (
            <div className="grid place-items-center py-16 text-center">
              <ListChecks className="mb-3 size-8 text-muted-foreground" />
              <strong>没有匹配的任务</strong>
              <span className="mt-1 text-sm text-muted-foreground">
                调整筛选，或创建第一个维护计划。
              </span>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>任务</TableHead>
                  <TableHead>数据身份</TableHead>
                  <TableHead>输出</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>下一次运行</TableHead>
                  <TableHead className="w-10">
                    <span className="sr-only">详情</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((plan) => (
                  <TableRow key={plan.task_id}>
                    <TableCell>
                      <Link
                        className="font-medium hover:underline"
                        to="/tasks/$taskId"
                        params={{ taskId: plan.task_id }}
                        search={{ q: search.q, health: search.health }}
                      >
                        {plan.name}
                      </Link>
                      <div className="mono mt-1 text-xs text-muted-foreground">
                        {plan.task_id.slice(0, 12)} · v{plan.definition_version}
                      </div>
                    </TableCell>
                    <TableCell>
                      {plan.provider ?? "—"} / {plan.symbol ?? "—"}
                    </TableCell>
                    <TableCell>{outputOf(plan)}</TableCell>
                    <TableCell>
                      <Badge variant={stateVariant(plan)}>
                        {plan.health ?? plan.desired_state}
                      </Badge>
                      {plan.block_reason && (
                        <div className="mt-1 max-w-48 text-xs text-muted-foreground">
                          {plan.block_reason}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>{formatTime(plan.next_run_at)}</TableCell>
                    <TableCell>
                      <Button asChild size="icon" variant="ghost">
                        <Link
                          aria-label={`查看 ${plan.name}`}
                          to="/tasks/$taskId"
                          params={{ taskId: plan.task_id }}
                          search={{ q: search.q, health: search.health }}
                        >
                          <ChevronRight />
                        </Link>
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      <CreateTaskSheet
        open={open}
        onOpenChange={setOpen}
        capabilities={capabilities.data}
        capabilitiesLoading={capabilities.isLoading}
        capabilitiesError={capabilities.error}
      />
    </>
  );
}

export function TaskDetailPage() {
  const { taskId } = useParams({ from: "/tasks/$taskId" });
  const sourceSearch = useSearch({ from: "/tasks/$taskId" });
  const [copied, setCopied] = useState(false);
  const plan = useQuery({
    queryKey: ["production-plan", taskId],
    queryFn: () => services.production.plan(taskId),
    refetchInterval: 2500,
  });
  const executions = useQuery({
    queryKey: ["production-executions", taskId],
    queryFn: () => services.production.executions(taskId, 10),
    enabled: !!plan.data,
    refetchInterval: 2500,
  });
  const capabilities = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => services.catalog.capabilities(),
  });
  const latestExecution =
    executions.data?.items[0] ??
    plan.data?.current_execution ??
    plan.data?.executions?.[0];
  const steps = useQuery({
    queryKey: ["production-steps", latestExecution?.execution_id],
    queryFn: () => services.production.steps(latestExecution!.execution_id),
    enabled: !!latestExecution,
    refetchInterval: 2500,
  });
  const taskPayload = (plan.data?.payload ?? {}) as {
    raw_timeframe?: string;
    bar_timeframes?: string[];
    price_basis?: string;
    window_policy?: { mode?: string; history_start?: string; start?: string; end?: string };
    schedule?: Record<string, unknown>;
  };
  const recipe5m = capabilities.data?.recipes.find(
    (recipe) =>
      recipe.input_dataset === "provider_bars" &&
      recipe.output_dataset === "market_bars" &&
      recipe.source_timeframe === taskPayload.raw_timeframe &&
      recipe.target_timeframe === "5m",
  );
  const rawRequest =
    plan.data?.provider && plan.data.symbol && taskPayload.raw_timeframe
      ? `/api/v1/bars?${new URLSearchParams({
          provider: plan.data.provider,
          symbol: plan.data.symbol,
          timeframe: taskPayload.raw_timeframe,
          page_size: "100",
        })}`
      : null;
  const derivedRequest =
    plan.data?.provider &&
    plan.data.symbol &&
    taskPayload.price_basis &&
    recipe5m
      ? `/api/v1/market-bars?${new URLSearchParams({
          provider: plan.data.provider,
          symbol: plan.data.symbol,
          timeframe: "5m",
          price_basis: taskPayload.price_basis,
          recipe_id: recipe5m.recipe_id,
          recipe_version: recipe5m.recipe_version,
          page_size: "100",
        })}`
      : null;
  const readback = useQuery({
    queryKey: ["production-readback", taskId, rawRequest, derivedRequest],
    queryFn: async () => {
      const provider = plan.data!.provider!;
      const symbol = plan.data!.symbol!;
      const raw = await services.query.bars({
        provider,
        symbol,
        timeframe: taskPayload.raw_timeframe!,
      });
      const derived =
        recipe5m && taskPayload.price_basis
          ? await services.query.marketBars({
              provider,
              symbol,
              timeframe: "5m",
              price_basis: taskPayload.price_basis,
              recipe_id: recipe5m.recipe_id,
              recipe_version: recipe5m.recipe_version,
            })
          : null;
      return { raw, derived };
    },
    enabled: !!rawRequest,
    refetchInterval: 2500,
  });
  const runMutation = useMutation({
    mutationFn: async () => {
      const current = plan.data;
      if (!current) throw new Error("任务尚未载入");
      if (current.desired_state === "paused") {
        await services.production.act(
          taskId,
          "resume",
          `ui-resume-${createIdempotencySuffix()}`,
          { expected_version: current.definition_version },
        );
      }
      return services.production.act(
        taskId,
        "run_now",
        `ui-run-${createIdempotencySuffix()}`,
      );
    },
    onSuccess: async () => {
      await Promise.all([plan.refetch(), executions.refetch()]);
    },
  });
  if (plan.isLoading)
    return (
      <div className="grid place-items-center py-20 text-sm text-muted-foreground">
        <LoaderCircle className="mb-2 animate-spin" />
        正在载入任务详情…
      </div>
    );
  if (plan.error || !plan.data)
    return (
      <>
        <Button asChild variant="ghost">
          <Link to="/tasks" search={sourceSearch}>
            <ArrowLeft />
            返回任务
          </Link>
        </Button>
        <div className="mt-6">
          <ErrorNotice error={plan.error ?? new Error("任务不存在")} />
        </div>
      </>
    );
  const item = plan.data;
  const payload = taskPayload;
  const runIsComplete =
    latestExecution?.state === "completed" &&
    latestExecution.outcome === "pass" &&
    item.progress?.backlog === false &&
    (item.progress?.deferred_derived?.length ?? 0) === 0;
  const runIsActive = ["pending", "queued", "running"].includes(
    latestExecution?.state ?? "",
  );
  return (
    <>
      <header className="mb-6">
        <Button asChild variant="ghost" className="mb-3 -ml-3">
          <Link to="/tasks" search={sourceSearch}>
            <ArrowLeft />
            返回任务列表
          </Link>
        </Button>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge variant={stateVariant(item)}>
                {item.health ?? item.desired_state}
              </Badge>
              <Badge variant="outline">{item.desired_state}</Badge>
            </div>
            <h1 className="m-0 text-2xl font-bold tracking-tight">
              {item.name}
            </h1>
            <button
              className="mono mt-2 flex items-center gap-2 text-left text-xs text-muted-foreground hover:text-foreground"
              onClick={() =>
                void navigator.clipboard
                  .writeText(item.task_id)
                  .then(() => setCopied(true))
              }
            >
              {item.task_id}
              <Copy className="size-3" />
              <span className="sr-only">复制任务 ID</span>
              {copied && <span className="font-sans">已复制</span>}
            </button>
          </div>
          <Button
            variant="outline"
            onClick={() =>
              void Promise.all([
                plan.refetch(),
                executions.refetch(),
                steps.refetch(),
                readback.refetch(),
              ])
            }
          >
            <RefreshCw />
            刷新
          </Button>
          <Button
            onClick={() => runMutation.mutate()}
            disabled={runMutation.isPending || item.desired_state === "archived"}
          >
            <CirclePlay data-icon="inline-start" />
            {runMutation.isPending
              ? "正在派发…"
              : item.desired_state === "paused"
                ? "启用并立即运行"
                : "立即运行"}
          </Button>
        </div>
      </header>
      {runMutation.error && <div className="mb-6"><ErrorNotice error={runMutation.error} /></div>}
      <Card className="mb-6" aria-label="运行结论">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex min-w-0 gap-3">
              {runIsComplete ? (
                <CircleCheckBig className="mt-0.5 shrink-0 text-primary" />
              ) : (
                <CircleAlert className="mt-0.5 shrink-0 text-muted-foreground" />
              )}
              <div className="min-w-0">
                <CardTitle>
                  {runIsComplete
                    ? "本次运行已完成，数据已就绪"
                    : runIsActive
                      ? "任务正在运行"
                      : latestExecution
                        ? "本次运行需要检查"
                        : "任务尚未运行"}
                </CardTitle>
                <CardDescription className="mt-1">
                  {runIsComplete
                    ? `最新执行已通过，处理边界为 ${formatTime(item.progress?.raw_frontier)}，当前无积压。`
                    : runIsActive
                      ? "执行状态会自动刷新；完成后这里会明确显示通过结果和处理边界。"
                      : latestExecution
                        ? `最新执行状态为${executionStateLabel(latestExecution.state)}，结果为${executionOutcomeLabel(latestExecution.outcome)}。`
                        : "点击“立即运行”后，这里会显示本轮是否完成以及数据处理边界。"}
                </CardDescription>
              </div>
            </div>
            <Badge variant={runIsComplete ? "default" : "outline"}>
              {runIsComplete
                ? "已完成 · 通过"
                : latestExecution?.outcome
                  ? executionOutcomeLabel(latestExecution.outcome)
                  : latestExecution
                    ? executionStateLabel(latestExecution.state)
                    : "未运行"}
            </Badge>
          </div>
        </CardHeader>
        {latestExecution && (
          <CardContent>
            <dl className="grid gap-4 text-sm sm:grid-cols-3">
              <div>
                <dt className="text-xs text-muted-foreground">最新执行</dt>
                <dd className="mt-1">
                  <CopyValueButton value={latestExecution.execution_id} label="运行 ID" />
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">完成时间</dt>
                <dd className="mt-1 font-medium">{formatTime(latestExecution.finished_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">积压</dt>
                <dd className="mt-1 font-medium">
                  {item.progress?.backlog === false
                    ? "无"
                    : item.progress?.backlog
                      ? "有"
                      : "尚未记录"}
                </dd>
              </div>
            </dl>
          </CardContent>
        )}
      </Card>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.3fr)_minmax(18rem,.7fr)]">
        <div className="grid gap-6">
          <Card>
            <CardHeader>
              <CardTitle>数据定义</CardTitle>
              <CardDescription>
                任务定义版本 v{item.definition_version}
                ；原始与派生数据保持各自身份。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-5 sm:grid-cols-2">
                <div>
                  <dt className="text-xs text-muted-foreground">
                    数据源 / 品种
                  </dt>
                  <dd className="mt-1 font-medium">
                    {item.provider} / {item.symbol}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">价格基准</dt>
                  <dd className="mt-1 font-medium">
                    {payload.price_basis ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">原始周期</dt>
                  <dd className="mt-1 font-medium">
                    {payload.raw_timeframe ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">派生周期</dt>
                  <dd className="mt-1 font-medium">
                    {payload.bar_timeframes?.join(", ") || "无"}
                  </dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-xs text-muted-foreground">数据窗口</dt>
                  <dd className="mt-1 font-medium">
                    {formatTime(payload.window_policy?.history_start ?? payload.window_policy?.start)}
                    {payload.window_policy?.end ? ` → ${formatTime(payload.window_policy.end)}` : " → 持续维护"}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>执行步骤</CardTitle>
              <CardDescription>
                raw 发布完成后，派生步骤使用固定 input snapshot 生成 5m。
              </CardDescription>
            </CardHeader>
            <CardContent className="px-0">
              {steps.isLoading ? (
                <div className="p-6 text-sm text-muted-foreground">正在载入…</div>
              ) : steps.error ? (
                <div className="px-6"><ErrorNotice error={steps.error} /></div>
              ) : steps.data?.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>阶段</TableHead>
                      <TableHead>窗口</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>run ID</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {steps.data.map((step: ProductionStep) => (
                      <TableRow key={step.step_id}>
                        <TableCell>{step.stage}</TableCell>
                        <TableCell className="text-xs">
                          {formatTime(step.window_start)} → {formatTime(step.window_end)}
                        </TableCell>
                        <TableCell><Badge variant="outline">{step.state}</Badge></TableCell>
                        <TableCell>
                          {step.run_id ? (
                            <CopyValueButton value={step.run_id} label="run ID" />
                          ) : (
                            "—"
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <div className="p-6 text-sm text-muted-foreground">运行派发后显示步骤。</div>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Database className="size-4" />
                原始 1m 数据
              </CardTitle>
              <CardDescription>
                UI 通过与 HTTP 相同的 provider-bars 查询读取最近样本。
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 px-0">
              <div className="px-6">
                {rawRequest && <CopyRequestButton request={`curl -sS '${window.location.origin}${rawRequest}'`} />}
              </div>
              {readback.isLoading ? (
                <div className="px-6 text-sm text-muted-foreground">正在载入原始数据…</div>
              ) : readback.error ? (
                <div className="px-6"><ErrorNotice error={readback.error} /></div>
              ) : (
                <BarsTable rows={readback.data?.raw.items ?? []} />
              )}
            </CardContent>
          </Card>
          {payload.bar_timeframes?.includes("5m") && (
            <Card>
              <CardHeader>
                <CardTitle>派生 5m 数据</CardTitle>
                <CardDescription>
                  recipe {recipe5m ? `${recipe5m.recipe_id}@${recipe5m.recipe_version}` : "载入中"}
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 px-0">
                <div className="px-6">
                  {derivedRequest && <CopyRequestButton request={`curl -sS '${window.location.origin}${derivedRequest}'`} />}
                </div>
                {readback.isLoading ? (
                  <div className="px-6 text-sm text-muted-foreground">正在载入派生数据…</div>
                ) : readback.error ? (
                  <div className="px-6"><ErrorNotice error={readback.error} /></div>
                ) : (
                  <BarsTable rows={readback.data?.derived?.items ?? []} />
                )}
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader>
              <CardTitle>最近运行</CardTitle>
              <CardDescription>
                排队、运行成功与数据完整性是不同状态。
              </CardDescription>
            </CardHeader>
            <CardContent className="px-0">
              {executions.isLoading ? (
                <div className="p-6 text-sm text-muted-foreground">
                  正在载入…
                </div>
              ) : executions.error ? (
                <div className="px-6">
                  <ErrorNotice error={executions.error} />
                </div>
              ) : executions.data?.items.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>运行 ID</TableHead>
                      <TableHead>触发</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>结果</TableHead>
                      <TableHead>创建时间</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {executions.data.items.map((run) => (
                      <TableRow key={run.execution_id}>
                        <TableCell className="mono text-xs">
                          {run.execution_id.slice(0, 12)}
                        </TableCell>
                        <TableCell>{run.trigger_source}</TableCell>
                        <TableCell>
                          <Badge variant="outline">{executionStateLabel(run.state)}</Badge>
                        </TableCell>
                        <TableCell>
                          <Badge variant={run.outcome === "pass" ? "default" : "outline"}>
                            {executionOutcomeLabel(run.outcome)}
                          </Badge>
                        </TableCell>
                        <TableCell>{formatTime(run.created_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <div className="p-6 text-sm text-muted-foreground">
                  尚无运行记录。
                </div>
              )}
            </CardContent>
          </Card>
        </div>
        <div className="grid content-start gap-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <CalendarClock className="size-4" />
                调度
              </CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-4">
                <div>
                  <dt className="text-xs text-muted-foreground">规则</dt>
                  <dd className="mt-1 font-medium">
                    {item.schedule?.kind ??
                      String(payload.schedule?.schedule ?? "manual")}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">下一次运行</dt>
                  <dd className="mt-1 font-medium">
                    {formatTime(item.next_run_at)}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">最近更新</dt>
                  <dd className="mt-1 font-medium">
                    {formatTime(item.updated_at)}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>数据模式</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              当前页面按预览顶部标识区分模拟与真实源。模拟模式保留
              Dukascopy/EURUSD/BID 身份，但 connector 版本明确标记为 isolated
              preview fixture，不访问 Dukascopy 网络。
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
