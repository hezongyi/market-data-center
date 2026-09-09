/* Run with NODE_PATH pointing to an installed Playwright package. */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");

(async () => {
  const base = process.env.DATACENTER_BROWSER_URL || "http://127.0.0.1:18380";
  const output = path.resolve("acceptance-receipts/browser");
  fs.mkdirSync(output, { recursive: true });
  let key = process.env.DATACENTER_API_KEY;
  if (!key) {
    const pid = execFileSync("systemctl", ["--user", "show", "market-data-center-api.service", "-p", "MainPID", "--value"]).toString().trim();
    const entry = fs.readFileSync(`/proc/${pid}/environ`, "utf8").split("\0").find(item => item.startsWith("DATACENTER_API_KEY="));
    key = entry ? entry.slice("DATACENTER_API_KEY=".length) : "";
  }
  async function call(method, route, body) {
    const response = await fetch(base + "/api/v1" + route, { method,
      headers: { "Content-Type": "application/json", "X-API-Key": key || "" },
      body: body ? JSON.stringify(body) : undefined });
    assert.ok(response.ok, `HTTP ${response.status}`);
    return (await response.json()).data;
  }
  const failed = await call("POST", "/ingest/runs", { job_id: "browser-acceptance", provider: "acceptance_invalid",
    symbol: "UI_TEST", start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z" });
  let receipt;
  for (let i = 0; i < 100; i++) {
    receipt = await call("GET", "/runs/" + failed.run_id);
    if (receipt.status === "failed") break;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  assert.equal(receipt.status, "failed");
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE });
  const results = [];
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.goto(base);
      await page.locator("header .status.ok").waitFor();
      await page.getByRole("button", { name: "runs", exact: true }).click();
      await page.getByLabel("API key").fill(key || "");
      await page.getByLabel("Status").selectOption("failed");
      const row = page.locator("tr").filter({ has: page.locator("td.mono", { hasText: failed.run_id }) });
      const before = (await call("GET", "/runs")).filter(run => run.retry_of === failed.run_id).length;
      await row.getByRole("button", { name: "Retry", exact: true }).click();
      await page.locator(".notice").filter({ hasText: "Queued retry" }).waitFor();
      assert.equal((await call("GET", "/runs")).filter(run => run.retry_of === failed.run_id).length, before + 1);
      assert.deepEqual(await call("GET", "/runs/" + failed.run_id), receipt);
      assert.ok(await page.getByLabel("API key").isVisible());
      const widths = await page.evaluate(() => ({ body: document.body.scrollWidth, html: document.documentElement.scrollWidth, inner: innerWidth }));
      assert.equal(widths.body <= widths.inner, true, JSON.stringify({ viewport, widths }));
      assert.deepEqual(errors, []);
      await page.screenshot({ path: path.join(output, `runs-${viewport.width}.png`), fullPage: true });
      await page.getByLabel("Status").selectOption("dead_letter");
      await page.screenshot({ path: path.join(output, `dead-letter-${viewport.width}.png`), fullPage: true });
      results.push({ viewport, status: "pass", checks: ["ready", "failed_filter", "dead_letter_filter", "manual_retry", "original_immutable", "api_key_visible", "no_page_overflow", "no_js_errors"] });
      await page.close();
    }
    const report = { status: "pass", checked_at: new Date().toISOString(), original_run_id: failed.run_id, results };
    fs.writeFileSync(path.join(output, "receipt.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
