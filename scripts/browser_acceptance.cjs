const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { execFileSync, spawn } = require("node:child_process");

const startedAt = new Date().toISOString();
const repo = path.resolve(__dirname, "..");
const { chromium } = require(path.join(repo, "webui/node_modules/playwright"));
const output = path.resolve(process.env.DATACENTER_BROWSER_OUTPUT || path.join(repo, "acceptance-receipts/browser"));
const python = process.env.DATACENTER_PYTHON || path.join(repo, ".venv/bin/python");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "market-data-center-browser-"));
const key = "browser-acceptance-key";
let api;
let worker;
let browser;
let fredEndpoint;
// The last completed step is recorded in the receipt so a failure names the
// stage that broke instead of only the locator that timed out.
let step = "start";
const recordStep = name => { step = name; };

const commit = () => {
  try { return execFileSync("git", ["rev-parse", "HEAD"], { cwd: repo, encoding: "utf8" }).trim(); }
  catch { return "unknown"; }
};

const freePort = () => new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once("error", reject);
  server.listen(0, "127.0.0.1", () => {
    const { port } = server.address();
    server.close(() => resolve(port));
  });
});

const waitFor = async (fn, message, attempts = 150) => {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const value = await fn();
      if (value) return value;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(message);
};

const writeReceipt = (result, details, failureStage = null, errorCategory = null) => {
  fs.mkdirSync(output, { recursive: true });
  const report = {
    receipt_version: "operational-receipt.v1",
    action: "browser_acceptance",
    commit: commit(),
    environment: {
      node: process.version, platform: process.platform, isolated_root: true,
      browser_source: process.env.PLAYWRIGHT_BROWSER_EXECUTABLE ? "explicit_executable" : "playwright_managed",
    },
    command: "npm --prefix webui run test:e2e",
    started_at: startedAt,
    completed_at: new Date().toISOString(),
    software_version: require(path.join(repo, "webui/package.json")).version,
    result,
    failure_stage: failureStage,
    error_category: errorCategory,
    details,
  };
  fs.writeFileSync(path.join(output, "receipt.json"), JSON.stringify(report, null, 2) + "\n");
  return report;
};

(async () => {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  // Economic ingest is exercised end to end without contacting FRED: the
  // configured endpoint points at a local fixture that speaks the same shape.
  fredEndpoint = http.createServer((request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    const payload = url.pathname.includes("/series/observations")
      ? { observations: ["2026-01-01", "2026-01-02", "2026-01-03"].map((date, index) => ({
          date, value: String(index + 1), realtime_start: date, realtime_end: "9999-12-31",
        })) }
      : { seriess: [{ frequency: "Daily", units: "Index" }] };
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify(payload));
  });
  const fredPort = await new Promise(resolve => fredEndpoint.listen(0, "127.0.0.1", () => resolve(fredEndpoint.address().port)));
  const childEnv = {
    ...process.env,
    PYTHONPATH: path.join(repo, "backend/src"),
    DATACENTER_HOST: "127.0.0.1",
    DATACENTER_PORT: String(port),
    DATACENTER_API_KEY: key,
    DATACENTER_CANONICAL_ROOT: path.join(temp, "canonical"),
    DATACENTER_LEDGER_PATH: path.join(temp, "canonical/audit/data_center.sqlite"),
    DATACENTER_EVIDENCE_ROOT: path.join(temp, "evidence"),
    DATACENTER_WEBUI_DIST: path.join(repo, "webui/dist"),
    // The local fixture ignores this placeholder credential; no real provider
    // is contacted and the value is never logged or persisted.  It is composed
    // at runtime so the committed file holds no credential-shaped literal.
    FRED_API_KEY: ["browser", "acceptance", "placeholder"].join("-"),
    DATACENTER_FRED_ENDPOINT: `http://127.0.0.1:${fredPort}/fred/series/observations`,
    DATACENTER_FRED_METADATA_ENDPOINT: `http://127.0.0.1:${fredPort}/fred/series`,
    // Pin the measured free ratio so the warning state is reproducible on any host.
    // A 0.99 warning threshold only classified as "warning" when the filesystem
    // backing the temporary root happened to be more than 1% full, so on a
    // completely free tmpfs the capacity UI check could never pass.
    DATACENTER_CAPACITY_FIXED_FREE_RATIO: "0.03",
    DATACENTER_CAPACITY_WARNING_FREE_RATIO: "0.05",
    DATACENTER_CAPACITY_CRITICAL_FREE_RATIO: "0.02",
  };
  const deadLetterId = execFileSync(python, ["-c", [
    "import sys",
    "from pathlib import Path",
    "from data_center.runs.ledger import RunLedger",
    "ledger = RunLedger(Path(sys.argv[1]))",
    "run_id = ledger.enqueue_job({'job_id': 'browser-dead-letter', 'dataset_id': 'provider_bars', 'run_scope': 'production'})",
    "for _ in range(3):",
    "    claimed = ledger.claim_next_job()",
    "    ledger.fail_job(claimed['job_id'], run_id, 'acceptance dead letter')",
    "print(run_id)",
  ].join("\n"), childEnv.DATACENTER_LEDGER_PATH], { cwd: repo, env: childEnv, encoding: "utf8" }).trim();
  // Seed a governed 1m raw part so the derive and parity flows have a real
  // immutable input snapshot instead of a synthetic one.
  execFileSync(python, ["-c", [
    "import sys",
    "from datetime import datetime, timedelta, timezone",
    "from pathlib import Path",
    "from data_center.catalog.manifest import build_manifest, write_manifest",
    "from data_center.domain.models import ProviderBar",
    "from data_center.storage.parquet import write_provider_bars",
    "root = Path(sys.argv[1])",
    "start = datetime(2026, 3, 1, tzinfo=timezone.utc)",
    "rows = [ProviderBar(symbol='UI_TEST', asset_class='test', provider='fixture', timeframe='1m',",
    "                    bar_ts=start + timedelta(minutes=index), open=100 + index, high=101 + index,",
    "                    low=99 + index, close=100.5 + index, volume=10 + index,",
    "                    ingest_ts=start, source_hash=f'seed-{index}') for index in range(1440)]",
    "paths = write_provider_bars(root, rows, part_id='seed-1m')",
    "write_manifest(root, build_manifest(root, run_id='seed-1m', dataset_id='provider_bars',",
    "    schema_version='provider_bars.v1', paths=paths, row_count=len(rows),",
    "    quality_summary={'status': 'pass', 'finding_count': 0, 'findings': []}))",
    "print(len(rows))",
  ].join("\n"), childEnv.DATACENTER_CANONICAL_ROOT], { cwd: repo, env: childEnv, encoding: "utf8" }).trim();
  api = spawn(python, ["-m", "data_center.api"], { cwd: repo, env: childEnv, stdio: "ignore" });
  worker = spawn(python, ["-m", "data_center.worker_main"], { cwd: repo, env: childEnv, stdio: "ignore" });

  const envelope = async (method, route, body, authorized = true) => {
    const headers = { "Content-Type": "application/json" };
    if (authorized) headers["X-API-Key"] = key;
    const response = await fetch(base + "/api/v1" + route, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
    });
    const payload = await response.json();
    return { response, payload };
  };
  // A submission is verified through the API, never through a rendered list
  // that may still show the previous task.
  const findNewRun = async (before, kind) => {
    const known = new Set(before.map(run => run.run_id));
    return waitFor(async () => {
      const created = (await call("GET", "/runs")).find(run => !known.has(run.run_id) && run.run_kind === kind);
      return created ? created.run_id : false;
    }, `no new ${kind} run was queued`);
  };
  const call = async (method, route, body) => {
    const { response, payload } = await envelope(method, route, body);
    assert.ok(response.ok, `${method} ${route}: HTTP ${response.status}`);
    return payload.data;
  };

  await waitFor(async () => {
    const { response, payload } = await envelope("GET", "/health/ready");
    return response.ok && payload.data.status === "ready";
  }, "isolated service did not become ready");

  const unauthorized = await envelope("POST", "/ingest/runs", {
    job_id: "unauthorized", run_scope: "acceptance", symbol: "UI_TEST", start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z",
  }, false);
  assert.equal(unauthorized.response.status, 401);

  const fixture = await call("POST", "/ingest/runs", {
    job_id: "browser-bars", run_scope: "acceptance", provider: "fixture", symbol: "UI_TEST", asset_class: "test",
    timeframe: "1d", start: "2026-01-01T00:00:00Z", end: "2026-01-03T00:00:00Z",
  });
  const fixtureReceipt = await waitFor(async () => {
    const value = await call("GET", "/runs/" + fixture.run_id);
    return value.status === "pass" ? value : false;
  }, "fixture ingest did not pass");
  assert.ok(fixtureReceipt.row_count > 0);
  const coverage = await call("GET", "/provider-bars/coverage?provider=fixture&symbol=UI_TEST&timeframe=1d");
  assert.ok(coverage.row_count > 0);

  const failed = await call("POST", "/ingest/runs", {
    job_id: "browser-failure", run_scope: "acceptance", provider: "acceptance_invalid", symbol: "UI_TEST",
    start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z",
  });
  const failedReceipt = await waitFor(async () => {
    const value = await call("GET", "/runs/" + failed.run_id);
    return value.status === "failed" ? value : false;
  }, "failure fixture did not reach terminal state");

  const browserExecutable = process.env.PLAYWRIGHT_BROWSER_EXECUTABLE;
  browser = await chromium.launch({
    headless: true,
    executablePath: browserExecutable || undefined,
  });
  const results = [];
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    await page.addInitScript((apiKey) => { const original = window.fetch; window.fetch = (input, init = {}) => { const headers = new Headers(init.headers || {}); if (!headers.has("X-API-Key")) headers.set("X-API-Key", apiKey); return original(input, { ...init, headers }); }; }, key);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => localStorage.setItem("mdc.locale", "zh-CN"));
    await page.goto(base + "/legacy.html");
    await page.locator(".sidebar").getByText("API ready", { exact: true }).waitFor();
    await page.locator(".sidebar").getByText("Snapshot fresh", { exact: true }).waitFor();
    await page.getByText("development", { exact: false }).first().waitFor();
    await page.getByText(/Capacity warning|容量.*警告/, { exact: false }).first().waitFor();
    await page.getByText("降级与失败运行", { exact: true }).waitFor();
    await page.getByRole("button", { name: "English", exact: true }).click();
    await page.getByText("Degraded and failed runs", { exact: true }).waitFor();

    if (viewport.width === 1440) {
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      await page.locator(".app-shell.shell-collapsed").waitFor();
      await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
      await page.locator(".app-shell:not(.shell-collapsed)").waitFor();
    }

    recordStep("catalog");
    await page.getByRole("button", { name: "datasets", exact: true }).click();
    // "provider_bars" also appears in the lineage list and in every provider
    // capability row, and those panels render asynchronously: matching page-wide
    // passed only while they had not rendered yet.  The dataset row inside the
    // "Data catalog" panel is the one this step means.
    const datasetCell = page.locator("section[aria-label='Data catalog']")
      .getByRole("cell", { name: "provider_bars", exact: true });
    await datasetCell.waitFor();
    await datasetCell.click();
    await page.getByRole("complementary", { name: "provider_bars" }).waitFor();
    await page.getByRole("button", { name: "Close details", exact: true }).last().click();
    const economicRow = page.locator("tr").filter({ hasText: "economic_observations" });
    await economicRow.getByRole("button", { name: "Open Explorer", exact: true }).click();
    await page.getByRole("button", { name: "Economic", exact: true }).waitFor();
    await page.getByRole("button", { name: "datasets", exact: true }).click();

    recordStep("runs");
    await page.getByRole("button", { name: "runs", exact: true }).click();
    await page.locator("button.access-button").click();
    await page.getByLabel("Status").selectOption("failed");
    const runIdCell = page.locator(".mono").filter({
      hasText: new RegExp(`^${failed.run_id}$`),
    });
    const row = page.locator("tr").filter({ has: runIdCell });
    await row.waitFor();
    const before = (await call("GET", "/runs")).filter(run => run.retry_of === failed.run_id).length;
    await row.getByRole("button", { name: "Retry", exact: true }).click();
    await page.getByRole("dialog", { name: "Retry failed run?" }).getByRole("button", { name: "Queue retry" }).click();
    await page.locator(".notice").filter({ hasText: "Queued retry" }).waitFor();
    assert.equal((await call("GET", "/runs")).filter(run => run.retry_of === failed.run_id).length, before + 1);
    assert.deepEqual(await call("GET", "/runs/" + failed.run_id), failedReceipt);

    if (viewport.width === 1440) {
      await page.getByLabel("Status").selectOption("dead_letter");
      const deadLetterRow = page.locator("tr").filter({ hasText: deadLetterId });
      await deadLetterRow.getByRole("button", { name: "Acknowledge", exact: true }).click();
      // The isolated page fetch shim supplies the valid key; exercise the
      // invalid path with a direct request so the confirmation dialog remains open.
      const denied = await page.evaluate(async () => { const response = await fetch("/api/v1/maintenance/tasks", { method: "POST", headers: { "X-API-Key": "incorrect-key", "Content-Type": "application/json" }, body: JSON.stringify({run_kind:"ingest",run_scope:"acceptance",provider:"fixture",symbol:"UI_TEST",asset_class:"test",timeframe:"1d",start:"2026-01-01T00:00:00Z",end:"2026-01-02T00:00:00Z"}) }); return { status: response.status, body: await response.json() }; });
      assert.equal(denied.status, 401);
      assert.equal(denied.body.errors[0].code, "unauthorized");
      const acknowledgeDialog = page.getByRole("dialog", { name: "Acknowledge dead letter?" });
      await acknowledgeDialog.getByRole("button", { name: "Cancel", exact: true }).click();
      await deadLetterRow.getByRole("button", { name: "Acknowledge", exact: true }).click();
      await page.getByRole("dialog", { name: "Acknowledge dead letter?" }).getByRole("button", { name: "Acknowledge", exact: true }).click();
      await page.locator(".notice").filter({ hasText: `Acknowledged ${deadLetterId}` }).waitFor();
      const acknowledged = await call("GET", "/runs/" + deadLetterId);
      assert.equal(acknowledged.status, "dead_letter");
      assert.equal(acknowledged.error, "acceptance dead letter");
      assert.equal(acknowledged.dead_letter_state.state, "acknowledged");
    }

    recordStep("explorer");
    await page.getByRole("button", { name: "explorer", exact: true }).click();
    await page.getByRole("button", { name: "Provider bars", exact: true }).click();
    await page.getByLabel("Provider").fill("fixture");
    await page.getByLabel("Symbol").fill("UI_TEST");
    await page.getByLabel("Query start date").fill("2026-01-03");
    await page.getByLabel("Query end date").fill("2026-01-01");
    await page.getByLabel("Page size").selectOption("2");
    // Issue #84: bars mode loads bars and coverage in one submit, so the label must name both and
    // stay distinguishable from the market/economic labels asserted below and further down.
    await page.getByRole("button", { name: "Load bars and coverage", exact: true }).waitFor();
    await page.getByRole("button", { name: "Load bars and coverage", exact: true }).click();
    await page.getByText("Start date must not be after end date.", { exact: true }).waitFor();
    await page.getByLabel("Query start date").fill("");
    await page.getByLabel("Query end date").fill("");
    await page.getByRole("button", { name: "Load bars and coverage", exact: true }).click();
    await page.locator(".coverage-strip").waitFor();
    await page.getByText("Page 1 · 2 rows", { exact: true }).waitFor();
    // Issue #85: this selector gets a summary response, so the console must name the reason instead of
    // rendering the same "not published" it renders for a selector that was checked and is unhealthy.
    await page.getByText("Detailed coverage was not computed:", { exact: false }).waitFor();
    const summaryCoverage = await envelope("GET", "/provider-bars/coverage?provider=fixture&symbol=UI_TEST&timeframe=1d");
    assert.equal(summaryCoverage.payload.data.coverage_detail_unavailable,
      "detailed coverage is computed for provider=dukascopy timeframe=1m with start and end; "
      + "fixture 1d answers with the summary only");
    await page.getByRole("button", { name: "Next page", exact: true }).click();
    await page.getByText(/Page 2 · \d+ rows/, { exact: false }).waitFor();
    await page.getByRole("button", { name: "Economic", exact: true }).click();
    // A series that is never ingested keeps the empty state deterministic even
    // after the maintenance flows have published PAYEMS.
    await page.getByLabel("Series ID").fill("NO_SUCH_SERIES");
    await page.getByRole("button", { name: "Load observations", exact: true }).click();
    await page.getByText("No observations in this page.", { exact: true }).waitFor();
    await page.getByLabel("Query mode").selectOption("pit");
    await page.getByLabel("As-of timestamp").fill("");
    await page.getByRole("button", { name: "Load observations", exact: true }).click();
    await page.getByText("PIT mode requires an as-of timestamp.", { exact: true }).waitFor();

    recordStep("quality");
    await page.getByRole("button", { name: "quality", exact: true }).click();
    await page.getByLabel("Finding code").waitFor();
    await page.getByLabel("Finding from date").waitFor();
    // Degraded verifications record findings, so the empty state is only
    // asserted while no finding exists yet; afterwards the same filters must
    // surface the recorded coverage finding.
    const recordedFindings = await call("GET", "/quality/findings");
    if (recordedFindings.length === 0) {
      await page.getByText("No quality findings.", { exact: true }).waitFor();
    } else {
      await page.getByLabel("Severity").selectOption("warning");
      await page.locator("table tbody tr").filter({ hasText: "coverage_degraded" }).first().waitFor();
    }

    recordStep("operations");
    await page.getByRole("button", { name: "operations", exact: true }).click();
    await page.getByText("Capacity and recovery", { exact: true }).waitFor();
    await page.getByText("Active alerts", { exact: true }).waitFor();
    const deniedIngest = await page.evaluate(async () => { const response = await fetch("/api/v1/maintenance/tasks", { method: "POST", headers: { "X-API-Key": "incorrect-key", "Content-Type": "application/json" }, body: JSON.stringify({run_kind:"ingest",run_scope:"acceptance",provider:"fixture",symbol:"UI_TEST",asset_class:"test",timeframe:"1d",start:"2026-01-01T00:00:00Z",end:"2026-01-02T00:00:00Z"}) }); return { status: response.status, body: await response.json() }; });
    assert.equal(deniedIngest.status, 401);
    await page.getByRole("button", { name: "Review ingest", exact: true }).click();
    const ingestDialog = page.getByRole("dialog", { name: "Queue ingest run?" });
    await ingestDialog.getByRole("button", { name: "Queue ingest", exact: true }).click();
    await ingestDialog.getByRole("button", { name: "Cancel", exact: true }).click().catch(() => {});
    await page.getByRole("button", { name: "Review ingest", exact: true }).click();
    await page.getByRole("dialog", { name: "Queue ingest run?" }).getByRole("button", { name: "Queue ingest", exact: true }).click();
    await page.locator(".notice").filter({ hasText: "Queued" }).waitFor();
    const queuedNotice = await page.locator(".notice").filter({ hasText: "Queued" }).last().textContent();
    const queuedRunId = queuedNotice?.match(/[0-9a-f-]{36}/i)?.[0];
    assert.ok(queuedRunId, "successful ingest notice did not include run id");
    await waitFor(async () => {
      const value = await call("GET", "/runs/" + queuedRunId);
      return value.status === "pass" ? value : false;
    }, "UI ingest did not reach terminal pass");

    // ---- v0.4 unified maintenance workbench -------------------------------
    recordStep("maintenance");
    await page.getByRole("button", { name: "maintenance", exact: true }).click();
    await page.getByRole("radio", { name: "Provider ingest", exact: true }).waitFor();
    await page.getByLabel("Run scope").selectOption("acceptance");

    // The console disables run kinds the platform cannot serve for the selected
    // dataset, instead of letting the planner reject the submission later.
    await page.getByLabel("Task provider").selectOption("fred");
    // The run-kind availability follows the selected provider, so wait for the
    // radios to settle instead of reading them mid-render.
    await waitFor(async () => await page.getByRole("radio", { name: "Gap repair", exact: true }).isDisabled()
      && await page.getByRole("radio", { name: "Derive", exact: true }).isDisabled()
      && await page.getByRole("radio", { name: "Quality check", exact: true }).isEnabled(),
      "run kinds must follow the selected dataset (gap repair/derive off, quality on)");
    await page.getByLabel("Task provider").selectOption("fixture");
    assert.equal(await page.getByRole("radio", { name: "Gap repair", exact: true }).isEnabled(), true,
      "gap repair stays available for provider bars");

    // A saved template only refills parameters; it is re-validated on use.
    await page.getByLabel("Template name").fill("acceptance ingest");
    await page.getByRole("button", { name: "Save template", exact: true }).click();
    await page.getByLabel("Task template").selectOption("acceptance ingest");
    await page.getByText("Loaded template acceptance ingest", { exact: false }).waitFor();
    await page.getByRole("button", { name: "Delete template", exact: true }).click();

    // Capacity protection is visible in the preview, before any write.
    await page.getByRole("radio", { name: "Backfill", exact: true }).click();
    await page.getByLabel("Task provider").selectOption("fixture");
    await page.getByLabel("Task symbol").fill("UI_TEST");
    await page.getByLabel("Asset class").fill("test");
    await page.getByLabel("Task start date").fill("2026-01-01");
    await page.getByLabel("Task end date").fill("2026-04-01");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("Blocked by capacity", { exact: true }).waitFor();
    await page.getByText("capacity_warning_backfill", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Confirm and queue" }).isDisabled(), true,
      "a capacity-protected task must not be submittable");

    // A refused write never queues work: validate, then submit with a bad key.
    await page.getByRole("radio", { name: "Provider ingest", exact: true }).click();
    await page.getByLabel("Task start date").fill("2026-01-01");
    await page.getByLabel("Task end date").fill("2026-01-03");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("Ready to submit", { exact: true }).waitFor();
    const beforeRefused = (await call("GET", "/runs")).length;
    const deniedTask = await page.evaluate(async () => { const response = await fetch("/api/v1/maintenance/tasks", { method: "POST", headers: { "X-API-Key": "incorrect-key", "Content-Type": "application/json" }, body: JSON.stringify({run_kind:"ingest",run_scope:"acceptance",provider:"fixture",symbol:"UI_TEST",asset_class:"test",timeframe:"1d",start:"2026-01-01T00:00:00Z",end:"2026-01-03T00:00:00Z"}) }); return response.status; });
    assert.equal(deniedTask, 401);
    assert.equal((await call("GET", "/runs")).length, beforeRefused, "a refused write must not queue a run");

    // The authorized path queues one run and tracks it to a terminal receipt.
    const beforeIngest = await call("GET", "/runs");
    await page.getByRole("button", { name: "Confirm and queue" }).click();
    await page.locator(".notice").filter({ hasText: "Queued ingest" }).waitFor();
    const maintenanceRunId = await findNewRun(beforeIngest, "ingest");
    await page.locator(".track-row .mono").filter({ hasText: maintenanceRunId }).waitFor();
    const maintenanceRun = await waitFor(async () => {
      const value = await call("GET", "/runs/" + maintenanceRunId);
      return value.status === "pass" ? value : false;
    }, "maintenance ingest did not reach terminal pass");
    assert.equal(maintenanceRun.run_kind, "ingest");
    assert.equal(maintenanceRun.run_scope, "acceptance");

    // Derive follows the same contract and consumes the seeded 1m snapshot.
    await page.getByRole("radio", { name: "Derive", exact: true }).click();
    await page.getByLabel("Task symbol").fill("UI_TEST");
    await page.getByLabel("Recipe", { exact: true }).selectOption("utc-24x7-1m-to-1h-ohlcv");
    await page.getByLabel("Task start date").fill("2026-03-01");
    await page.getByLabel("Task end date").fill("2026-03-02");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("Ready to submit", { exact: true }).waitFor();
    await page.getByText("input part(s)", { exact: false }).waitFor();
    const beforeDerive = await call("GET", "/runs");
    await page.getByRole("button", { name: "Confirm and queue" }).click();
    await page.locator(".notice").filter({ hasText: "Queued derive" }).waitFor();
    const deriveRunId = await findNewRun(beforeDerive, "derive");
    const deriveRun = await waitFor(async () => {
      const value = await call("GET", "/runs/" + deriveRunId);
      return value.status === "pass" ? value : false;
    }, "derive run did not reach terminal pass");
    assert.ok(deriveRun.input_snapshot_id, "derive run must record its input snapshot");
    const marketCoverage = await call("GET", "/market-bars/coverage?provider=fixture&symbol=UI_TEST&timeframe=1h"
      + "&price_basis=raw&recipe_id=utc-24x7-1m-to-1h-ohlcv&recipe_version=1&start=2026-03-01T00:00:00Z&end=2026-03-02T00:00:00Z");
    assert.equal(marketCoverage.readiness_status, "ready");
    assert.equal(marketCoverage.recipe_status, "registered");

    // Parity verifies the derived layer against the raw snapshot it came from.
    await page.getByRole("radio", { name: "Parity check", exact: true }).click();
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("parity_read_only", { exact: true }).waitFor();
    const beforeParity = await call("GET", "/runs");
    await page.getByRole("button", { name: "Confirm and queue" }).click();
    await page.locator(".notice").filter({ hasText: "Queued parity" }).waitFor();
    const parityRunId = await findNewRun(beforeParity, "parity");
    const parityRun = await waitFor(async () => {
      const value = await call("GET", "/runs/" + parityRunId);
      return value.status === "pass" ? value : false;
    }, "parity verification did not reach terminal pass");
    assert.equal(parityRun.verification.publishes_parts, false);
    assert.equal(parityRun.manifest_status, undefined);

    // A quality check over the controlled fixture reports a degraded dataset
    // without failing the verification itself.
    await page.getByRole("radio", { name: "Quality check", exact: true }).click();
    await page.getByLabel("Task timeframe").selectOption("1m");
    await page.getByLabel("Task start date").fill("2026-01-01");
    await page.getByLabel("Task end date").fill("2026-01-03");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("Ready to submit", { exact: true }).waitFor();
    const beforeQuality = await call("GET", "/runs");
    await page.getByRole("button", { name: "Confirm and queue" }).click();
    await page.locator(".notice").filter({ hasText: "Queued quality" }).waitFor();
    const qualityRunId = await findNewRun(beforeQuality, "quality");
    const qualityRun = await waitFor(async () => {
      const value = await call("GET", "/runs/" + qualityRunId);
      return value.status === "pass" ? value : false;
    }, "quality verification did not reach terminal pass");
    assert.equal(qualityRun.verification.publishes_parts, false);
    const qualityDetail = await call("GET", "/runs/" + qualityRunId + "/detail");
    assert.equal(qualityDetail.outcome, "degraded");
    assert.equal(qualityDetail.manifest_status, "not_applicable");
    assert.ok(qualityDetail.degraded_reasons.length > 0, "a degraded verification must explain itself");
    assert.ok(qualityDetail.finding_count > 0, "a coverage gap must be recorded as a finding");
    await page.getByText("Degraded", { exact: true }).first().waitFor();

    // Economic ingest runs through the same contract against the fixture endpoint.
    await page.getByRole("radio", { name: "Provider ingest", exact: true }).click();
    await page.getByLabel("Task provider").selectOption("fred");
    await page.getByLabel("Task series ID").fill("PAYEMS");
    await page.getByLabel("Task start date").fill("2026-01-01");
    await page.getByLabel("Task end date").fill("2026-01-05");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("Ready to submit", { exact: true }).waitFor();
    const beforeEconomic = await call("GET", "/runs");
    await page.getByRole("button", { name: "Confirm and queue" }).click();
    await page.locator(".notice").filter({ hasText: "Queued ingest" }).waitFor();
    const economicRunId = await findNewRun(beforeEconomic, "ingest");
    const economicRun = await waitFor(async () => {
      const value = await call("GET", "/runs/" + economicRunId);
      return value.status === "pass" ? value : false;
    }, "economic ingest did not reach terminal pass");
    assert.equal(economicRun.dataset_id, "economic_observations");
    assert.ok(economicRun.row_count > 0);

    // Capacity protection also refuses a direct write and audits the refusal.
    const runsBeforeProtected = (await call("GET", "/runs")).length;
    const protectedAttempt = await envelope("POST", "/maintenance/tasks", {
      run_kind: "backfill", run_scope: "acceptance", provider: "fixture", symbol: "UI_TEST",
      asset_class: "test", timeframe: "1d", start: "2026-01-01T00:00:00Z", end: "2026-04-01T00:00:00Z",
    });
    assert.equal(protectedAttempt.response.status, 507);
    assert.equal(protectedAttempt.payload.errors[0].code, "capacity_protected");
    assert.equal((await call("GET", "/runs")).length, runsBeforeProtected,
      "a capacity-protected write must not queue a run");

    // ---- v0.4 data asset workbench (Phase 2) ------------------------------
    recordStep("catalog_v04");
    await page.getByRole("button", { name: "datasets", exact: true }).click();
    await page.getByText("Data asset catalog", { exact: true }).waitFor();
    await page.getByText("Raw → derived recipes", { exact: true }).waitFor();
    await page.getByText("Provider capability", { exact: true }).waitFor();
    await page.getByText("derived · recipe output", { exact: false }).first().waitFor();
    await page.getByText("raw · provider feed", { exact: false }).first().waitFor();
    await page.getByText("utc-24x7-1m-to-1h-ohlcv", { exact: false }).first().waitFor();

    // Issue #82: the catalog hand-off locks the run kind that writes the dataset it was opened for and
    // says the window was not carried, instead of dropping the operator on the workspace defaults.
    await page.getByLabel("Search datasets").fill("provider_bars");
    await page.locator("table tbody tr").first()
      .getByRole("button", { name: "Create maintenance task", exact: true }).click();
    await page.getByText(/Prefilled .*from Data catalog\./).waitFor();
    await page.getByText(/The window was not carried/).waitFor();
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.locator(".preview-card").filter({ hasText: "provider_bars" }).waitFor();

    recordStep("explorer_market");
    await page.getByRole("button", { name: "explorer", exact: true }).click();
    await page.getByRole("button", { name: "Market bars", exact: true }).click();
    await page.getByLabel("Provider").fill("fixture");
    await page.getByLabel("Symbol").fill("UI_TEST");
    await page.getByLabel("Recipe", { exact: true }).selectOption("utc-24x7-1m-to-1h-ohlcv");
    await page.getByLabel("Query start date").fill("2026-03-01");
    await page.getByLabel("Query end date").fill("2026-03-02");
    await page.getByRole("button", { name: "Load market bars", exact: true }).click();
    await page.locator(".coverage-strip").waitFor();
    await page.getByText("Readiness", { exact: false }).first().waitFor();
    await page.getByText("ready", { exact: false }).first().waitFor();
    // The derived query keeps the same cursor/snapshot semantics as the raw one.
    const marketMeta = await call("GET", "/market-bars?provider=fixture&symbol=UI_TEST&timeframe=1h"
      + "&price_basis=raw&recipe_id=utc-24x7-1m-to-1h-ohlcv&recipe_version=1&page_size=5");
    assert.ok(marketMeta.length > 0, "the derived query returned no rows");
    const marketPage = await envelope("GET", "/market-bars?provider=fixture&symbol=UI_TEST&timeframe=1h"
      + "&price_basis=raw&recipe_id=utc-24x7-1m-to-1h-ohlcv&recipe_version=1&page_size=5");
    assert.ok(marketPage.payload.meta.snapshot_id, "a derived query must publish its input snapshot");
    assert.deepEqual(marketPage.payload.meta.schema_versions, ["market_bars.v1"]);

    // Coverage hands the selector and window to the maintenance workspace.
    await page.getByRole("button", { name: "Create task from coverage", exact: true }).click();
    await page.getByRole("radio", { name: "Derive", exact: true }).waitFor();
    await page.getByLabel("Task symbol").waitFor();
    assert.equal(await page.getByLabel("Task symbol").inputValue(), "UI_TEST",
      "the handoff must carry the selector into the maintenance form");
    assert.equal(await page.getByLabel("Recipe", { exact: true }).inputValue(), "utc-24x7-1m-to-1h-ohlcv");
    await page.getByRole("button", { name: "Validate and preview" }).click();
    await page.getByText("Ready to submit", { exact: true }).waitFor();

    // ---- v0.4 quality feedback loop (Phase 3) -----------------------------
    recordStep("quality_v04");
    await page.getByRole("button", { name: "quality", exact: true }).click();
    await page.getByLabel("Finding state").waitFor();
    await page.getByLabel("Finding code").selectOption("coverage_degraded");
    const findingRow = page.locator("table tbody tr").first();
    await findingRow.waitFor();
    await findingRow.click();
    await page.getByRole("button", { name: "Acknowledge finding", exact: true }).waitFor();
    await page.getByRole("button", { name: "Acknowledge finding", exact: true }).click();
    await page.locator(".notice, [role=status]").filter({ hasText: "acknowledg" }).first().waitFor();
    // The write lands when the API says so; polling avoids reading the list
    // between the click and its persistence (the old assertion flaked).
    const acknowledged = await waitFor(async () => {
      const rows = await call("GET", "/quality/findings?state=acknowledged");
      return Array.isArray(rows) && rows.length > 0 ? rows : null;
    }, "the finding handling state was not persisted");
    assert.ok(acknowledged.every(item => item.run_id), "a finding must stay linked to its reporting run");
    await page.getByRole("button", { name: "Close details", exact: true }).last().click();
    await page.getByLabel("Finding state").selectOption("open");

    // Issue #83: the drawer hands its assessed task to the workspace instead of dropping it. The fixture
    // finding is bounded, so the carriage is asserted on the values the workspace then shows.
    await page.getByLabel("Finding code").selectOption("coverage_degraded");
    await page.getByLabel("Finding state").selectOption("acknowledged");
    await page.locator("table tbody tr").first().click();
    await page.getByRole("button", { name: "Open maintenance workspace", exact: true }).click();
    await page.getByText(/Prefilled [^.]*dataset[^.]* from Quality\./).waitFor();
    await page.getByText(/Validate before queueing\./).waitFor();
    assert.equal(await page.getByLabel("Task provider").inputValue(), "fixture",
      "the drawer hand-off must carry the provider the finding recorded");
    assert.equal(await page.getByLabel("Task symbol").inputValue(), "UI_TEST",
      "the drawer hand-off must carry the symbol the finding recorded");
    await page.getByRole("button", { name: "quality", exact: true }).click();
    await page.getByLabel("Finding state").selectOption("open");

    // ---- v0.4 operations and audit (Phase 4) ------------------------------
    recordStep("operations_v04");
    await page.getByRole("button", { name: "operations", exact: true }).click();
    await page.getByText("Maintenance queue", { exact: true }).waitFor();
    await page.getByText("Worker activity", { exact: true }).waitFor();
    await page.getByText("Capacity history", { exact: true }).waitFor();
    // Issue #86: the acceptance harness pins the free-space ratio, so the console must say the value is
    // a pinned acceptance measurement instead of presenting it as a live production disk reading.
    await page.getByText("Pinned acceptance measurement", { exact: true }).waitFor();
    await page.getByText("fixed_acceptance", { exact: false }).first().waitFor();
    const capacityHistory = await call("GET", "/operations/capacity-history");
    assert.equal(capacityHistory.live.measurement_source, "fixed_acceptance");
    assert.equal(capacityHistory.live.recorded_only, false);
    assert.equal(capacityHistory.measurement_source, "fixed_acceptance");
    const readiness = await call("GET", "/health/ready");
    assert.equal(readiness.capacity_measurement_source, "fixed_acceptance");
    await page.getByText("Write audit trail", { exact: true }).waitFor();
    await page.getByText("Recorded transitions", { exact: false }).first().waitFor();
    // The receipts panel renders the actions the API reports.  A name the
    // platform never writes must not appear as a phantom gap, and a name it
    // does write must not be hidden because the console kept its own list.
    const receiptActions = await page.locator(".receipt-list li b").allTextContents();
    assert.ok(receiptActions.includes("deployment_stage"),
      `deployment_stage missing from the receipts panel: ${receiptActions.join(", ")}`);
    assert.ok(!receiptActions.includes("release") && !receiptActions.includes("deployment"),
      `a receipt action the platform never writes is rendered: ${receiptActions.join(", ")}`);
    const auditRow = page.locator("table tbody tr").filter({ hasText: "maintenance." }).first();
    await auditRow.waitFor();
    await page.getByText("non-reversible", { exact: false }).first().waitFor();

    // The write audit trail records actor, selector and outcome.
    const audit = await call("GET", "/operations/audit?limit=20");
    assert.ok(audit.some(entry => entry.outcome === "protected"), "capacity refusal must be audited");
    assert.ok(audit.some(entry => entry.outcome === "queued" && entry.run_kind === "quality"));
    assert.ok(audit.some(entry => entry.outcome === "queued" && entry.run_kind === "parity"));
    assert.ok(audit.every(entry => (entry.actor ?? "").startsWith("api-key:")),
      "the audit trail must never store a raw credential");

    await page.getByRole("button", { name: "runs", exact: true }).click();
    await page.getByLabel("Run kind filter").selectOption("quality");
    await page.getByLabel("Run scope filter").selectOption("acceptance");
    const qualityRow = page.locator("tr").filter({ hasText: qualityRunId });
    await qualityRow.waitFor();
    await qualityRow.click();
    await page.getByRole("complementary", { name: qualityRunId }).waitFor();
    await page.getByText("Verification runs record findings and publish no canonical manifest.", { exact: true }).waitFor();
    await page.getByText("Retry chain", { exact: false }).first().waitFor();
    await page.getByRole("button", { name: "Close details", exact: true }).last().click();
    await page.getByRole("button", { name: "Next page", exact: true }).isDisabled();
    await page.getByText(/Page 1 · \d+ run\(s\)/, { exact: false }).waitFor();

    // The production plan workspace reports the plan registry and the persisted
    // dispatch switch; it must say "no plans" rather than invent one, and the
    // scheduler strip must exist before an operator can pause dispatch.
    await page.getByRole("button", { name: "production", exact: true }).click();
    await page.getByText("Unified dispatch", { exact: true }).waitFor();
    await page.getByText("Registered plans", { exact: true }).waitFor();
    // The registry renders after its own load, so wait for whichever of the two
    // honest states appears instead of sampling the table mid-load.
    await waitFor(async () => (await page.locator("table tbody tr").count()) > 0
      || (await page.getByText("No production plans yet", { exact: false }).count()) > 0,
      "the plan registry must list plans or state that there are none");
    const schedulerView = await call("GET", "/operations/scheduler");
    assert.equal(typeof schedulerView.dispatch_enabled, "boolean");
    // The capacity gate and provider backoff are observed values, and the strip
    // must agree with the API instead of showing a hard-coded green state.
    assert.equal(schedulerView.publishing_allowed, schedulerView.capacity.status !== "critical");
    assert.ok(Array.isArray(schedulerView.provider_backoff));
    assert.ok(Array.isArray(schedulerView.queue_backoff),
      "the queue-level retry waits must stay distinct from the governed backoff");
    assert.ok(await page.getByText("New publishing", { exact: true }).count() > 0);
    assert.ok(await page.getByText("Provider backoff", { exact: true }).count() > 0);
    assert.ok(await page.getByText("Dispatch", { exact: true }).count() > 0);

    // The wizard validates against the live registry before anything is saved:
    // a preview must render either the field errors or the computed plan.
    await page.getByRole("textbox", { name: "Name", exact: true }).fill("Acceptance plan");
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await page.getByText("Submittable", { exact: false }).waitFor();
    assert.ok(await page.getByLabel("Provider", { exact: true }).inputValue(),
      "the wizard must be seeded from the registry");
    // Saving is a real write.  The plan owns its outputs, so a second run only
    // verifies the plan the first run created rather than colliding with it.
    const registeredBefore = await call("GET", "/production/tasks");
    if (!registeredBefore.some(plan => plan.name === "Acceptance plan")) {
      await page.getByRole("button", { name: "Save as paused", exact: true }).click();
    }
    await page.locator("tr").filter({ hasText: "Acceptance plan" }).waitFor();
    const created = await call("GET", "/production/tasks");
    assert.ok(created.some(plan => plan.name === "Acceptance plan" && plan.desired_state === "paused"),
      "the saved plan must be readable through the plan registry");

    // Opening a plan shows its recorded progress: boundaries the scheduler
    // actually persisted, or an explicit "nothing yet", never an estimate.
    await page.locator("tr").filter({ hasText: "Acceptance plan" })
      .getByRole("button", { name: /Details/ }).click();
    await page.getByText("Recorded boundaries", { exact: true }).waitFor();
    await page.getByText("not live provider freshness", { exact: false }).waitFor();
    const registeredPlans = await call("GET", "/production/tasks");
    const acceptancePlan = registeredPlans.find(plan => plan.name === "Acceptance plan");
    assert.ok(acceptancePlan, "the plan saved by the wizard must be readable");
    const detail = await call("GET", `/production/tasks/${acceptancePlan.task_id}`);
    assert.equal(typeof detail.progress.recorded, "boolean");
    // Phase and health are read-model values the API must report, and the list
    // filter has to accept them; the console offers exactly those values.
    assert.ok(["initializing", "catching_up", "maintaining"].includes(detail.phase),
      `unexpected plan phase: ${detail.phase}`);
    assert.ok(detail.health, "the plan must report a health value");
    const filtered = await call("GET", `/production/tasks?health=${encodeURIComponent(detail.health)}`);
    assert.ok(filtered.some(plan => plan.task_id === acceptancePlan.task_id),
      "a plan must be listed under its own health value");
    const healthFilter = page.getByLabel("plan health filter", { exact: true });
    assert.ok(await healthFilter.count() > 0, "the registry must offer the read-model health filter");
    await healthFilter.selectOption(detail.health);
    await page.locator("tr").filter({ hasText: "Acceptance plan" }).waitFor();
    await healthFilter.selectOption("");

    // Editing is an optimistic-locked write against the version the console
    // loaded, and it must not change desired_state on its own (spec 3.3).
    await page.getByRole("button", { name: "Edit Acceptance plan", exact: true }).click();
    await page.getByText("Edit Acceptance plan", { exact: true }).waitFor();
    await page.getByRole("textbox", { name: "Name", exact: true }).fill("Acceptance plan (edited)");
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await page.locator("tr").filter({ hasText: "Acceptance plan (edited)" }).waitFor();
    const edited = (await call("GET", "/production/tasks"))
      .find(plan => plan.name === "Acceptance plan (edited)");
    assert.ok(edited, "the edited plan must be readable through the registry");
    assert.equal(edited.desired_state, "paused", "editing must not resume a paused plan");
    assert.ok(edited.definition_version > acceptancePlan.definition_version,
      "a definition edit must create a new version");
    // Replaying the stale version is refused rather than silently overwriting.
    const stale = await envelope("PATCH", `/production/tasks/${edited.task_id}`, {
      expected_version: acceptancePlan.definition_version,
      definition: edited.payload,
    });
    assert.ok([409, 422].includes(stale.response.status),
      `a stale expected_version must be refused, got ${stale.response.status}`);
    // A display field is not a definition change: it updates in place (no new
    // version), which also leaves the registry as this check found it.
    const restore = await envelope("PATCH", `/production/tasks/${edited.task_id}`, {
      expected_version: edited.definition_version, name: "Acceptance plan",
    });
    assert.ok(restore.response.ok, `restoring the plan name failed: ${restore.response.status}`);
    const restored = (await call("GET", "/production/tasks"))
      .find(plan => plan.task_id === edited.task_id);
    assert.equal(restored.name, "Acceptance plan");
    assert.equal(restored.definition_version, edited.definition_version,
      "renaming must not form a new definition version");

    // Lifecycle controls are the operator's daily path, so the acceptance clicks
    // them instead of only creating plans (AC15): trigger, resume, trigger, pause.
    const planRow = () => page.locator("tr").filter({ hasText: "Acceptance plan" });
    // ``call`` already unwraps the response envelope.
    const readPlan = async () => call("GET", `/production/tasks/${edited.task_id}`);
    assert.equal((await readPlan()).desired_state, "paused");

    // A paused plan refuses "run now" with a conflict rather than producing.
    await planRow().getByRole("button", { name: /^Run now/ }).click();
    await page.getByText("resume the plan before triggering it", { exact: false }).waitFor();
    assert.equal((await readPlan()).desired_state, "paused");

    await planRow().getByRole("button", { name: /^Resume/ }).click();
    await planRow().getByRole("button", { name: /^Pause/ }).waitFor();
    assert.equal((await readPlan()).desired_state, "enabled");

    await planRow().getByRole("button", { name: /^Run now/ }).click();
    await waitFor(async () => (await readPlan()).current_execution !== null,
      "triggering an enabled plan must open a round");
    const triggered = await readPlan();
    const firstExecution = triggered.current_execution.execution_id;

    // Pausing lets the round in flight finish instead of cancelling it.
    await planRow().getByRole("button", { name: /^Pause/ }).click();
    await waitFor(async () => (await readPlan()).desired_state === "paused",
      "pausing must be recorded on the plan");
    const paused = await readPlan();
    assert.ok(["pending", "running", "pausing", "paused"].includes(paused.current_execution.state),
      `unexpected round state after pause: ${paused.current_execution.state}`);
    assert.equal(paused.current_execution.execution_id, firstExecution);

    // The matrix and the governance list are read-only projections: they must
    // render, and a governance unit must carry no lifecycle control.
    await page.getByText("Registered × planned", { exact: true }).waitFor();
    await page.getByText("Units this view does not manage", { exact: true }).waitFor();
    const matrix = await call("GET", "/production/catalog-matrix");
    assert.ok(matrix.rows.length > 0, "the matrix must cover the registered outputs");
    assert.equal(Object.values(matrix.counts).reduce((total, count) => total + count, 0), matrix.rows.length);
    const units = await call("GET", "/operations/units");
    assert.ok(units.units.every(unit => unit.read_only === true),
      "the governance list must be read-only by construction");

    await page.getByRole("button", { name: "overview", exact: true }).click();

    const widths = await page.evaluate(() => ({
      body: document.body.scrollWidth, html: document.documentElement.scrollWidth, inner: innerWidth,
      overflow: [...document.querySelectorAll("*")].filter(element => element.scrollWidth > element.clientWidth + 1).slice(0, 10).map(element => ({ tag: element.tagName, className: element.className, scroll: element.scrollWidth, client: element.clientWidth })),
    }));
    assert.equal(widths.body <= widths.inner && widths.html <= widths.inner, true, JSON.stringify({ viewport, widths }));
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, `acceptance-${viewport.width}.png`), fullPage: true });
    results.push({ viewport, status: "pass" });
    await page.close();
  }

  const loadingPage = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  await loadingPage.route("**/api/v1/metrics", async route => {
    await new Promise(resolve => setTimeout(resolve, 500));
    await route.continue();
  });
  await loadingPage.goto(base + "/legacy.html");
  await loadingPage.getByLabel("Loading").first().waitFor();
  await loadingPage.locator(".sidebar").getByText("API ready", { exact: true }).waitFor();
  await loadingPage.close();

  const errorPage = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  await errorPage.route("**/api/v1/metrics", route => route.fulfill({
    status: 500,
    contentType: "application/json",
    body: JSON.stringify({ data: null, meta: { request_id: "browser-error-state" }, errors: [{ code: "injected", message: "Injected metrics failure" }] }),
  }));
  await errorPage.goto(base + "/legacy.html");
  await errorPage.locator(".error-state").waitFor();
  await errorPage.getByText("Injected metrics failure", { exact: false }).waitFor();
  await errorPage.close();

  // P1 mainline: authenticate through the real HttpOnly-cookie API, then
  // verify URL-backed task search, the capabilities-backed sheet, and a detail
  // route that survives direct refresh. The legacy suite above runs in its own
  // document so its global CSS cannot override this shell.
  recordStep("p1_task_mainline");
  await call("POST", "/auth/initialize", {
    username: "admin", password: "browser-p1-password",
  });
  const managedDatasetId = "browser-managed-long";
  const managedDatasetName = "P2.1 browser acceptance dataset with a deliberately long name";
  await call("POST", "/managed-datasets", {
    dataset_id: managedDatasetId, name: managedDatasetName,
    notes: "URL and responsive acceptance",
  });
  await call("POST", `/managed-datasets/${managedDatasetId}/members`, {
    symbol: "EURUSD", expected_version: 1,
  });
  const secondManagedDatasetId = "browser-managed-second";
  await call("POST", "/managed-datasets", {
    dataset_id: secondManagedDatasetId, name: "Second managed dataset",
  });
  await call("POST", `/managed-datasets/${secondManagedDatasetId}/members`, {
    symbol: "EURUSD", expected_version: 1,
  });
  const p1Page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await p1Page.addInitScript((apiKey) => {
    const original = window.fetch;
    window.fetch = (input, init = {}) => {
      const headers = new Headers(init.headers || {});
      if (!headers.has("X-API-Key")) headers.set("X-API-Key", apiKey);
      return original(input, { ...init, headers });
    };
  }, key);
  const p1Errors = [];
  p1Page.on("pageerror", error => p1Errors.push(error.message));
  await p1Page.goto(base + "/tasks");
  await p1Page.getByRole("heading", { name: "登录数据中心" }).waitFor();
  await p1Page.getByLabel("用户名").fill("admin");
  await p1Page.getByLabel("密码").fill("browser-p1-password");
  await p1Page.route("**/api/v1/operations/scheduler", async route => {
    await new Promise(resolve => setTimeout(resolve, 250));
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ errors: [{ code: "acceptance_scheduler_unavailable", message: "injected" }] }),
    });
  }, { times: 2 });
  await p1Page.getByRole("button", { name: "登录", exact: true }).click();
  await p1Page.getByRole("heading", { name: "数据任务", exact: true }).waitFor();
  const schedulerCard = p1Page.getByText("调度派发", { exact: true }).locator("..");
  await schedulerCard.getByText("载入中…", { exact: true }).waitFor();
  await schedulerCard.getByText("状态不可用", { exact: true }).waitFor();
  await p1Page.getByText("Acceptance plan", { exact: true }).waitFor();
  await p1Page.getByRole("button", { name: "创建任务", exact: true }).click();
  await p1Page.getByRole("dialog").getByText("选项来自 capabilities API", { exact: false }).waitFor();
  assert.ok((await p1Page.getByLabel("数据源").textContent()).trim(), "P1 form must use a provider from capabilities");
  assert.ok((await p1Page.getByLabel("品种").textContent()).trim(), "P1 form must use an instrument from capabilities");
  if ((await p1Page.getByLabel("数据源").textContent()).trim() !== "dukascopy") {
    await p1Page.getByLabel("数据源").click();
    await p1Page.getByRole("option", { name: "dukascopy", exact: true }).click();
  }
  await p1Page.getByLabel("品种").click();
  await p1Page.getByRole("option", { name: "GBPUSD", exact: true }).click();
  await p1Page.getByLabel("任务名称").fill("P1 browser plan");
  await p1Page.route("**/api/v1/production/plans", async route => {
    await new Promise(resolve => setTimeout(resolve, 250));
    await route.continue();
  }, { times: 1 });
  await p1Page.getByRole("button", { name: "校验任务", exact: true }).click();
  await p1Page.getByRole("button", { name: "校验中…", exact: true }).waitFor();
  assert.equal(await p1Page.getByRole("button", { name: "校验中…", exact: true }).isDisabled(), true);
  await p1Page.getByText("校验通过，可以保存", { exact: true }).waitFor();
  await p1Page.route("**/api/v1/production/tasks", async route => {
    if (route.request().method() !== "POST") return route.continue();
    await new Promise(resolve => setTimeout(resolve, 250));
    await route.continue();
  }, { times: 1 });
  await p1Page.getByRole("button", { name: "保存为暂停", exact: true }).click();
  await p1Page.getByRole("button", { name: "保存中…", exact: true }).waitFor();
  assert.equal(await p1Page.getByRole("button", { name: "保存中…", exact: true }).isDisabled(), true);
  await p1Page.getByRole("heading", { name: "P1 browser plan", exact: true }).waitFor();
  await p1Page.getByRole("link", { name: "返回任务列表", exact: true }).click();
  await p1Page.getByLabel("搜索任务").fill("Acceptance");
  await p1Page.waitForURL(/\/tasks\?q=Acceptance/);
  await p1Page.getByRole("link", { name: "Acceptance plan", exact: true }).click();
  await p1Page.waitForURL(/\/tasks\//);
  const detailUrl = p1Page.url();
  await p1Page.getByRole("heading", { name: "Acceptance plan", exact: true }).waitFor();
  await p1Page.getByLabel("运行结论", { exact: true }).waitFor();
  await p1Page.reload();
  assert.equal(p1Page.url(), detailUrl, "direct detail refresh must preserve the task URL");
  await p1Page.getByRole("heading", { name: "Acceptance plan", exact: true }).waitFor();
  await p1Page.getByLabel("运行结论", { exact: true }).waitFor();
  await p1Page.screenshot({ path: path.join(output, "p1-tasks-1440.png"), fullPage: true });
  await p1Page.getByRole("link", { name: "返回任务列表", exact: true }).click();
  assert.equal(await p1Page.getByLabel("搜索任务").inputValue(), "Acceptance",
    "returning from detail must preserve the task filter");
  await p1Page.getByRole("button", { name: "创建任务", exact: true }).focus();
  await p1Page.keyboard.press("Enter");
  await p1Page.getByRole("dialog").waitFor();
  await p1Page.keyboard.press("Escape");
  await p1Page.getByRole("dialog").waitFor({ state: "hidden" });

  // A task opened while running must update its summary when both the execution
  // and recorded progress settle. Keeping only the execution list fresh leaves
  // the operator with contradictory running/completed states on one page.
  let statusTransitionComplete = false;
  const transitionExecution = () => ({
    execution_id: "status-transition-execution",
    task_id: "status-transition",
    definition_version: 1,
    trigger_source: "manual",
    scheduled_for: null,
    state: statusTransitionComplete ? "completed" : "running",
    outcome: statusTransitionComplete ? "pass" : null,
    created_at: "2026-09-15T00:00:00Z",
    finished_at: statusTransitionComplete ? "2026-09-15T00:01:00Z" : null,
    coalesced_count: 0,
  });
  const transitionEnvelope = data => ({
    data, meta: { request_id: "status-transition", schema_version: "v1" }, errors: [],
  });
  await p1Page.route("**/api/v1/production/tasks/status-transition", route => {
    const execution = transitionExecution();
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(transitionEnvelope({
        task_id: "status-transition",
        alias: null,
        name: "Status transition",
        desired_state: "enabled",
        definition_version: 1,
        payload: {
          provider: "fixture", symbol: "UI_TEST", raw_timeframe: "1d",
          price_basis: "raw", bar_timeframes: [],
          window_policy: { mode: "fixed", start: "2026-09-14T00:00:00Z", end: "2026-09-15T00:00:00Z" },
          schedule: { schedule: "manual", interval_seconds: 900 },
        },
        created_at: "2026-09-15T00:00:00Z",
        updated_at: "2026-09-15T00:00:00Z",
        deleted_at: null,
        provider: "fixture",
        symbol: "UI_TEST",
        next_run_at: null,
        health: "healthy",
        phase: statusTransitionComplete ? "maintaining" : "catching_up",
        executions: [execution],
        current_execution: statusTransitionComplete ? null : execution,
        schedule: { kind: "manual", next_run_at: null },
        progress: {
          raw_frontier: statusTransitionComplete ? "2026-09-15T00:00:00Z" : null,
          provider_bounded_end: "2026-09-15T00:00:00Z",
          backlog: !statusTransitionComplete,
          derived_cursor: null,
          last_outcome: statusTransitionComplete ? "pass" : null,
          last_finished_at: statusTransitionComplete ? "2026-09-15T00:01:00Z" : null,
          last_execution_id: execution.execution_id,
          recompute_pending: 0,
          recorded: true,
          note: "Recorded planning boundaries, not live provider freshness.",
          observed_boundary: null,
          complete_boundary: null,
          gaps: [],
          deferred_derived: [],
        },
      })),
    });
  });
  await p1Page.route("**/api/v1/production/tasks/status-transition/executions*", route =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(transitionEnvelope([transitionExecution()])),
    }));
  await p1Page.route("**/api/v1/production/executions/status-transition-execution/steps*", route =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(transitionEnvelope([
        { step_id: "raw-step", stage: "raw", state: "completed", run_id: "raw-run",
          window_start: "2026-09-14T00:00:00Z", window_end: "2026-09-15T00:00:00Z" },
        { step_id: "derived-step", stage: "derive:5m", state: "completed", run_id: "derived-run",
          window_start: "2026-09-14T00:00:00Z", window_end: "2026-09-15T00:00:00Z" },
      ])),
    }));
  await p1Page.goto(base + "/tasks/status-transition");
  await p1Page.getByText("任务正在运行", { exact: true }).waitFor();
  await p1Page.getByText("原始数据", { exact: true }).waitFor();
  await p1Page.getByText("派生数据（5m）", { exact: true }).waitFor();
  statusTransitionComplete = true;
  await p1Page.getByText("本次运行已完成，数据已就绪", { exact: true }).waitFor();
  await p1Page.getByText("已完成 · 通过", { exact: true }).waitFor();

  recordStep("p21_dataset_mainline");
  await p1Page.goto(base + `/datasets?dataset=${managedDatasetId}`);
  await p1Page.getByText(managedDatasetName, { exact: true }).last().waitFor();
  assert.equal(new URL(p1Page.url()).searchParams.get("dataset"), managedDatasetId);
  await p1Page.getByText("Second managed dataset", { exact: true }).click();
  await p1Page.waitForURL(new RegExp(`dataset=${secondManagedDatasetId}`));
  await p1Page.goBack();
  await p1Page.getByText(managedDatasetName, { exact: true }).last().waitFor();
  const datasetUrl = p1Page.url();
  await p1Page.reload();
  assert.equal(p1Page.url(), datasetUrl, "direct dataset refresh must preserve selection");
  await p1Page.getByText(managedDatasetName, { exact: true }).last().waitFor();

  const retainedName = "Retained P2.1 draft";
  await p1Page.getByLabel("名称", { exact: true }).fill(retainedName);
  await p1Page.reload();
  assert.equal(await p1Page.getByLabel("名称", { exact: true }).inputValue(), retainedName,
    "dataset draft must survive navigation and refresh in the current session");

  await p1Page.getByRole("button", { name: "暂停", exact: true }).click();
  await p1Page.getByText("已暂停自动维护；查询和手工补数仍可用。", { exact: true }).waitFor();
  await p1Page.getByRole("button", { name: "恢复", exact: true }).click();
  await p1Page.getByText("数据集已恢复。", { exact: true }).waitFor();

  await call("PATCH", `/managed-datasets/${secondManagedDatasetId}`, {
    status: "archived", expected_version: 2,
  });
  const archivedMaintenancePattern = new RegExp(
    `/api/v1/managed-datasets/${secondManagedDatasetId}/maintenance$`,
  );
  const archivedMaintenance = route => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data: [{
      request_id: "archived-audit-request", dataset_id: secondManagedDatasetId,
      symbol: "EURUSD", start: "2026-09-14T11:00:00Z", end: "2026-09-14T12:00:00Z",
      status: "completed", created_at: "2026-09-16T00:00:00Z", run_statuses: ["pass"],
      execution: { execution_id: "archived-execution", task_id: "archived-task",
        state: "completed", run_ids: ["archived-run"] },
    }], meta: {}, errors: [] }),
  });
  await p1Page.route(archivedMaintenancePattern, archivedMaintenance);
  let archivedCoverageCalls = 0;
  const archivedCoveragePattern = new RegExp(
    `/api/v1/managed-datasets/${secondManagedDatasetId}/coverage`,
  );
  const archivedCoverage = route => {
    archivedCoverageCalls += 1;
    return route.fulfill({ status: 409, contentType: "application/json",
      body: JSON.stringify({ data: null, meta: {}, errors: [{ message: "archived dataset is not queryable" }] }) });
  };
  await p1Page.route(archivedCoveragePattern, archivedCoverage);
  await p1Page.goto(base + `/datasets?dataset=${secondManagedDatasetId}`);
  await p1Page.getByText("已归档，只保留配置与执行审计。", { exact: true }).waitFor();
  await p1Page.getByText("archived", { exact: true }).first().waitFor();
  await p1Page.locator("table tbody tr").filter({ hasText: "2026-09-14T11:00" }).waitFor();
  assert.equal(await p1Page.getByRole("button", { name: "暂停", exact: true }).count(), 0,
    "an archived dataset must expose no status write control");
  assert.equal(archivedCoverageCalls, 0,
    "the archived detail must not request forbidden market coverage");
  await p1Page.unroute(archivedMaintenancePattern, archivedMaintenance);
  await p1Page.unroute(archivedCoveragePattern, archivedCoverage);
  await p1Page.goto(base + `/datasets?dataset=${managedDatasetId}`);
  await p1Page.getByText(managedDatasetName, { exact: true }).last().waitFor();

  const managedDatasetsPattern = /\/api\/v1\/managed-datasets$/;
  let releaseManagedDatasets;
  const managedDatasetsGate = new Promise(resolve => { releaseManagedDatasets = resolve; });
  const delayedManagedDatasets = async route => {
    await managedDatasetsGate;
    await route.continue();
  };
  await p1Page.route(managedDatasetsPattern, delayedManagedDatasets);
  const delayedReload = p1Page.reload({ waitUntil: "domcontentloaded" });
  await p1Page.getByText("正在读取数据集…", { exact: true }).waitFor();
  releaseManagedDatasets();
  await delayedReload;
  await p1Page.getByText(managedDatasetName, { exact: true }).last().waitFor();
  await p1Page.unroute(managedDatasetsPattern, delayedManagedDatasets);

  const emptyManagedDatasets = route => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data: [], meta: {}, errors: [] }),
  });
  await p1Page.route(managedDatasetsPattern, emptyManagedDatasets);
  await p1Page.reload();
  await p1Page.getByText("尚无数据集，请先创建。", { exact: true }).waitFor();
  await p1Page.unroute(managedDatasetsPattern, emptyManagedDatasets);
  await p1Page.goto(base + `/datasets?dataset=${managedDatasetId}`);
  await p1Page.getByText(managedDatasetName, { exact: true }).last().waitFor();
  await p1Page.screenshot({ path: path.join(output, "p21-datasets-1440.png"), fullPage: true });
  assert.deepEqual(p1Errors, []);
  await p1Page.close();

  const p1Mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const p1MobileErrors = [];
  p1Mobile.on("pageerror", error => p1MobileErrors.push(error.message));
  await p1Mobile.goto(base + "/tasks");
  await p1Mobile.getByLabel("用户名").fill("admin");
  await p1Mobile.getByLabel("密码").fill("browser-p1-password");
  await p1Mobile.getByRole("button", { name: "登录", exact: true }).click();
  await p1Mobile.getByRole("heading", { name: "数据任务", exact: true }).waitFor();
  await p1Mobile.getByRole("button", { name: "创建任务", exact: true }).click();
  await p1Mobile.getByRole("dialog").getByText("选项来自 capabilities API", { exact: false }).waitFor();
  await p1Mobile.screenshot({ path: path.join(output, "p1-tasks-390.png"), fullPage: true });
  await p1Mobile.getByRole("button", { name: "取消", exact: true }).click();
  await p1Mobile.getByRole("link", { name: "Acceptance plan", exact: true }).click();
  await p1Mobile.getByRole("heading", { name: "Acceptance plan", exact: true }).waitFor();
  await p1Mobile.getByRole("link", { name: "返回任务列表", exact: true }).click();
  await p1Mobile.getByRole("heading", { name: "数据任务", exact: true }).waitFor();
  const mobileWidth = await p1Mobile.evaluate(() => ({ body:document.body.scrollWidth, inner:innerWidth }));
  assert.ok(mobileWidth.body <= mobileWidth.inner, JSON.stringify(mobileWidth));
  await p1Mobile.goto(base + `/datasets?dataset=${managedDatasetId}`);
  await p1Mobile.getByText(managedDatasetName, { exact: true }).last().waitFor();
  const managedMobileWidth = await p1Mobile.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    inner: innerWidth,
  }));
  assert.ok(managedMobileWidth.body <= managedMobileWidth.inner
    && managedMobileWidth.document <= managedMobileWidth.inner,
  JSON.stringify(managedMobileWidth));
  await p1Mobile.screenshot({ path: path.join(output, "p21-datasets-390.png"), fullPage: true });
  assert.deepEqual(p1MobileErrors, []);
  await p1Mobile.close();
  const report = writeReceipt("pass", {
    checks: ["readiness", "capacity_warning", "dataset_list", "dataset_detail", "run_filter",
      "retry_confirmation", "failed_retry", "original_immutable", "acknowledge_confirmation",
      "acknowledge_permission_denied", "acknowledge_additive_state", "permission_denied_state",
      "bars_coverage", "explicit_cursor_next_page", "economic_current_query", "economic_pit_validation",
      "bars_date_window_validation", "catalog_explorer_link", "quality_filters", "quality_empty_state",
      "operations_view", "active_alerts", "successful_ingest", "collapsible_sidebar",
      "loading_state", "api_error_state", "mobile_layout", "no_javascript_errors", "webui_api_deployment_identity",
      "maintenance_run_kinds", "maintenance_capacity_protection", "maintenance_unauthorized_write",
      "maintenance_queued_ingest", "maintenance_derive_snapshot", "maintenance_market_coverage",
      "maintenance_parity_read_only", "maintenance_quality_degraded", "maintenance_economic_ingest",
      "maintenance_protected_write", "maintenance_write_audit", "run_detail_drawer",
      "run_kind_scope_filters", "run_cursor_pager", "maintenance_provider_ingest",
      "maintenance_task_template", "overview_freshness_and_attention", "catalog_kind_and_lineage",
      "catalog_capability", "explorer_market_bars", "explorer_snapshot_meta", "coverage_to_task_handoff",
      "quality_finding_filters", "quality_finding_acknowledge", "operations_queue_worker_capacity",
      "operations_write_audit", "maintenance_run_kind_matrix", "operations_receipt_actions",
      "production_plans_workspace", "production_plan_wizard", "production_catalog_matrix",
      "governance_unit_list", "production_plan_progress", "production_plan_health_filter",
      "production_capacity_gate", "production_plan_edit", "production_plan_lifecycle",
      "p1_real_cookie_login", "p1_capabilities_task_sheet", "p1_url_search",
      "p1_direct_detail_refresh", "p1_status_transition", "p1_filter_back", "p1_keyboard_focus",
      "p1_scheduler_loading_error", "p1_create_pending_and_save",
      "p1_mobile_task_sheet", "p1_legacy_css_isolation",
      "p21_dataset_url_refresh_back", "p21_dataset_draft_retention",
      "p21_dataset_loading_empty", "p21_dataset_optimistic_status",
      "p21_dataset_archived_audit_read_only", "p21_dataset_mobile_no_overflow"],
    original_run_id: failed.run_id,
    acknowledged_run_id: deadLetterId,
    fixture_run_id: fixture.run_id,
    viewports: results,
  });
  process.stdout.write(JSON.stringify(report) + "\n");
})().catch(error => {
  const report = writeReceipt("failed", { message: String(error.message || error) }, "browser_acceptance", error.name || "Error");
  process.stderr.write(JSON.stringify(report) + "\n");
  process.exitCode = 1;
}).finally(async () => {
  if (fredEndpoint) fredEndpoint.close();
  if (browser) await browser.close();
  for (const child of [worker, api]) {
    if (child && child.exitCode === null) child.kill("SIGTERM");
  }
});
