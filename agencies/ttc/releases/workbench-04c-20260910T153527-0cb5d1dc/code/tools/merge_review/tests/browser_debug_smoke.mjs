/** Real single-day debug interactions. Requires a disposable server on port 8768.
 * node tools/merge_review/tests/browser_debug_smoke.mjs --allow-test-writes
 * No production review store, external resources, dependencies, or raw writes.
 */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const appUrl = new URL(option('--url', 'http://127.0.0.1:8768'));
const readOnly = args.includes('--read-only');
if (appUrl.origin !== 'http://127.0.0.1:8768' || (!readOnly && !args.includes('--allow-test-writes'))) {
  throw new Error('Only explicitly disposable http://127.0.0.1:8768 with --allow-test-writes is permitted.');
}
const output = resolve(option('--output', `artifacts/merge-debug-20231106/browser-acceptance${readOnly ? '/final-readonly' : ''}`));
await mkdir(output, { recursive: true, mode: 0o700 });
await chmod(output, 0o700);
const profile = await mkdtemp(join(tmpdir(), 'merge-debug-chrome-'));
const checks = [], consoleErrors = [], runtimeErrors = [], externalRequests = [], responseErrors = [], dialogs = [], requests = [];
let cdp, browser, failure = null, summary = {}, dialogAccept = false;
const startedAt = new Date().toISOString();
const assert = (condition, name, details) => {
  checks.push({ name, passed: Boolean(condition), ...(details === undefined ? {} : { details }) });
  if (!condition) throw new Error(`Acceptance check failed: ${name}`);
};

class CDP {
  constructor(socket) {
    this.socket = socket; this.id = 0; this.pending = new Map(); this.handlers = new Map();
    socket.addEventListener('message', ({ data }) => {
      const message = JSON.parse(data);
      if (message.id) {
        const pending = this.pending.get(message.id); if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result);
      } else for (const handler of this.handlers.get(message.method) || []) {
        Promise.resolve(handler(message.params)).catch(error => runtimeErrors.push(String(error)));
      }
    });
  }
  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((yes, no) => { socket.addEventListener('open', yes, { once: true }); socket.addEventListener('error', no, { once: true }); });
    return new CDP(socket);
  }
  on(method, fn) { this.handlers.set(method, [...(this.handlers.get(method) || []), fn]); }
  send(method, params = {}) {
    const id = ++this.id;
    const result = new Promise((resolveResponse, reject) => this.pending.set(id, { resolve: resolveResponse, reject }));
    this.socket.send(JSON.stringify({ id, method, params })); return result;
  }
  async evaluate(expression) {
    const value = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (value.exceptionDetails) throw new Error(JSON.stringify(value.exceptionDetails));
    return value.result.value;
  }
}

async function waitFor(expression, timeout = 45000) {
  const start = Date.now(); let lastProgress = start;
  while (Date.now() - start < timeout) {
    const value = await cdp.evaluate(expression); if (value) return value;
    if (Date.now() - lastProgress > 30000) {
      console.log(JSON.stringify({ waiting_seconds: Math.round((Date.now() - start) / 1000), job: await cdp.evaluate("document.querySelector('#debug-job-message')?.textContent") }));
      lastProgress = Date.now();
    }
    await delay(150);
  }
  throw new Error(`Timeout waiting for ${expression}`);
}
async function click(selector) {
  assert(await cdp.evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e || e.disabled) return false; if(e.click) e.click(); else e.dispatchEvent(new MouseEvent('click',{bubbles:true})); return true; })()`), `click ${selector}`);
}
async function setValue(selector, value) {
  assert(await cdp.evaluate(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); if(!e)return false; e.value=${JSON.stringify(String(value))}; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); return true; })()`), `set ${selector}`);
}
async function read(path) {
  const response = await fetch(new URL(path, appUrl));
  assert(response.ok, `GET ${path.split('?')[0]}`); return response.json();
}
async function screenshot(name, width = 1500, height = 1050, mobile = false) {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile });
  await delay(250);
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false, fromSurface: true });
  await writeFile(join(output, name), Buffer.from(shot.data, 'base64'), { mode: 0o600 });
}
async function ready() { await waitFor("!document.querySelector('#case-workspace').hidden && document.querySelector('#case-loading').hidden && document.querySelector('#apc-table-body tr[data-rid]')"); }
async function currentCase() { return cdp.evaluate("document.querySelector('#case-list .case-item.active')?.dataset.caseId"); }
async function currentRun() { return cdp.evaluate("document.querySelector('#debug-run-select').value"); }
async function chooseCase(id) {
  await setValue('#case-search', id);
  await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId === ${JSON.stringify(id)} && document.querySelector('#case-loading').hidden`);
}
async function chooseRun(id) {
  await setValue('#debug-run-select', id);
  await waitFor(`document.querySelector('#debug-run-select').value === ${JSON.stringify(id)} && document.querySelector('#run-label').textContent.includes(${JSON.stringify(id)}) && document.querySelector('#case-loading').hidden`);
  await ready();
}

async function startBrowser() {
  browser = spawn(option('--chrome', 'google-chrome'), [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking',
    '--disable-component-update', '--disable-domain-reliability', '--disable-extensions', '--disable-sync',
    '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', '--metrics-recording-only',
    '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let log = '';
  const websocket = await new Promise((yes, no) => {
    const timer = setTimeout(() => no(new Error(`Chrome startup timeout: ${log.slice(-2000)}`)), 15000);
    browser.stderr.on('data', value => { log += value; const found = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (found) { clearTimeout(timer); yes(found[1]); } });
    browser.on('error', error => { clearTimeout(timer); no(error); });
  });
  const origin = websocket.replace(/^ws:/, 'http:').split('/devtools/')[0];
  const targets = await fetch(`${origin}/json/list`).then(response => response.json());
  cdp = await CDP.connect(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  cdp.on('Runtime.consoleAPICalled', ({ type, args: values }) => { if (type === 'error') consoleErrors.push(values.map(value => value.value ?? value.description).join(' ')); });
  cdp.on('Runtime.exceptionThrown', value => runtimeErrors.push(value));
  cdp.on('Page.javascriptDialogOpening', async value => { dialogs.push({ ...value, accepted: dialogAccept }); await cdp.send('Page.handleJavaScriptDialog', { accept: dialogAccept }); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({ url: request.url, method: request.method, postData: request.url.endsWith('/api/debug/runs') ? request.postData : undefined }); if (/^https?:/.test(request.url) && new URL(request.url).origin !== appUrl.origin) externalRequests.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if (response.status >= 400) responseErrors.push({ url: response.url, status: response.status }); });
  cdp.on('Fetch.requestPaused', async ({ requestId, request }) => {
    const allowed = /^(data:|blob:|about:)/.test(request.url) || (/^https?:/.test(request.url) && new URL(request.url).origin === appUrl.origin);
    await cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', { requestId, ...(allowed ? {} : { errorReason: 'BlockedByClient' }) });
  });
  for (const method of ['Runtime.enable', 'Network.enable', 'Page.enable']) await cdp.send(method);
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1050, deviceScaleFactor: 1, mobile: false });
}

async function runChecks() {
  const debug = await read('/api/debug');
  assert(debug.enabled && debug.date === '2023-11-06', 'debug date is fixed to 2023-11-06');
  assert(debug.runs.length > 0, 'at least one completed baseline exists');
  await cdp.send('Page.navigate', { url: appUrl.href }); await ready();
  assert(await cdp.evaluate("!document.querySelector('#debug-console').hidden && document.querySelector('#debug-date').textContent==='2023-11-06'"), 'single-day experiment controls visible');
  const baseline = await currentRun();
  const meta = await read(`/api/meta?run=${encodeURIComponent(baseline)}`);
  assert(meta.methods.length === 1 && meta.debug_date === '2023-11-06', 'run metadata advertises only its actual method and debug date');
  const vehicle = await cdp.evaluate("document.querySelector('#vehicle-select').value");
  const params = new URLSearchParams({ run: baseline, method: meta.default_method, vehicle });
  const catalog = await read(`/api/catalog?${params}`);
  const moderate = catalog.cases.find(row => row.kind === 'segment' && row.apc_count >= 5 && row.apc_count <= 100 && row.avl_member_count >= 20)
    ?? catalog.cases.find(row => row.kind === 'segment' && row.apc_count >= 5);
  assert(Boolean(moderate), 'real baseline contains reviewable APC and AVL'); await chooseCase(moderate.id);
  await setValue('#window-size', 'full'); await delay(150);
  const linked = await cdp.evaluate(`(() => { const sets=['map-svg','timeline-svg','progress-svg'].map(id=>new Set([...document.querySelectorAll('#'+id+' [data-rid]')].map(e=>e.dataset.rid))); return [...sets[2]].filter(id=>sets.every(s=>s.has(id))); })()`);
  assert(linked.length > 0, 'baseline has genuine APC evidence linked across all three views');
  const anchor = linked[0];
  for (const view of ['map-svg', 'timeline-svg', 'progress-svg']) {
    await click(`#${view} [data-rid="${anchor}"]`);
    assert(await cdp.evaluate(`document.querySelector('#apc-table-body tr.selected')?.dataset.rid===${JSON.stringify(anchor)}`), `${view} selects source APC row`);
    assert(await cdp.evaluate(`['map-svg','timeline-svg','progress-svg'].every(id=>[...document.querySelectorAll('#'+id+' [data-selected="true"]')].some(e=>e.dataset.rid===${JSON.stringify(anchor)}))`), `${view} linked selection is visible in all three views`);
  }
  params.set('case_id', moderate.id);
  const detail = await read(`/api/case?${params}`);
  const source = detail.apc.find(row => row.rid === anchor);
  assert(source && Object.hasOwn(source, 'branch') && Object.hasOwn(source, 'route_name'), 'APC payload exposes Branch and RouteName separately');
  assert(await cdp.evaluate("document.querySelector('#apc-table-body').closest('table').querySelector('thead').textContent.includes('Branch')"), 'table distinguishes Route / RouteName / Branch / direction');
  for (const [field, value] of Object.entries({ branch: source.branch, route_name: source.route_name })) {
    if (value != null && String(value)) assert(await cdp.evaluate(`document.querySelector('#apc-table-body tr.selected').textContent.includes(${JSON.stringify(String(value))})`), `${field} original value is displayed`);
  }
  assert(detail.window.start >= meta.scope.start && detail.window.end <= meta.scope.end, 'detail timeline is clipped to the debug date');
  assert(detail.warnings.some(value => value.includes('data-scope boundaries')), 'day edges explicitly mean data truncation');
  await screenshot('baseline-desktop.png');
  await setValue('#case-search', '');
  await waitFor("document.querySelectorAll('#case-list .case-item').length>1");

  const baselineNote = `Disposable debug-browser baseline ${startedAt}: <b>literal text</b>`;
  await setValue('#review-notes', baselineNote); await click('#review-verdict input[value="uncertain"]');
  const previousCase = await currentCase();
  await click('#next-case'); await delay(100);
  assert(await currentCase() === previousCase && await cdp.evaluate("document.querySelector('#review-notes').value") === baselineNote, 'unsaved notes block ordinary case navigation when discard is declined');
  await setValue('#debug-label', `Browser acceptance ${startedAt}`);
  const numericChange = async (group, key, desired) => {
    const spec = debug.parameters[group].find(value => value.key === key);
    assert(Boolean(spec), `parameter ${group}.${key} is editable`);
    const current = Number(await cdp.evaluate(`document.querySelector('#debug-${group}-${key}').value`));
    let value = Math.max(spec.min ?? -Infinity, Math.min(spec.max ?? Infinity, desired));
    if (value === current) value = Math.max(spec.min ?? -Infinity, Math.min(spec.max ?? Infinity, current + (spec.step || 1)));
    assert(value !== current, `parameter ${group}.${key} actually changes`);
    await setValue(`#debug-${group}-${key}`, value); return value;
  };
  const gap = await numericChange('segmentation', 'gap_seconds', 20);
  const radius = await numericChange('merge', 'radius_m', 30);
  await click('#debug-submit');
  await waitFor("!document.querySelector('#debug-job').hidden");
  await waitFor("document.querySelector('#debug-job-title').textContent.includes('重跑完成') || document.querySelector('#debug-job-title').textContent.includes('重跑失败')", 600000);
  assert(await cdp.evaluate("document.querySelector('#debug-job-title').textContent.includes('重跑完成')"), 'parameter change executes a real completed one-day experiment');
  await waitFor("!document.querySelector('#debug-load-ready').hidden");
  assert(await currentRun() === baseline, 'completed experiment does not silently replace dirty baseline review');
  assert(await cdp.evaluate("document.querySelector('#review-notes').value") === baselineNote, 'unsaved note survives background computation');
  const posts = requests.filter(request => request.method === 'POST' && request.url.endsWith('/api/debug/runs'));
  assert(posts.length === 1, 'exactly one experiment submission, no duplicate polling writes');
  const submitted = JSON.parse(posts[0].postData);
  assert(submitted.config.segmentation.gap_seconds === gap && submitted.config.merge.radius_m === radius, 'browser submits the actual edited segmentation and merge parameters');
  await click('#save-review'); await waitFor("!document.querySelector('#review-save-state').classList.contains('dirty')");
  await click('#debug-load-ready');
  await waitFor(`document.querySelector('#debug-run-select').value !== ${JSON.stringify(baseline)} && document.querySelector('#case-loading').hidden`); await ready();
  const newRun = await currentRun();
  assert(await cdp.evaluate("document.querySelector('#apc-table-body tr.selected')?.dataset.rid") === anchor, 'run switch preserves selected source APC rid despite new chunk IDs');
  assert(await cdp.evaluate("document.querySelector('#review-notes').value") !== baselineNote, 'new run does not inherit baseline review note');
  await setValue('#debug-base-select', baseline);
  await waitFor("document.querySelector('#debug-diff-json').textContent.trim().startsWith('{')");
  const diff = await read(`/api/debug/compare?base=${encodeURIComponent(baseline)}&run=${encodeURIComponent(newRun)}&vehicle=${encodeURIComponent(vehicle)}`);
  assert(diff.source_compatible !== false && diff.summary.apc_common > 0, 'comparison uses common raw APC source evidence');
  assert(Array.isArray(diff.changes) && Array.isArray(diff.boundaries.added) && Array.isArray(diff.boundaries.removed), 'source APC decision changes and raw-member boundaries are separate outputs');
  assert(diff.summary.changed_rows > 0, 'edited thresholds produce actual source-row decision differences');
  assert(diff.summary.boundaries_added + diff.summary.boundaries_removed > 0, 'edited segmentation produces actual boundary differences');
  await waitFor("document.querySelector('#debug-boundary-svg').childElementCount>3 && document.querySelector('#debug-diff-list [data-apc-rid]')");
  assert(await cdp.evaluate("document.querySelector('#debug-diff-summary').textContent.includes('状态变化')"), 'UI renders decision and boundary comparison counts');
  await screenshot('rerun-diff-desktop.png');
  await cdp.evaluate("document.querySelector('#debug-diff').scrollIntoView({block:'start'})"); await screenshot('source-rid-boundary-diff.png');
  await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})"); await screenshot('rerun-timeline-progress.png');
  const newCase = await currentCase();
  const newNote = `Disposable debug-browser new run ${startedAt}`;
  await setValue('#review-notes', newNote); await click('#save-review'); await waitFor("!document.querySelector('#review-save-state').classList.contains('dirty')");
  const baselineReviews = await read(`/api/reviews/export?run=${encodeURIComponent(baseline)}`);
  const newReviews = await read(`/api/reviews/export?run=${encodeURIComponent(newRun)}`);
  assert(baselineReviews.reviews.some(row => row.case_id === previousCase && row.notes === baselineNote) && !baselineReviews.reviews.some(row => row.notes === newNote), 'baseline review remains scoped to its original run');
  assert(newReviews.reviews.some(row => row.case_id === newCase && row.notes === newNote) && !newReviews.reviews.some(row => row.notes === baselineNote), 'new review remains scoped to its new run');
  await chooseRun(baseline); await chooseCase(previousCase);
  await waitFor(`document.querySelector('#review-notes').value===${JSON.stringify(baselineNote)}`);
  assert(await cdp.evaluate("document.querySelector('#review-verdict input:checked').value") === 'uncertain', 'switching back restores baseline verdict');
  await chooseRun(newRun); await chooseCase(newCase);
  await cdp.send('Page.reload', { ignoreCache: true }); await ready(); await chooseCase(newCase);
  await waitFor(`document.querySelector('#review-notes').value===${JSON.stringify(newNote)}`);
  assert(true, 'new-run note survives a real page reload');
  await cdp.evaluate('window.scrollTo(0,0)'); await screenshot('mobile-debug.png', 390, 844, true);
  assert(await cdp.evaluate('document.documentElement.scrollWidth<=window.innerWidth+2'), 'mobile debug controls do not overflow the document');
  await cdp.evaluate("document.querySelector('#map-stage').scrollIntoView({block:'start'})"); await screenshot('mobile-map.png', 390, 844, true);
  await cdp.evaluate('window.scrollTo(0,0)'); await screenshot('desktop-final.png');
  assert(await cdp.evaluate("[...document.querySelectorAll('svg *')].every(e=>[...e.attributes].every(a=>! /\\b(NaN|Infinity)\\b/.test(a.value)))"), 'SVG geometry has no nonfinite coordinates');
  assert(externalRequests.length === 0, 'no external page request', externalRequests);
  assert(consoleErrors.length === 0, 'no browser console errors', consoleErrors);
  assert(runtimeErrors.length === 0, 'no uncaught browser exceptions', runtimeErrors);
  assert(responseErrors.length === 0, 'no failed HTTP response', responseErrors);
  summary = { baseline, newRun, vehicle, anchor, previousCase, newCase, editedGap: gap, editedRadius: radius, sourceApcCommon: diff.summary.apc_common, changes: diff.summary };
}

async function runReadOnlyChecks() {
  const debug = await read('/api/debug');
  assert(debug.enabled && debug.date === '2023-11-06', 'single-day debug mode unchanged');
  await cdp.send('Page.navigate', { url: appUrl.href }); await ready();
  const run = await currentRun();
  const base = debug.runs.find(row => (row.id || row.run_id) !== run);
  assert(Boolean(base), 'existing baseline available without creating another run');
  const baseId = base.id || base.run_id;
  await setValue('#debug-base-select', baseId);
  await waitFor(`(() => { try { const d=JSON.parse(document.querySelector('#debug-diff-json').textContent); return d.base_run_id===${JSON.stringify(baseId)} && d.run_id===${JSON.stringify(run)}; } catch { return false; } })()`);
  await waitFor("['基线移除','当前新增','理由变化'].every(s=>document.querySelector('#debug-boundary-svg').textContent.includes(s))");
  assert(true, 'all three boundary comparison lanes render, including reason changes');
  assert(await cdp.evaluate("document.querySelector('#debug-diff-summary').textContent.includes('判定细节变化') && document.querySelector('#debug-diff-summary').textContent.includes('边界理由变化')"), 'decision detail and boundary reason counts are visible');
  assert(await cdp.evaluate("[...document.querySelectorAll('svg *')].every(e=>[...e.attributes].every(a=>! /\\b(NaN|Infinity)\\b/.test(a.value)))"), 'latest SVG geometry is finite');
  await cdp.evaluate("document.querySelector('#debug-diff').scrollIntoView({block:'start'})");
  await screenshot('three-lane-boundary-diff.png');
  await cdp.send('Page.reload', { ignoreCache: true }); await ready();
  await setValue('#debug-base-select', baseId);
  await waitFor("document.querySelector('#debug-boundary-svg').textContent.includes('理由变化')");
  assert(await currentRun() === run, 'read-only reload retains latest existing run');
  await cdp.evaluate("document.querySelector('#debug-diff').scrollIntoView({block:'start'})");
  await screenshot('three-lane-after-reload.png');
  await screenshot('three-lane-mobile.png', 390, 844, true);
  assert(await cdp.evaluate('document.documentElement.scrollWidth<=window.innerWidth+2'), 'latest mobile diff does not overflow document');
  assert(!requests.some(request => !['GET','HEAD','OPTIONS'].includes(request.method)), 'strictly read-only: no experiment or review POST');
  assert(externalRequests.length === 0, 'no external page requests', externalRequests);
  assert(consoleErrors.length === 0, 'no console errors', consoleErrors);
  assert(runtimeErrors.length === 0, 'no uncaught runtime errors', runtimeErrors);
  assert(responseErrors.length === 0, 'no HTTP errors', responseErrors);
  summary = { baseId, run, mode: 'final-readonly', writes: 0, threeBoundaryLanes: true };
}

try { await startBrowser(); if (readOnly) await runReadOnlyChecks(); else await runChecks(); }
catch (error) { failure = String(error.stack || error); if(cdp) try { await screenshot('failure.png'); } catch {} }
finally {
  if(cdp) cdp.socket.close(); if(browser) browser.kill('SIGTERM');
  await writeFile(join(output, 'browser-debug-results.json'), JSON.stringify({ startedAt, completedAt: new Date().toISOString(), url: appUrl.href, checks, summary, failure, consoleErrors, runtimeErrors, externalRequests, responseErrors, dialogs, requests, profile, note: 'Dedicated disposable review state. UI/lineage checks, not accuracy validation.' }, null, 2), { mode: 0o600 });
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, output, failure }, null, 2));
  if(failure) process.exitCode = 1;
}
