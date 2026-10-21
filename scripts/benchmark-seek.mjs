#!/usr/bin/env node
/**
 * Local browser media benchmark. Requires playwright-core and installed Chrome.
 * Uses existing authenticated READ endpoints; never saves annotations or images.
 *
 * SSI_PLAYWRIGHT_MODULE=/tmp/bench/node_modules/playwright-core/index.mjs \
 * node scripts/benchmark-seek.mjs --origin http://127.0.0.1:8002 \
 *   --case CASE_ID --session-file /tmp/private-session.json --mode video \
 *   --frames 54000,108000,24000,84000 --mbps 20 --latency 80 --output /tmp/video.json
 *
 * Session JSON: {"token":"ssi_session cookie value"}; keep this file private.
 * --mode image measures first access, server disk reuse (fresh HTTP requests),
 * and decoded image reuse separately. First access is only server-cold if the
 * endpoint reports MISS; background warming may already have filled the cache.
 * HTTP cache is disabled for both modes to isolate server vs decoded reuse.
 * Use a separate data directory to avoid warming a production cache.
 * Frame numbers are zero-based. Loopback by default; no images leave the server.
 */
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const args = Object.fromEntries(process.argv.slice(2).reduce((rows, value, i, all) => {
  if (value.startsWith("--")) rows.push([value.slice(2), all[i + 1]]);
  return rows;
}, []));
if (!args.case || !args["session-file"] || !["video", "image"].includes(args.mode)) {
  console.error("Required: --case ID --session-file FILE --mode video|image; optional: --origin URL --frames 100,101,102 --mbps 20 --latency 80 --output FILE --chrome PATH");
  process.exit(2);
}
const origin = new URL(args.origin ?? "http://127.0.0.1:8002").origin;
const frames = (args.frames ?? "300,301,302,303,304").split(",").map(Number);
if (frames.some(n => !Number.isSafeInteger(n) || n < 0)) throw new Error("Invalid frame list");
const session = JSON.parse(fs.readFileSync(args["session-file"], "utf8"));
const moduleName = process.env.SSI_PLAYWRIGHT_MODULE ?? "playwright-core";
const { chromium } = await import(moduleName.startsWith("/") ? pathToFileURL(moduleName).href : moduleName);
const mbps = Number(args.mbps ?? 0), latency = Number(args.latency ?? 0);
const browser = await chromium.launch({ executablePath: args.chrome ?? "/usr/bin/google-chrome", headless: true,
  args: ["--disable-dev-shm-usage", ...(process.getuid?.() === 0 ? ["--no-sandbox"] : [])] });
const result = {
  timestamp: new Date().toISOString(), browser: await browser.version(), mode: args.mode,
  environment: { mbps, latencyMs: latency, httpCacheDisabled: true, cpuThrottle: false },
  methodology: "Media-level benchmark, not full app interaction. Video currentTime to seeked + two animation frames; image fetch + decode + two animation frames. CDP encoded bytes are approximate window deltas (video can download ahead). OS file cache is not flushed. Decoded reuse is a harness cache, not an app LRU integration test.",
  rows: [],
};

function summary(rows) {
  const values = rows.map(r => r.elapsedMs).sort((a, b) => a - b);
  return { n: values.length, medianMs: values.length % 2 ? values[(values.length - 1) / 2] : (values[values.length / 2 - 1] + values[values.length / 2]) / 2,
    p95Ms: values[Math.max(0, Math.ceil(values.length * .95) - 1)],
    transferredBytes: rows.reduce((sum, r) => sum + r.transferredBytes, 0),
    cacheResponses: Object.fromEntries([...new Set(rows.map(r => r.serverCache ?? "video"))].map(value => [value, rows.filter(r => (r.serverCache ?? "video") === value).length])) };
}

try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([{ name: "ssi_session", value: session.token, url: origin }]);
  const page = await context.newPage();
  await page.route("**/__seek_benchmark", route => route.fulfill({ contentType: "text/html",
    body: '<!doctype html><html><body style="margin:0"><video muted playsinline preload="auto" style="max-width:100%;max-height:90vh"></video><img style="max-width:100%;max-height:90vh"></body></html>' }));
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
  await cdp.send("Network.emulateNetworkConditions", { offline: false, latency, downloadThroughput: mbps ? mbps * 1e6 / 8 : -1, uploadThroughput: -1 });
  let transferred = 0;
  cdp.on("Network.dataReceived", e => { transferred += e.encodedDataLength; });
  await page.goto(origin + "/__seek_benchmark");
  const times = await page.evaluate(async caseId => {
    const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/frames`);
    if (!response.ok) throw new Error(`Frame metadata: HTTP ${response.status}`);
    return response.json();
  }, args.case);
  if (frames.some(frame => frame >= times.length)) throw new Error(`Frame exceeds case length (${times.length})`);
  if (args.mode === "video") {
    await page.locator("video").evaluate((video, caseId) => { video.src = `/api/cases/${encodeURIComponent(caseId)}/video`; }, args.case);
    await page.waitForFunction(() => document.querySelector("video").readyState >= 2, null, { timeout: 60_000 });
  } else {
    await page.locator("video").evaluate(video => video.remove());
    await page.evaluate(() => { window.benchmarkImages = new Map(); });
  }
  const phases = args.mode === "video" ? ["first_pass", "repeat"] : ["first_access", "server_disk_reuse", "decoded_reuse"];
  for (const phase of phases) {
    for (const frame of frames) {
      const bytesBefore = transferred;
      const timing = await page.evaluate(async ({ mode, phase, frame, caseId, seconds }) => {
        const start = performance.now();
        const paint = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        if (mode === "video") {
          const video = document.querySelector("video");
          const ready = new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("Video seek timed out")), 30_000);
            video.addEventListener("seeked", () => { clearTimeout(timer); resolve(); }, { once: true });
          });
          video.currentTime = seconds;
          await ready; await paint();
          return { elapsedMs: performance.now() - start, currentTime: video.currentTime };
        }
        let cached = window.benchmarkImages.get(frame), serverCache = "decoded", responseBytes = 0, fetchMs = 0, decodeMs = 0;
        if (phase !== "decoded_reuse" || !cached) {
          const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/frame/${frame}.jpg`);
          if (!response.ok) throw new Error(`Frame ${frame}: HTTP ${response.status}`);
          serverCache = response.headers.get("X-Frame-Cache") ?? "unspecified";
          const blob = await response.blob(); responseBytes = blob.size; fetchMs = performance.now() - start;
          const image = new Image(); image.src = URL.createObjectURL(blob);
          const decodeStart = performance.now(); await image.decode(); decodeMs = performance.now() - decodeStart;
          if (cached) URL.revokeObjectURL(cached.src);
          window.benchmarkImages.set(frame, image); cached = image;
        }
        cached.style.cssText = "max-width:100%;max-height:90vh";
        document.querySelector("img").replaceWith(cached); await paint();
        return { elapsedMs: performance.now() - start, fetchMs, decodeMs, serverCache, responseBytes };
      }, { mode: args.mode, phase, frame, caseId: args.case,
        seconds: (times[frame] + (times[frame + 1] ?? times[frame] + 1000 / 30)) / 2000 });
      const row = { phase, frame, ...timing, transferredBytes: transferred - bytesBefore };
      result.rows.push(row); console.log(JSON.stringify(row));
    }
  }
  result.summary = Object.fromEntries(phases.map(phase => [phase, summary(result.rows.filter(r => r.phase === phase))]));
  console.log(JSON.stringify(result.summary, null, 2));
} finally {
  if (args.output) fs.writeFileSync(args.output, JSON.stringify(result, null, 2) + "\n");
  await browser.close();
}
