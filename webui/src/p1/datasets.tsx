import { useCallback, useEffect, useMemo, useState } from "react";
import { Database, Pause, Play, Plus, RefreshCw, Wrench } from "lucide-react";
import { createDataCenterClient, type ManagedCoverage, type ManagedDataset, type ManagedMaintenance } from "../lib/api";
import { Badge } from "../components/shadcn/badge";
import { Button } from "../components/shadcn/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/shadcn/card";
import { Input } from "../components/shadcn/input";
import { Label } from "../components/shadcn/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/shadcn/table";

const client = createDataCenterClient("");

const localValue = (value: Date) => {
  const shifted = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
};

export function DatasetsPage() {
  const [datasets, setDatasets] = useState<ManagedDataset[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [requests, setRequests] = useState<ManagedMaintenance[]>([]);
  const [coverage, setCoverage] = useState<ManagedCoverage | null>(null);
  const [name, setName] = useState("Dukascopy EURUSD");
  const [notes, setNotes] = useState("EURUSD 手工维护数据集");
  const [historyStart, setHistoryStart] = useState("2026-01-01T00:00");
  const [start, setStart] = useState(() => localValue(new Date(Date.now() - 2 * 3_600_000)));
  const [end, setEnd] = useState(() => localValue(new Date(Date.now() - 3_600_000)));
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const selected = useMemo(
    () => datasets.find((item) => item.dataset_id === selectedId) ?? datasets[0] ?? null,
    [datasets, selectedId],
  );

  const load = useCallback(async (preferred?: string) => {
    setError("");
    try {
      const result = await client.managedDatasets();
      setDatasets(result.data);
      const next = preferred || selectedId || result.data[0]?.dataset_id || "";
      if (next) setSelectedId(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取数据集");
    }
  }, [selectedId]);

  const loadDetail = useCallback(async () => {
    if (!selected) return;
    try {
      const [maintenance, covered] = await Promise.all([
        client.managedMaintenance(selected.dataset_id),
        client.managedCoverage(selected.dataset_id).catch(() => null),
      ]);
      setRequests(maintenance.data);
      setCoverage(covered?.data ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取维护状态");
    }
  }, [selected?.dataset_id]);

  useEffect(() => { void load(); }, []);
  useEffect(() => { void loadDetail(); }, [loadDetail]);

  const create = async () => {
    setBusy("create"); setError(""); setMessage("");
    const datasetId = `eurusd-${Date.now().toString(36)}`;
    try {
      await client.createManagedDataset({
        dataset_id: datasetId, name, notes,
        history_start: historyStart ? new Date(historyStart).toISOString() : null,
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
      await client.addManagedMember(selected.dataset_id, { symbol: "EURUSD" });
      setMessage("EURUSD 已加入，并继承 BID 1m 与 5m 派生目标。");
      await load(selected.dataset_id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "加入失败"); }
    finally { setBusy(""); }
  };

  const setStatus = async (status: "active" | "paused") => {
    if (!selected) return;
    setBusy("status"); setError("");
    try {
      await client.updateManagedDataset(selected.dataset_id, { status });
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
        symbol: "EURUSD", start: new Date(start).toISOString(), end: new Date(end).toISOString(),
      }, `ui-${selected.dataset_id}-${start}-${end}`);
      setMessage("维护请求已排队；执行将依次发布 1m 和固定输入的 5m。 ");
      await loadDetail();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "维护请求失败"); }
    finally { setBusy(""); }
  };

  return <div className="flex flex-col gap-6">
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

    <div className="grid gap-6 xl:grid-cols-[22rem_1fr]">
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader><CardTitle>新建数据集</CardTitle><CardDescription>来源、价格口径和基础粒度在 P2.1 固定。</CardDescription></CardHeader>
          <CardContent><form className="flex flex-col gap-4" onSubmit={(event) => { event.preventDefault(); void create(); }}>
            <div className="field"><Label htmlFor="dataset-name">名称</Label><Input id="dataset-name" value={name} onChange={(event) => setName(event.target.value)} /></div>
            <div className="field"><Label htmlFor="dataset-notes">备注</Label><Input id="dataset-notes" value={notes} onChange={(event) => setNotes(event.target.value)} /></div>
            <div className="field"><Label htmlFor="history-start">默认历史起点</Label><Input id="history-start" type="datetime-local" value={historyStart} onChange={(event) => setHistoryStart(event.target.value)} /></div>
            <div className="flex flex-wrap gap-2"><Badge variant="outline">Dukascopy</Badge><Badge variant="outline">FX</Badge><Badge variant="outline">BID</Badge><Badge variant="outline">1m → 5m</Badge></div>
            <Button disabled={busy === "create" || !name.trim()}><Plus data-icon="inline-start" />{busy === "create" ? "创建中…" : "创建"}</Button>
          </form></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>数据集列表</CardTitle><CardDescription>{datasets.length} 个隔离数据集</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-2">
            {datasets.length === 0 && <p className="m-0 text-sm text-muted-foreground">尚无数据集，请先创建。</p>}
            {datasets.map((item) => <Button key={item.dataset_id} variant={selected?.dataset_id === item.dataset_id ? "secondary" : "ghost"} className="justify-start" onClick={() => setSelectedId(item.dataset_id)}>
              <Database data-icon="inline-start" />{item.name}<Badge variant="outline" className="ml-auto">{item.status}</Badge>
            </Button>)}
          </CardContent>
        </Card>
      </div>

      {selected && <div className="flex flex-col gap-6">
        <Card>
          <CardHeader><CardTitle>{selected.name}</CardTitle><CardDescription>{selected.dataset_id} · 配置修订 v{selected.version}</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-2"><Badge>{selected.status}</Badge><Badge variant="outline">{selected.provider}</Badge><Badge variant="outline">{selected.price_type.toUpperCase()}</Badge><Badge variant="outline">{selected.base_timeframe} → {selected.derived_targets.join(", ")}</Badge></div>
            <p className="m-0 text-sm text-muted-foreground">{selected.notes || "无备注"}</p>
            {selected.members.EURUSD
              ? <p className="m-0 text-sm">EURUSD · 有效起点 {selected.members.EURUSD.effective_history_start || "未设置"} · 派生 {selected.members.EURUSD.effective_derived_targets.join(", ")}</p>
              : <Button variant="outline" onClick={() => void addMember()} disabled={busy === "member"}><Plus data-icon="inline-start" />加入 EURUSD</Button>}
          </CardContent>
          <CardFooter className="gap-2">
            {selected.status === "paused"
              ? <Button variant="outline" onClick={() => void setStatus("active")} disabled={busy === "status"}><Play data-icon="inline-start" />恢复</Button>
              : <Button variant="outline" onClick={() => void setStatus("paused")} disabled={busy === "status"}><Pause data-icon="inline-start" />暂停</Button>}
          </CardFooter>
        </Card>

        <Card>
          <CardHeader><CardTitle>固定区间补数</CardTitle><CardDescription>暂停状态也允许显式手工维护；同一区间重复提交保持幂等。</CardDescription></CardHeader>
          <CardContent><form className="grid gap-4 md:grid-cols-2" onSubmit={(event) => { event.preventDefault(); void maintain(); }}>
            <div className="field"><Label htmlFor="maintenance-start">开始</Label><Input id="maintenance-start" type="datetime-local" value={start} onChange={(event) => setStart(event.target.value)} /></div>
            <div className="field"><Label htmlFor="maintenance-end">结束</Label><Input id="maintenance-end" type="datetime-local" value={end} onChange={(event) => setEnd(event.target.value)} /></div>
            <Button className="md:col-span-2" disabled={!selected.members.EURUSD || busy === "maintain" || start >= end}><Wrench data-icon="inline-start" />{busy === "maintain" ? "排队中…" : "补齐 1m 并派生 5m"}</Button>
          </form></CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Coverage</CardTitle><CardDescription>只统计当前数据集独立目录，不跨数据集兜底。</CardDescription></CardHeader>
          <CardContent className="flex flex-wrap gap-3">
            <Badge variant="outline">1m · {coverage?.raw.row_count ?? 0} 行</Badge>
            {(coverage?.derived ?? []).map((item) => <Badge key={item.timeframe} variant="outline">{item.timeframe} · {item.row_count} 行</Badge>)}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>维护请求</CardTitle><CardDescription>请求、production execution、raw/derive steps 与 run 状态。</CardDescription></CardHeader>
          <CardContent><Table><TableHeader><TableRow><TableHead>区间</TableHead><TableHead>状态</TableHead><TableHead>执行</TableHead><TableHead>Runs</TableHead></TableRow></TableHeader>
            <TableBody>{requests.map((request) => <TableRow key={request.request_id}><TableCell>{request.start.slice(0, 16)} → {request.end.slice(0, 16)}</TableCell><TableCell><Badge variant="outline">{request.status}</Badge></TableCell><TableCell className="font-mono text-xs">{request.execution?.execution_id?.slice(0, 8) ?? "—"}</TableCell><TableCell>{request.execution?.run_ids?.length ?? 0}</TableCell></TableRow>)}</TableBody>
          </Table></CardContent>
        </Card>
      </div>}
    </div>
  </div>;
}
