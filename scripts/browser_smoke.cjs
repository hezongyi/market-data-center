// Focused, isolated route smoke. Full product journeys remain in browser_acceptance.cjs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { cleanupPreview } = require("./preview_cleanup.cjs");
const repo = path.resolve(__dirname, "..");
const { chromium } = require(path.join(repo, "webui/node_modules/playwright"));
const python = process.env.DATACENTER_PYTHON || path.join(repo, ".venv/bin/python");
const routes = process.argv.slice(2);
assert.ok(routes.length && routes.every(route => /^\/(?!\/)[^:\\]*$/.test(route)), "local routes required");
const base = fs.mkdtempSync(path.join(os.tmpdir(), "mdc-route-smoke-"));
const output = path.join(repo, "acceptance-receipts/browser-smoke", path.basename(base));
fs.mkdirSync(output, { recursive: true });
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  !/^(DATACENTER_|DUKASCOPY_|FRED_|BINANCE_|YFINANCE_)/.test(key)));
const preview = action => JSON.parse(execFileSync(python, [
  "scripts/dev_preview.py", action, "--id", "smoke", "--base", base, "--python", python, "--json",
], { cwd: repo, env, encoding: "utf8", timeout: 180000 }));
let browser;
const checks = [];
(async () => {
  try {
    const state = preview("start");
    assert.equal(state.state, "running");
    browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_BROWSER_EXECUTABLE || undefined });
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const credentials = { username: "admin", password: "isolated-smoke-password" };
      if (width === 1440) {
        const response = await context.request.post(state.ui_url + "/api/v1/auth/initialize", {
          data: credentials, headers: { Origin: state.ui_url },
        });
        assert.equal(response.status(), 200);
      }
      const login = await context.request.post(state.ui_url + "/api/v1/auth/login", {
        data: credentials, headers: { Origin: state.ui_url },
      });
      assert.equal(login.status(), 200);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("response", response => {
        if (response.url().startsWith(state.ui_url) && response.status() >= 500) errors.push(`HTTP ${response.status()}: ${response.url()}`);
      });
      for (const route of routes) {
        const start = Date.now();
        const response = await page.goto(state.ui_url + route);
        assert.equal(response.status(), 200);
        await page.locator("main h1").first().waitFor();
        const title = await page.locator("main h1").first().innerText();
        assert.ok(title.trim() && !/404|not found|登录/i.test(title), `unexpected page: ${title}`);
        await page.reload();
        await page.locator("main h1").first().waitFor();
        assert.equal(new URL(page.url()).pathname, new URL(route, state.ui_url).pathname);
        const widths = await page.evaluate(() => ({ body: document.body.scrollWidth, html: document.documentElement.scrollWidth, inner: innerWidth }));
        assert.ok(widths.body <= widths.inner && widths.html <= widths.inner, JSON.stringify(widths));
        assert.deepEqual(errors, []);
        const screenshot = path.join(output, `${width}-${route.replace(/[^a-z0-9]/gi, "_")}.png`);
        await page.screenshot({ path: screenshot, fullPage: true });
        checks.push({ route, width, title, result: "pass", duration_seconds: (Date.now() - start) / 1000, screenshot });
      }
      await context.close();
    }
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ result: "pass", source: state.actual_identity, checks }, null, 2));
    console.log(`browser-smoke: pass (${checks.length} route/viewport checks); evidence: ${output}`);
  } finally {
    await cleanupPreview({ browser, base, metadataPath: path.join(base, "smoke/preview.json"), stop: () => preview("stop") });
  }
})().catch(error => {
  fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ result: "failed", error: error.message, checks }, null, 2));
  console.error(error);
  process.exitCode = 1;
});
