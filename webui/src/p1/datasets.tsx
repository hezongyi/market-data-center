import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { Database, Pause, Play, Plus, RefreshCw, Wrench } from "lucide-react";
import { createDataCenterClient, type ManagedCoverage, type ManagedDataset, type ManagedMaintenance } from "../lib/api";
import { Badge } from "../components/shadcn/badge";
import { Button } from "../components/shadcn/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/shadcn/card";
import { Input } from "../components/shadcn/input";
import { Label } from "../components/shadcn/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/shadcn/table";

const client = createDataCenterClient("");
const draftKey = "mdc.p21.dataset-draft.v1";

const localValue = (value: Date) => {
  const shifted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
};

type DatasetDraft = {
  name: string;
  notes: string;
  historyStart: string;
  start: string;
  end: string;
};

const defaultDraft = (): DatasetDraft => ({
  name: "Dukascopy EURUSD",
  notes: "EURUSD 手工维护数据集",
  historyStart: "2026-01-01T00:00",
  start: localValue(new Date(Date.now() - 2 * 3_600_000)),
  end: localValue(new Date(Date.now() - 3_600_000)),
});

const retainedDraft = (): DatasetDraft => {
  try {
    const value = JSON.parse(sessionStorage.getItem(draftKey) ?? "null") as Partial<DatasetDraft> | null;
    return { ...defaultDraft(), ...(value ?? {}) };
  } catch {
    return defaultDraft();
  }
};

export function DatasetsPage() {
  const search = useSearch({ from: "/datasets" });
  const navigate = useNavigate({ from: "/datasets" });
  const [datasets, setDatasets] = useState<ManagedDataset[]>([]);
  const [requests, setRequests] = useState<ManagedMaintenance[]>([]);
  const [coverage, setCoverage] = useState<ManagedCoverage | null>(null);
  const [draft, setDraft] = useState(retainedDraft);
  const [datasetsLoading, setDatasetsLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const selected = useMemo(
    () => datasets.find((item) => item.dataset_id === search.dataset) ?? null,
    [datasets, search.dataset],
  );
  const nameInvalid = !draft.name.trim();
  const startInvalid = !draft.start;
  const endInvalid = !draft.end || Boolean(draft.start && draft.end && draft.start >= draft.end);

  const load = useCallback(async (preferred?: string) => {
    setError(""); setDatasetsLoading(true);
    try {
      const result = await client.managedDatasets();
      setDatasets(result.data);
      const requested = preferred || search.dataset;
      const next = result.data.some((item) => item.dataset_id === requested)
        ? requested
        : result.data[0]?.dataset_id || "";
      if (next !== search.dataset) {
        void navigate({ search: { dataset: next }, replace: true });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取数据集");
    } finally { setDatasetsLoading(false); }
  }, [navigate, search.dataset]);

  const loadDetail = useCallback(async () => {
    if (!selected) { setRequests([]); setCoverage(null); return; }
    setDetailLoading(true); setError(""); setRequests([]); setCoverage(null);
    try {
      const maintenance = await client.managedMaintenance(selected.dataset_id);
      setRequests(maintenance.data);
      if (selected.status !== "archived") {
        const covered = await client.managedCoverage(selected.dataset_id);
        setCoverage(covered.data);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取维护状态");
    } finally { setDetailLoading(false); }
  }, [selected?.dataset_id]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { void loadDetail(); }, [loadDetail]);
  useEffect(() => { sessionStorage.setItem(draftKey, JSON.stringify(draft)); }, [draft]);

  const create = async () => {
    setBusy("create"); setError(""); setMessage("");
    const datasetId = `eurusd-${Date.now().toString(36)}`;
    try {
      await client.createManagedDataset({
        dataset_id: datasetId, name: draft.name, notes: draft.notes,
        history_start: draft.historyStart ? new Date(draft.historyStart).toISOString() : null,
      });
      setMessage(`已创建 ${datasetId}。下一步加入 EURUSD。`);
      await load(datasetId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建失败");
    } finally { setBusy(""); }
  };

  const addMember = async () => {
    if (!selected) return;
    setBusy("member"); setError("");
    try {
      await client.addManagedMember(selected.dataset_id, {
        symbol: "EURUSD", expected_version: selected.version,
      });
      setMessage("EURUSD 已加入，并继承 BID 1m 与 5m 派生目标。");
      await load(selected.dataset_id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "加入失败"); }
    finally { setBusy(""); }
  };

  const setStatus = async (status: "active" | "paused") => {
    if (!selected) return;
    setBusy("status"); setError("");
    try {
      await client.updateManagedDataset(selected.dataset_id, {
        status, expected_version: selected.version,
      });
      setMessage(status === "paused" ? "已暂停自动维护；查询和手工补数仍可用。" : "数据集已恢复。 ");
      await load(selected.dataset_id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "状态更新失败"); }
    finally { setBusy(""); }
  };

  const maintain = async () => {
    if (!selected) return;
    setBusy("maintain"); setError("");
    try {
      await client.submitManagedMaintenance(selected.dataset_id, {
        symbol: "EURUSD", start: new Date(draft.start).toISOString(), end: new Date(draft.end).toISOString(),
      }, `ui-${selected.dataset_id}-${draft.start}-${draft.end}`);
      setMessage("维护请求已排队；执行将依次发布 1m 和固定输入的 5m。 ");
      await loadDetail();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "维护请求失败"); }
    finally { setBusy(""); }
  };

  return <div className="min-w-0 flex flex-col gap-6">
    <header className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
      <div>
        <h1 className="m-0 text-2xl font-bold">行情数据集</h1>
        <p className="m-0 text-sm text-muted-foreground">创建 Dukascopy FX · BID 1m 数据集，维护 EURUSD，并派生 5m。</p>
      </div>
      <Button variant="outline" onClick={() => void loadDetail()} disabled={!selected || Boolean(busy)}>
        <RefreshCw data-icon="inline-start" />刷新状态
      </Button>
    </header>

    {error && <p role="alert" className="m-0 text-sm text-destructive">{error}</p>}
    {message && <p role="status" className="m-0 text-sm text-muted-foreground">{message}</p>}

    <div className="grid min-w-0 gap-6 xl:grid-cols-[22rem_minmax(0,1fr)]">
      <div className="min-w-0 flex flex-col gap-6">
        <Card className="min-w-0">
          <CardHeader><CardTitle>新建数据集</CardTitle><CardDescription>来源、价格口径和基础粒度在 P2.1 固定。</CardDescription></CardHeader>
          <CardContent><form className="flex flex-col gap-4" onSubmit={(event) => { event.preventDefault(); void create(); }}>
            <div className="field"><Label htmlFor="dataset-name">名称</Label><Input id="dataset-name" required aria-invalid={nameInvalid} value={draft.name} onChange={(event) => setDraft((value) => ({ ...value, name: event.target.value }))} />{nameInvalid && <p className="m-0 text-xs text-destructive">名称不能为空。</p>}</div>
            <div className="field"><Label htmlFor="dataset-notes">备注</Label><Input id="dataset-notes" value={draft.notes} onChange={(event) => setDraft((value) => ({ ...value, notes: event.target.value }))} /></div>
            <div className="field"><Label htmlFor="history-start">默认历史起点</Label><Input id="history-start" type="datetime-local" value={draft.historyStart} onChange={(event) => setDraft((value) => ({ ...value, historyStart: event.target.value }))} /></div>
            <div className="flex flex-wrap gap-2"><Badge variant="outline">Dukascopy</Badge><Badge variant="outline">FX</Badge><Badge variant="outline">BID</Badge><Badge variant="outline">1m → 5m</Badge></div>
            <Button disabled={busy === "create" || nameInvalid}><Plus data-icon="inline-start" />{busy === "create" ? "创建中…" : "创建"}</Button>
          </form></CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader><CardTitle>数据集列表</CardTitle><CardDescription>{datasets.length} 个隔离数据集</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-2">
            {datasetsLoading && <p role="status" className="m-0 text-sm text-muted-foreground">正在读取数据集…</p>}
            {!datasetsLoading && datasets.length === 0 && <p className="m-0 text-sm text-muted-foreground">尚无数据集，请先创建。</p>}
            {datasets.map((item) => <Button key={item.dataset_id} variant={selected?.dataset_id === item.dataset_id ? "secondary" : "ghost"} className="w-full min-w-0 justify-start" onClick={() => void navigate({ search: { dataset: item.dataset_id } })}>
              <Database data-icon="inline-start" className="shrink-0" /><span className="min-w-0 truncate">{item.name}</span><Badge variant="outline" className="ml-auto shrink-0">{item.status}</Badge>
            </Button>)}
          </CardContent>
        </Card>
      </div>

      {selected && <div className="min-w-0 flex flex-col gap-6">
        <Card className="min-w-0">
          <CardHeader><CardTitle className="break-words">{selected.name}</CardTitle><CardDescription className="break-all">{selected.dataset_id} · 配置修订 v{selected.version}</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-2"><Badge>{selected.status}</Badge><Badge variant="outline">{selected.provider}</Badge><Badge variant="outline">{selected.price_type.toUpperCase()}</Badge><Badge variant="outline">{selected.base_timeframe} → {selected.derived_targets.join(", ")}</Badge></div>
            <p className="m-0 break-words text-sm text-muted-foreground">{selected.notes || "无备注"}</p>
            {selected.members.EURUSD
              ? <p className="m-0 text-sm">EURUSD · 有效起点 {selected.members.EURUSD.effective_history_start || "未设置"} · 派生 {selected.members.EURUSD.effective_derived_targets.join(", ")}</p>
              : selected.status === "archived"
                ? <p className="m-0 text-sm text-muted-foreground">归档时未加入 EURUSD；配置不可再修改。</p>
              : <Button variant="outline" onClick={() => void addMember()} disabled={busy === "member"}><Plus data-icon="inline-start" />加入 EURUSD</Button>}
          </CardContent>
          <CardFooter className="gap-2">
            {selected.status === "archived"
              ? <p className="m-0 text-sm text-muted-foreground">已归档，只保留配置与执行审计。</p>
              : selected.status === "paused"
              ? <Button variant="outline" onClick={() => void setStatus("active")} disabled={busy === "status"}><Play data-icon="inline-start" />恢复</Button>
              : <Button variant="outline" onClick={() => void setStatus("paused")} disabled={busy === "status"}><Pause data-icon="inline-start" />暂停</Button>}
          </CardFooter>
        </Card>

        <Card className="min-w-0">
          <CardHeader><CardTitle>固定区间补数</CardTitle><CardDescription>暂停状态也允许显式手工维护；同一区间重复提交保持幂等。</CardDescription></CardHeader>
          <CardContent><form className="grid gap-4 md:grid-cols-2" onSubmit={(event) => { event.preventDefault(); void maintain(); }}>
            <div className="field"><Label htmlFor="maintenance-start">开始</Label><Input id="maintenance-start" type="datetime-local" required disabled={selected.status === "archived"} aria-invalid={startInvalid} max={draft.end || undefined} value={draft.start} onChange={(event) => setDraft((value) => ({ ...value, start: event.target.value }))} />{startInvalid && <p className="m-0 text-xs text-destructive">请选择开始时间。</p>}</div>
            <div className="field"><Label htmlFor="maintenance-end">结束</Label><Input id="maintenance-end" type="datetime-local" required disabled={selected.status === "archived"} aria-invalid={endInvalid} min={draft.start || undefined} value={draft.end} onChange={(event) => setDraft((value) => ({ ...value, end: event.target.value }))} />{endInvalid && <p className="m-0 text-xs text-destructive">结束时间必须晚于开始时间。</p>}</div>
            <Button className="md:col-span-2" disabled={selected.status === "archived" || !selected.members.EURUSD || busy === "maintain" || startInvalid || endInvalid}><Wrench data-icon="inline-start" />{selected.status === "archived" ? "已归档，不能补数" : busy === "maintain" ? "排队中…" : "补齐 1m 并派生 5m"}</Button>
          </form></CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader><CardTitle>Coverage</CardTitle><CardDescription>只统计当前数据集独立目录，不跨数据集兜底。</CardDescription></CardHeader>
          <CardContent className="flex flex-wrap gap-3">
            {detailLoading
              ? <p role="status" className="m-0 text-sm text-muted-foreground">正在读取 coverage…</p>
              : selected.status === "archived"
                ? <p className="m-0 text-sm text-muted-foreground">归档数据集禁止行情查询；文件与审计仍保留。</p>
              : <><Badge variant="outline">1m · {coverage?.raw.row_count ?? 0} 行</Badge>
                {(coverage?.derived ?? []).map((item) => <Badge key={item.timeframe} variant="outline">{item.timeframe} · {item.row_count} 行</Badge>)}</>}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader><CardTitle>维护请求</CardTitle><CardDescription>请求、production execution、raw/derive steps 与 run 状态。</CardDescription></CardHeader>
          <CardContent className="min-w-0">
            {detailLoading
              ? <p role="status" className="m-0 text-sm text-muted-foreground">正在读取维护请求…</p>
              : requests.length === 0
                ? <p className="m-0 text-sm text-muted-foreground">尚无维护请求。</p>
                : <div className="max-w-full overflow-x-auto"><Table><TableHeader><TableRow><TableHead>区间</TableHead><TableHead>状态</TableHead><TableHead>执行</TableHead><TableHead>Runs</TableHead></TableRow></TableHeader>
                  <TableBody>{requests.map((request) => <TableRow key={request.request_id}><TableCell>{request.start.slice(0, 16)} → {request.end.slice(0, 16)}</TableCell><TableCell><Badge variant="outline">{request.status}</Badge></TableCell><TableCell className="font-mono text-xs">{request.execution?.execution_id?.slice(0, 8) ?? "—"}</TableCell><TableCell>{request.execution?.run_ids?.length ?? 0}</TableCell></TableRow>)}</TableBody>
                </Table></div>}
          </CardContent>
        </Card>
      </div>}
    </div>
  </div>;
}
