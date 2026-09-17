const fs = require("node:fs");

// Keep recovery metadata whenever stopping cannot be confirmed.
async function cleanupPreview({ browser, base, metadataPath, stop }) {
  const errors = [];
  try { if (browser) await browser.close(); }
  catch (error) { errors.push(error); }
  let stopped = false;
  try {
    if (!fs.existsSync(metadataPath)) throw new Error(`preview metadata unavailable; retained ${base}`);
    const state = stop();
    if (state.state !== "stopped" || Object.values(state.processes || {}).some(item => item.running)) {
      throw new Error(`preview stop not confirmed; retained ${base}`);
    }
    stopped = true;
  } catch (error) { errors.push(error); }
  if (stopped) fs.rmSync(base, { recursive: true, force: true });
  if (errors.length) throw new AggregateError(errors, errors.map(error => error.message).join("; "));
}

module.exports = { cleanupPreview };
