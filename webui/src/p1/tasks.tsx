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
  Copy,
  ListChecks,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
} from "lucide-react";
import type {
  Capabilities,
  ProductionPlan,
  ProductionPreview,
} from "../lib/api";
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
const stamp = () => `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const datetimeLocal = () => {
  const date = new Date(Date.now() - 7 * 86400_000);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
};
type Draft = {
  name: string;
  provider: string;
  symbol: string;
  rawTimeframe: string;
  priceBasis: string;
  historyStart: string;
  schedule: string;
  intervalMinutes: number;
  derived5m: boolean;
};
const emptyDraft = (): Draft => ({
  name: "",
  provider: "",
  symbol: "",
  rawTimeframe: "",
  priceBasis: "",
  historyStart: datetimeLocal(),
  schedule: "manual",
  intervalMinutes: 15,
  derived5m: false,
});
const definitionOf = (draft: Draft) => ({
  provider: draft.provider,
  symbol: draft.symbol,
  raw_timeframe: draft.rawTimeframe,
  price_basis: draft.priceBasis,
  bar_timeframes: draft.derived5m ? ["5m"] : [],
  window_policy: {
    mode: "continuous",
    history_start: draft.historyStart
      ? new Date(draft.historyStart).toISOString()
      : "",
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
  const [createKey, setCreateKey] = useState(() => `ui-create-${stamp()}`);
  const providers = capabilities?.providers ?? [];
  const selectedProvider =
    providers.find((item) => item.provider === draft.provider) ?? providers[0];
  const canDerive5m =
    capabilities?.production?.outputs?.some(
      (output) =>
        output.dataset_id === "market_bars" && output.timeframes.includes("5m"),
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
      setCreateKey(`ui-create-${stamp()}`);
      setPreview(null);
      void navigate({
        to: "/tasks/$taskId",
        params: { taskId: plan.task_id },
        search: { q: "", health: "" },
      });
    },
    onError: setError,
  });
  const provider = draft.provider || selectedProvider?.provider || "";
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
            选项来自 capabilities API。P1 先保存为暂停，运行与真实数据闭环在 P2
            验收。
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
          <div className="field">
            <Label htmlFor="history-start">历史起点</Label>
            <Input
              id="history-start"
              type="datetime-local"
              value={draft.historyStart}
              onChange={(e) => update({ historyStart: e.target.value })}
            />
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
              {scheduler.data?.dispatch_enabled ? "有效" : "未启用"}
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
  });
  const executions = useQuery({
    queryKey: ["production-executions", taskId],
    queryFn: () => services.production.executions(taskId, 10),
    enabled: !!plan.data,
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
  const payload = item.payload as {
    raw_timeframe?: string;
    bar_timeframes?: string[];
    price_basis?: string;
    window_policy?: { history_start?: string };
    schedule?: Record<string, unknown>;
  };
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
              void Promise.all([plan.refetch(), executions.refetch()])
            }
          >
            <RefreshCw />
            刷新
          </Button>
        </div>
      </header>
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
                  <dt className="text-xs text-muted-foreground">历史起点</dt>
                  <dd className="mt-1 font-medium">
                    {formatTime(payload.window_policy?.history_start)}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>
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
                          <Badge variant="outline">{run.state}</Badge>
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
              <CardTitle>阶段限制</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              P1
              用于确认列表、创建与详情体验。任务创建为暂停状态；手动运行、真实
              Dukascopy 发布和完整数据读回在 P2 实施。
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
