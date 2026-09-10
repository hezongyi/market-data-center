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
    DATACENTER_CAPACITY_WARNING_FREE_RATIO: "0.01",
    DATACENTER_CAPACITY_CRITICAL_FREE_RATIO: "0.005",
  };
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
    job_id: "unauthorized", symbol: "UI_TEST", start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z",
  }, false);
  assert.equal(unauthorized.response.status, 401);

  const fixture = await call("POST", "/ingest/runs", {
    job_id: "browser-bars", provider: "fixture", symbol: "UI_TEST", asset_class: "test",
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
    job_id: "browser-failure", provider: "acceptance_invalid", symbol: "UI_TEST",
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
    await page.locator("header .status.ok").waitFor();

    await page.getByRole("button", { name: "datasets", exact: true }).click();
    await page.getByText("provider_bars", { exact: true }).waitFor();

    await page.getByRole("button", { name: "runs", exact: true }).click();
    await page.getByLabel("API key").fill(key);
    await page.getByLabel("Status").selectOption("failed");
    const row = page.locator("tr").filter({ has: page.locator("td.mono", { hasText: failed.run_id }) });
    await row.waitFor();
    const before = (await call("GET", "/runs")).filter(run => run.retry_of === failed.run_id).length;
    await row.getByRole("button", { name: "Retry", exact: true }).click();
    await page.locator(".notice").filter({ hasText: "Queued retry" }).waitFor();
    assert.equal((await call("GET", "/runs")).filter(run => run.retry_of === failed.run_id).length, before + 1);
    assert.deepEqual(await call("GET", "/runs/" + failed.run_id), failedReceipt);

    await page.getByRole("button", { name: "explorer", exact: true }).click();
    await page.getByLabel("Provider").fill("fixture");
    await page.getByLabel("Symbol").fill("UI_TEST");
    await page.getByRole("button", { name: "Load coverage", exact: true }).click();
    await page.getByText(String(coverage.row_count), { exact: true }).waitFor();

    const widths = await page.evaluate(() => ({
      body: document.body.scrollWidth, html: document.documentElement.scrollWidth, inner: innerWidth,
    }));
    assert.equal(widths.body <= widths.inner && widths.html <= widths.inner, true, JSON.stringify({ viewport, widths }));
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, `acceptance-${viewport.width}.png`), fullPage: true });
    results.push({ viewport, status: "pass" });
    await page.close();
  }
  const report = writeReceipt("pass", {
    checks: ["readiness", "dataset_list", "run_filter", "failed_retry", "original_immutable",
      "unauthorized_write", "bars_coverage", "mobile_layout", "no_javascript_errors"],
    original_run_id: failed.run_id,
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
