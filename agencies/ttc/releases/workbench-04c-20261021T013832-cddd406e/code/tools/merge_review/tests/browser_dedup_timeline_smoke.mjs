/** Exact AVL dedup + linked timeline pan. Writes only disposable localhost:8768. */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const baseURL = 'http://127.0.0.1:8768';
if (!args.includes('--allow-test-writes')) throw new Error('Dedicated test workspace and --allow-test-writes required.');
const output = resolve(option('--output', 'artifacts/merge-debug-20231106/dedup-timeline-browser'));
await mkdir(output, { recursive: true, mode: 0o700 });
const profile = await mkdtemp(join(tmpdir(), 'dedup-timeline-chrome-'));
const checks = [], errors = [], external = [], requests = [];
let browser, cdp, failure = null, summary = {};
const assert = (ok, name, details) => { checks.push({ name, passed: Boolean(ok), details }); if (!ok) throw new Error(name); };
class CDP {
  constructor(socket) {
    this.socket = socket; this.id = 0; this.pending = new Map(); this.handlers = new Map();
    socket.addEventListener('message', ({ data }) => {
      const m = JSON.parse(data);
      if (m.id) { const p = this.pending.get(m.id); this.pending.delete(m.id); if (p) m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
      else for (const f of this.handlers.get(m.method) || []) Promise.resolve(f(m.params)).catch(e => errors.push(String(e)));
    });
  }
  on(method, handler) { this.handlers.set(method, [...(this.handlers.get(method) || []), handler]); }
  send(method, params = {}) { const id = ++this.id; const p = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject })); this.socket.send(JSON.stringify({ id, method, params })); return p; }
  async evaluate(expression) { const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; }
}
async function waitFor(expression, timeout = 45000) {
  const started = Date.now(); let nextLog = started + 15000;
  while (Date.now() - started < timeout) {
    const value = await cdp.evaluate(expression); if (value) return value;
    if (Date.now() > nextLog) { console.log(JSON.stringify({ waiting_s: Math.round((Date.now() - started) / 1000) })); nextLog += 15000; }
    await delay(100);
  }
  throw new Error(`Timeout: ${expression}`);
}
async function read(path) { const r = await fetch(baseURL + path); assert(r.ok, 'HTTP GET ' + path.split('?')[0]); return r.json(); }
async function ready() { await waitFor("!document.querySelector('#case-workspace').hidden && document.querySelector('#case-loading').hidden && !document.querySelector('#debug-form').inert"); }
async function value(id, v) { await cdp.evaluate(`(() => {const e=document.getElementById(${JSON.stringify(id)}); e.value=${JSON.stringify(String(v))}; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));})()`); }
async function click(id) { assert(await cdp.evaluate(`(() => {const e=document.getElementById(${JSON.stringify(id)});if(!e||e.disabled)return false;e.click();return true})()`), 'click ' + id); }
async function switchRun(id) { await value('debug-run-select', id); await waitFor(`document.querySelector('#run-label').textContent.includes(${JSON.stringify(id)}) && document.querySelector('#case-loading').hidden`); await ready(); }
async function keyboard(key, code = key) { const windowsVirtualKeyCode = { Home: 36, End: 35, ArrowLeft: 37, ArrowRight: 39 }[key]; await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode }); await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode }); await delay(120); }
async function shot(name, width = 1500, height = 1050, mobile = false) { await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile }); await delay(220); const r = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true }); await writeFile(join(output, name), Buffer.from(r.data, 'base64'), { mode: 0o600 }); }
async function panState() { return cdp.evaluate("(() => {const e=document.querySelector('#timeline-pan'); return {value:Number(e.value), disabled:e.disabled, start:Number(e.dataset.viewStart), end:Number(e.dataset.viewEnd), label:document.querySelector('#timeline-pan-label').textContent}})()"); }

async function startBrowser() {
  browser = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability', '--disable-extensions', '--disable-sync', '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const endpoint = await new Promise((resolve, reject) => { let log = ''; const timer = setTimeout(() => reject(new Error(log)), 15000); browser.on('error', reject); browser.stderr.on('data', chunk => { log += chunk; const m = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (m) { clearTimeout(timer); resolve(m[1]); } }); });
  const targets = await fetch(endpoint.replace(/^ws:/, 'http:').split('/devtools/')[0] + '/json/list').then(r => r.json());
  const socket = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  cdp = new CDP(socket);
  cdp.on('Runtime.exceptionThrown', r => errors.push(r));
  cdp.on('Runtime.consoleAPICalled', r => { if (r.type === 'error') errors.push(r); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({ url: request.url, method: request.method }); if (/^https?:/.test(request.url) && new URL(request.url).origin !== baseURL) external.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if (response.status >= 400) errors.push({ url: response.url, status: response.status }); });
  cdp.on('Page.javascriptDialogOpening', () => cdp.send('Page.handleJavaScriptDialog', { accept: false }));
  cdp.on('Fetch.requestPaused', ({ requestId, request }) => { const allowed = /^(about:|data:|blob:)/.test(request.url) || new URL(request.url).origin === baseURL; return cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', { requestId, ...(allowed ? {} : { errorReason: 'BlockedByClient' }) }); });
  for (const method of ['Page.enable', 'Runtime.enable', 'Network.enable']) await cdp.send(method);
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1050, deviceScaleFactor: 1, mobile: false });
}

async function runChecks() {
  const debug = await read('/api/debug');
  const old = debug.runs.find(r => r.method === 'vehicle_only' || r.config?.method === 'vehicle_only');
  assert(debug.date === '2023-11-06' && old, 'fixed Nov6 workspace with original experiment');
  const baseline = old.run_id || old.id;
  const oldNotes = await read('/api/reviews/export?run=' + baseline);
  await cdp.send('Page.navigate', { url: baseURL }); await ready(); await switchRun(baseline);
  const existing = option('--existing-run', null);
  if (existing) await switchRun(existing);
  else {
    await value('debug-method', 'vehicle_only_dedup');
    assert(await cdp.evaluate("[...document.querySelectorAll('#debug-segmentation-parameters input,#debug-merge-parameters input')].every(e=>e.disabled)"), 'dedup mode disables trip and matching parameters');
    await value('debug-label', 'Disposable exact AVL dedup + timeline pan'); await click('debug-submit');
    await waitFor("document.querySelector('#debug-job-title').textContent.includes('Reprocessing complete') || document.querySelector('#debug-job-title').textContent.includes('Reprocessing failed')", 240000);
    assert(await cdp.evaluate("document.querySelector('#debug-job-title').textContent.includes('Reprocessing complete')"), 'actual UI dedup job completed');
    await waitFor("document.querySelector('#method-select').value==='vehicle_only_dedup' && document.querySelector('#case-loading').hidden"); await ready();
  }
  const run = await cdp.evaluate("document.querySelector('#debug-run-select').value");
  const meta = await read('/api/meta?run=' + run);
  assert(meta.methods.length === 1 && meta.methods[0] === 'vehicle_only_dedup', 'new mode persisted separately');
  assert(!meta.trip_segmentation_used && !meta.observation_matching_used, 'no trip segmentation or APC matching');
  const vehicle = await cdp.evaluate("document.querySelector('#vehicle-select').value");
  const query = new URLSearchParams({ run, method: 'vehicle_only_dedup', vehicle });
  const catalog = await read('/api/catalog?' + query); assert(catalog.cases.length === 1, 'one vehicle-day, no inferred trips');
  query.set('case_id', catalog.cases[0].id);
  const detail = await read('/api/case?' + query);
  assert(detail.avl.length === 2814 && detail.apc.length === 281, 'default vehicle 9027 keeps 2814 AVL and all 281 APC');
  assert(detail.observations.length === 3095, 'dedup stream has expected exact rows');
  assert(detail.provenance.deduplication_performed, 'deduplication explicitly disclosed');
  assert(detail.apc.every(r => r.status === 'not_evaluated' && r.progress_m === null), 'APC unchanged and never projected to GPS');
  assert(detail.observations.every((r, i) => r.display_order === i), 'retained rows have contiguous stable display order');
  assert(new Set(detail.observations.map(r => `${r.source}|${r.rid}`)).size === 3095, 'retained source identities unique');
  assert(detail.deduplication.length === 3297, 'mapping accounts for every original AVL row');
  assert(detail.deduplication.filter(r => !r.is_representative).length === 483, 'exactly 483 default-vehicle duplicate copies collapsed');
  const duplicate = detail.observations.find(r => r.source === 'avl' && r.deduplication?.duplicate_group_size > 1);
  assert(duplicate && duplicate.deduplication.source_rids.length === duplicate.deduplication.duplicate_group_size, 'representative retains all duplicate source IDs');
  await value('observation-search', duplicate.rid);
  assert(await cdp.evaluate(`(() => {const r=document.querySelector('#observation-table-body tr[data-rid]');if(!r||r.dataset.rid!==${JSON.stringify(duplicate.rid)})return false;r.click();return true})()`), 'retained duplicate representative selectable');
  assert(await cdp.evaluate(`document.querySelector('#inspector').textContent.includes(${JSON.stringify(duplicate.deduplication.source_rids.at(-1))})`), 'inspector exposes collapsed original source identity');
  await value('observation-search', '');
  const originalQuery = new URLSearchParams({ run: baseline, method: 'vehicle_only', vehicle });
  const originalCase = (await read('/api/catalog?' + originalQuery)).cases[0]; originalQuery.set('case_id', originalCase.id);
  const original = await read('/api/case?' + originalQuery);
  assert(original.avl.length === 3297 && original.apc.length === 281, 'experiment01 original observations preserved');
  assert(JSON.stringify(original.apc.map(r => [r.rid, r.arrival, r.departure, r.boarding, r.alighting]).sort()) === JSON.stringify(detail.apc.map(r => [r.rid, r.arrival, r.departure, r.boarding, r.alighting]).sort()), 'all APC identities, original times and counts unchanged');
  await value('window-size', '1800');
  await cdp.evaluate("document.querySelector('#timeline-pan').scrollIntoView({block:'center'});document.querySelector('#timeline-pan').focus()");
  await keyboard('Home');
  let pan = await panState();
  assert(!pan.disabled && pan.start === meta.scope.start && pan.end === meta.scope.start + 1800, 'native Home reaches first half hour');
  const firstTimeline = await cdp.evaluate("document.querySelector('#timeline-svg').innerHTML");
  const firstProgress = await cdp.evaluate("document.querySelector('#progress-svg').innerHTML");
  await keyboard('End'); pan = await panState();
  assert(pan.end === meta.scope.end && pan.start === meta.scope.end - 1800, 'native End reaches last half hour');
  const lastWindowAPC = await cdp.evaluate("[...document.querySelectorAll('#timeline-svg [data-rid]')].map(r=>r.dataset.rid).sort()");
  const expectedLastAPC = detail.apc.filter(r => Math.max(r.arrival ?? r.departure, r.departure ?? r.arrival) >= pan.start && Math.min(r.arrival ?? r.departure, r.departure ?? r.arrival) <= pan.end).map(r => r.rid).sort();
  assert(JSON.stringify(lastWindowAPC) === JSON.stringify(expectedLastAPC), 'last window renders every overlapping APC interval without clipping records away');
  assert(await cdp.evaluate("document.querySelector('#timeline-svg').innerHTML") !== firstTimeline, 'pan moves the actual timeline, not just the cursor');
  assert(await cdp.evaluate("document.querySelector('#progress-svg').innerHTML") !== firstProgress, 'distance chart follows the time window');
  await keyboard('ArrowLeft');
  const moved = await panState(); assert(moved.start < pan.start && moved.end - moved.start === 1800, 'keyboard panning preserves window duration');
  const rect = await cdp.evaluate("(() => {const r=document.querySelector('#timeline-pan').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()");
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 }); await delay(180);
  pan = await panState(); assert(pan.start > meta.scope.start + 6 * 3600 && pan.start < meta.scope.start + 18 * 3600, 'real mouse click pans into the middle of the day');
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: rect.x + 80, y: rect.y, button: 'left', buttons: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x + 80, y: rect.y, button: 'left', clickCount: 1 }); await delay(180);
  assert((await panState()).start > pan.start, 'real horizontal dragging pans the visible time window');
  assert(await cdp.evaluate("!document.querySelector('#observation-window-only').checked"), 'table stays all-day by default while panning');
  assert(await cdp.evaluate("document.querySelector('#observation-count').textContent.includes('3,095')"), 'all retained records remain accessible');
  await value('timeline-pan', 0);
  await click('observation-window-only');
  const filtered = await cdp.evaluate("[...document.querySelectorAll('#observation-table-body tr[data-rid]')].map(r=>r.dataset.rid)");
  const expected = new Set(detail.observations.filter(r => r.event_time >= meta.scope.start && r.event_time <= meta.scope.start + 1800).map(r => r.rid));
  assert(filtered.length > 0 && filtered.every(r => expected.has(r)), 'window-only source table follows the panned window');
  await click('observation-window-only');
  await value('window-size', 'full');
  assert((await panState()).disabled, 'all-day overview clearly disables meaningless full-width panning');
  await click('timeline-pan-detail');
  assert(!(await panState()).disabled, 'one click returns from overview to movable detail');
  await value('timeline-pan', 1000);
  await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})");
  await shot('timeline-last-window.png');
  await value('timeline-pan', 0);
  await shot('timeline-first-window.png');
  await cdp.evaluate("document.querySelector('#observation-stream-panel').scrollIntoView({block:'start'})");
  await shot('deduplicated-observations.png');
  await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})");
  await shot('timeline-mobile.png', 390, 844, true);
  assert(await cdp.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'mobile viewport has no horizontal page overflow');
  assert(await cdp.evaluate("(() => {const e=document.querySelector('#timeline-pan-overview');return Math.abs(Number(e.getAttribute('viewBox').split(' ')[2])-e.parentElement.clientWidth)<2})()"), 'mobile overview recalculates its coordinate width instead of shrinking desktop text');
  assert(await cdp.evaluate("Number(document.querySelector('#timeline-pan-overview [data-source=avl]').dataset.observations)") === detail.avl.length, 'overview includes all retained AVL observations, not only current window');
  assert(await cdp.evaluate("Number(document.querySelector('#timeline-pan-overview [data-source=apc]').dataset.observations)") === detail.apc.length, 'overview includes all APC observations, not only current window');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1050, deviceScaleFactor: 1, mobile: false });
  await switchRun(baseline); assert(await cdp.evaluate("document.querySelector('#method-select').value==='vehicle_only'"), 'old unmodified stream still opens');
  await switchRun(run); assert(await cdp.evaluate("document.querySelector('#method-select').value==='vehicle_only_dedup'"), 'new dedup stream reopens');
  const comparison = await read(`/api/debug/compare?base=${baseline}&run=${run}&vehicle=${vehicle}`);
  assert(comparison.summary.dedup_explained_removals === 483 && comparison.summary.unexplained_removals === 0, 'comparison explains all removed AVL copies');
  assert(comparison.summary.apc_unchanged && comparison.summary.relative_order_preserved, 'APC and retained relative chronology remain unchanged');
  assert(comparison.retained_counts.base.avl === 3297 && comparison.retained_counts.current.avl === 2814, 'comparison counts use selected vehicle, not whole-day fleet');
  const after = await read('/api/reviews/export?run=' + baseline);
  assert(JSON.stringify(after.reviews) === JSON.stringify(oldNotes.reviews), 'old review notes untouched');
  assert(requests.filter(r => r.method === 'POST' && r.url.includes('/api/reviews')).length === 0, 'visual tests did not write review notes');
  assert(external.length === 0, 'no external data requests'); assert(errors.length === 0, 'no console, runtime or HTTP errors');
  summary = { run, baseline, vehicle, retained_avl: detail.avl.length, apc: detail.apc.length, removed_avl: original.avl.length - detail.avl.length, pan_first: meta.scope.start, pan_last_end: meta.scope.end };
}
try { await startBrowser(); await runChecks(); }
catch (e) { failure = String(e.stack || e); console.error(failure); }
finally {
  await writeFile(join(output, 'browser-dedup-timeline-results.json'), JSON.stringify({ checks, summary, errors, external, requests, failure }, null, 2), { mode: 0o600 });
  if (cdp) { try { await cdp.send('Browser.close'); } catch {} cdp.socket.close(); }
  if (browser) browser.kill('SIGTERM');
}
console.log(JSON.stringify({ passed: checks.filter(r => r.passed).length, failed: checks.filter(r => !r.passed).length, failure, output }));
if (failure) process.exitCode = 1;
