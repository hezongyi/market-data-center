const assert = require("node:assert/strict");
const fs = require("node:fs");
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
    DATACENTER_CAPACITY_WARNING_FREE_RATIO: "0.99",
    DATACENTER_CAPACITY_CRITICAL_FREE_RATIO: "0.005",
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
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base);
    await page.locator(".sidebar .status.ok").waitFor();
    await page.getByText("development", { exact: false }).first().waitFor();
    await page.getByText("Capacity warning", { exact: false }).first().waitFor();

    if (viewport.width === 1440) {
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      await page.locator(".app-shell.shell-collapsed").waitFor();
      await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
      await page.locator(".app-shell:not(.shell-collapsed)").waitFor();
    }

    await page.getByRole("button", { name: "datasets", exact: true }).click();
    await page.getByText("provider_bars", { exact: true }).waitFor();
    await page.getByText("provider_bars", { exact: true }).click();
    await page.getByRole("complementary", { name: "provider_bars" }).waitFor();
    await page.getByRole("button", { name: "Close details", exact: true }).last().click();
    const economicRow = page.locator("tr").filter({ hasText: "economic_observations" });
    await economicRow.getByRole("button", { name: "Open Explorer", exact: true }).click();
    await page.getByRole("button", { name: "Economic", exact: true }).waitFor();
    await page.getByRole("button", { name: "datasets", exact: true }).click();

    await page.getByRole("button", { name: "runs", exact: true }).click();
    await page.getByRole("button", { name: "Session access", exact: true }).click();
    await page.getByLabel("API key").fill(key);
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
      await page.getByLabel("API key").fill("incorrect-key");
      const acknowledgeDialog = page.getByRole("dialog", { name: "Acknowledge dead letter?" });
      await acknowledgeDialog.getByRole("button", { name: "Acknowledge", exact: true }).click();
      await page.locator(".notice").filter({ hasText: "invalid api key" }).waitFor();
      await acknowledgeDialog.getByRole("button", { name: "Cancel", exact: true }).click();
      await page.getByLabel("API key").fill(key);
      await deadLetterRow.getByRole("button", { name: "Acknowledge", exact: true }).click();
      await page.getByRole("dialog", { name: "Acknowledge dead letter?" }).getByRole("button", { name: "Acknowledge", exact: true }).click();
      await page.locator(".notice").filter({ hasText: `Acknowledged ${deadLetterId}` }).waitFor();
      const acknowledged = await call("GET", "/runs/" + deadLetterId);
      assert.equal(acknowledged.status, "dead_letter");
      assert.equal(acknowledged.error, "acceptance dead letter");
      assert.equal(acknowledged.dead_letter_state.state, "acknowledged");
    }

    await page.getByRole("button", { name: "explorer", exact: true }).click();
    await page.getByRole("button", { name: "Provider bars", exact: true }).click();
    await page.getByLabel("Provider").fill("fixture");
    await page.getByLabel("Symbol").fill("UI_TEST");
    await page.getByLabel("Query start date").fill("2026-01-03");
    await page.getByLabel("Query end date").fill("2026-01-01");
    await page.getByLabel("Page size").selectOption("2");
    await page.getByRole("button", { name: "Load coverage", exact: true }).click();
    await page.getByText("Start date must not be after end date.", { exact: true }).waitFor();
    await page.getByLabel("Query start date").fill("");
    await page.getByLabel("Query end date").fill("");
    await page.getByRole("button", { name: "Load coverage", exact: true }).click();
    await page.locator(".coverage-strip").waitFor();
    await page.getByText("Page 1 · 2 rows", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Next page", exact: true }).click();
    await page.getByText(/Page 2 · \d+ rows/, { exact: false }).waitFor();
    await page.getByRole("button", { name: "Economic", exact: true }).click();
    await page.getByLabel("Series ID").fill("PAYEMS");
    await page.getByRole("button", { name: "Load observations", exact: true }).click();
    await page.getByText("No observations in this page.", { exact: true }).waitFor();
    await page.getByLabel("Query mode").selectOption("pit");
    await page.getByLabel("As-of timestamp").fill("");
    await page.getByRole("button", { name: "Load observations", exact: true }).click();
    await page.getByText("PIT mode requires an as-of timestamp.", { exact: true }).waitFor();

    await page.getByRole("button", { name: "quality", exact: true }).click();
    await page.getByLabel("Finding code").waitFor();
    await page.getByLabel("Finding from date").waitFor();
    await page.getByText("No quality findings.", { exact: true }).waitFor();

    await page.getByRole("button", { name: "operations", exact: true }).click();
    await page.getByText("Capacity and recovery", { exact: true }).waitFor();
    await page.getByText("Active alerts", { exact: true }).waitFor();
    await page.getByLabel("API key").fill("incorrect-key");
    await page.getByRole("button", { name: "Review ingest", exact: true }).click();
    const ingestDialog = page.getByRole("dialog", { name: "Queue ingest run?" });
    await ingestDialog.getByRole("button", { name: "Queue ingest", exact: true }).click();
    await page.locator(".notice").filter({ hasText: "invalid api key" }).waitFor();
    await ingestDialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByLabel("API key").fill(key);
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
  await loadingPage.goto(base);
  await loadingPage.getByLabel("Loading").first().waitFor();
  await loadingPage.locator(".sidebar .status.ok").waitFor();
  await loadingPage.close();

  const errorPage = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  await errorPage.route("**/api/v1/metrics", route => route.fulfill({
    status: 500,
    contentType: "application/json",
    body: JSON.stringify({ data: null, meta: { request_id: "browser-error-state" }, errors: [{ code: "injected", message: "Injected metrics failure" }] }),
  }));
  await errorPage.goto(base);
  await errorPage.getByText("Unable to load data", { exact: true }).waitFor();
  await errorPage.getByText("Injected metrics failure", { exact: false }).waitFor();
  await errorPage.close();
  const report = writeReceipt("pass", {
    checks: ["readiness", "capacity_warning", "dataset_list", "dataset_detail", "run_filter",
      "retry_confirmation", "failed_retry", "original_immutable", "acknowledge_confirmation",
      "acknowledge_permission_denied", "acknowledge_additive_state", "permission_denied_state",
      "bars_coverage", "explicit_cursor_next_page", "economic_current_query", "economic_pit_validation",
      "bars_date_window_validation", "catalog_explorer_link", "quality_filters", "quality_empty_state",
      "operations_view", "active_alerts", "successful_ingest", "collapsible_sidebar",
      "loading_state", "api_error_state", "mobile_layout", "no_javascript_errors", "webui_api_deployment_identity"],
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
  if (browser) await browser.close();
  for (const child of [worker, api]) {
    if (child && child.exitCode === null) child.kill("SIGTERM");
  }
});
