/** Read-only browser acceptance for persisted joint-gap GTFS child cases. */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2), option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
const run = option('--run', null), legacy = option('--legacy-run', 'run-20231106-20260909T054519-503f1dfd4bd7');
if (!run) throw new Error('--run must name a completed existing run; this script never creates experiments.');
const origin = 'http://127.0.0.1:8768', output = resolve(option('--output', 'artifacts/merge-debug-20231106/joint-gap-browser'));
await mkdir(output, { recursive: true, mode: 0o700 });
const profile = await mkdtemp(join(tmpdir(), 'gtfs-gap-chrome-')), checks = [], errors = [], external = [], requests = [], cases = [];
let browser, cdp, failure;
const assert = (value, name, details) => { checks.push({ name, passed: Boolean(value), ...(details === undefined ? {} : { details }) }); if (!value) throw new Error(name); };
class CDP {
  constructor(socket) { this.socket = socket; this.id = 0; this.pending = new Map(); this.handlers = new Map(); socket.addEventListener('message', ({ data }) => { const message = JSON.parse(data); if (message.id) { const p = this.pending.get(message.id); this.pending.delete(message.id); if (p) message.error ? p.reject(new Error(JSON.stringify(message.error))) : p.resolve(message.result); } else for (const f of this.handlers.get(message.method) || []) Promise.resolve(f(message.params)).catch(e => errors.push(String(e))); }); }
  on(method, f) { this.handlers.set(method, [...(this.handlers.get(method) || []), f]); }
  send(method, params = {}) { const id = ++this.id, p = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject })); this.socket.send(JSON.stringify({ id, method, params })); return p; }
  async evaluate(expression) { const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; }
}
async function waitFor(expression, timeout = 60000) { const start = Date.now(); while (Date.now() - start < timeout) { const value = await cdp.evaluate(expression); if (value) return value; await delay(100); } throw new Error('Timeout: ' + expression); }
async function read(path) { const response = await fetch(origin + path); assert(response.ok, 'GET ' + path.split('?')[0]); return response.json(); }
async function set(id, value) { await cdp.evaluate(`(() => {const e=document.getElementById(${JSON.stringify(id)});e.value=${JSON.stringify(String(value))};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`); }
async function ready() { await waitFor("document.querySelector('#case-workspace') && !document.querySelector('#case-workspace').hidden && document.querySelector('#case-loading').hidden && !document.querySelector('#debug-form').inert"); }
async function switchRun(id) { await set('debug-run-select', id); await waitFor(`document.querySelector('#run-label').textContent.includes(${JSON.stringify(id)})`); await ready(); }
async function chooseCase(id) { await set('case-search', id); await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId===${JSON.stringify(id)} && document.querySelector('#case-loading').hidden`); await ready(); }
async function screenshot(name, mobile = false) { await cdp.send('Emulation.setDeviceMetricsOverride', { width: mobile ? 390 : 1500, height: mobile ? 844 : 1100, deviceScaleFactor: 1, mobile }); await delay(300); const r = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true }); await writeFile(join(output, name), Buffer.from(r.data, 'base64'), { mode: 0o600 }); }
async function startBrowser() {
  browser = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability', '--disable-extensions', '--disable-sync', '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const endpoint = await new Promise((resolve, reject) => { let log = ''; const timer = setTimeout(() => reject(new Error(log)), 15000); browser.on('error', reject); browser.stderr.on('data', chunk => { log += chunk; const m = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (m) { clearTimeout(timer); resolve(m[1]); } }); });
  const pages = await fetch(endpoint.replace(/^ws:/, 'http:').split('/devtools/')[0] + '/json/list').then(r => r.json()), socket = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); }); cdp = new CDP(socket);
  cdp.on('Runtime.exceptionThrown', e => errors.push(e)); cdp.on('Runtime.consoleAPICalled', e => { if (e.type === 'error') errors.push(e); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({ url: request.url, method: request.method }); if (/^https?:/.test(request.url) && new URL(request.url).origin !== origin) external.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if (response.status >= 400) errors.push({ url: response.url, status: response.status }); });
  cdp.on('Page.javascriptDialogOpening', () => cdp.send('Page.handleJavaScriptDialog', { accept: false }));
  cdp.on('Fetch.requestPaused', ({ requestId, request }) => { const allowed = request.method === 'GET' && (/^(about:|data:|blob:)/.test(request.url) || new URL(request.url).origin === origin); return cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', { requestId, ...(allowed ? {} : { errorReason: 'BlockedByClient' }) }); });
  for (const method of ['Page.enable', 'Runtime.enable', 'Network.enable']) await cdp.send(method); await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] }); await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
}
const identity = row => `${row.source}|${row.rid}`;
async function inspectCase(meta, label, id) {
  const vehicle = meta.vehicles.find(v => String(v.label) === label); assert(vehicle, 'vehicle ' + label + ' exists');
  await set('case-search', ''); await set('vehicle-select', vehicle.id); await ready(); await chooseCase(id); await set('window-size', 'full');
  const query = new URLSearchParams({ run, method: 'gtfs_trip_matching', vehicle: vehicle.id, case_id: id, context_s: '180' }), detail = await read('/api/case?' + query), c = detail.case;
  if (c.gap_split) {
    const expected = new Set(c.source_rids.map(identity)), focus = detail.observations.filter(r => r.in_focus);
    assert(expected.size === focus.length && focus.every(r => expected.has(identity(r))), id + ' all and only persisted child members are focused');
    assert(detail.observations.every(r => r.in_focus || r.in_window), id + ' no distant parent members leak into child context');
    assert(detail.observations.every(r => r.review_candidate_ids.includes(id) === r.in_focus && r.candidate_segment_ids.every(p => !p.includes(':gap:'))), id + ' review membership is separate from unchanged parent annotations');
    assert(detail.avl.length === detail.observations.filter(r => r.source === 'avl').length && detail.apc.length === detail.observations.filter(r => r.source === 'apc').length, id + ' all three representations conserve loaded source rows');
    assert(detail.avl.filter(r => r.in_focus).length === c.avl_count && detail.apc.filter(r => r.in_focus).length === c.apc_count, id + ' child focus counts equal catalog');
    assert(detail.apc.every(r => r.progress_m == null && r.status === 'not_evaluated'), id + ' APC remains original interval evidence without fabricated progress');
    assert(detail.window.start >= c.source_start - 180 && detail.window.end <= c.source_end + 180, id + ' browse window follows observed coverage not inherited day edges');
    assert(await cdp.evaluate(`!document.querySelector('#gap-segmentation-panel').hidden && document.querySelector('#gap-segmentation-panel').dataset.parentCaseId===${JSON.stringify(c.parent_case_id)} && document.querySelector('#gap-fragment-title').textContent.includes(${JSON.stringify(`${c.gap_fragment_index} / ${c.gap_fragment_count}`)})`), id + ' child part and exact parent ID visible');
    assert(await cdp.evaluate("!document.querySelector('#direction-candidate-note').textContent.includes('Direction boundaries are inherited from the parent') && document.querySelector('#direction-candidate-note').textContent.includes('review_candidate_ids')"), id + ' no false unchanged-boundary claim');
    for (const [side, boundary] of [['before', c.gap_boundary_before], ['after', c.gap_boundary_after]]) if (boundary) {
      assert(boundary.duration_s > boundary.threshold_s && boundary.operator === '>', id + ' strict greater-than threshold');
      assert(await cdp.evaluate(`document.querySelector('.gap-boundary[data-side=${side}]')?.dataset.durationSeconds===${JSON.stringify(String(boundary.duration_s))}`), id + ' exact ' + side + ' gap duration visible');
    }
    assert(await cdp.evaluate(`document.querySelector('#gap-segmentation-audit').textContent.includes(${JSON.stringify(c.source_rids[0].rid)})`), id + ' boundary and member source lineage inspectable');
    const selected = detail.observations.find(r => r.source === 'apc' && r.in_focus) || focus[0];
    await set('observation-search', selected.rid); await waitFor("document.querySelectorAll('#observation-table-body tr[data-rid]').length>0");
    await cdp.evaluate(`document.querySelector('#observation-table-body tr[data-rid="${selected.rid}"][data-source="${selected.source}"]').click()`);
    assert(await cdp.evaluate(`document.querySelector('#inspector .record-id').textContent===${JSON.stringify(selected.rid)} && document.querySelector('#inspector').textContent.includes('Original parent direction candidate IDs') && document.querySelector('#inspector').textContent.includes('Current review_candidate_ids')`), id + ' raw row selection shows both parent and review lineage');
    await set('observation-search', '');
    await cdp.evaluate("document.querySelector('#gap-segmentation-panel').scrollIntoView({block:'start'})"); await screenshot(id.replaceAll(':', '-') + '.png');
    if (!c.avl_count) {
      assert(detail.avl.length === 0 && detail.gtfs.projection.points.length === 0, id + ' APC-only child contains no fabricated AVL trajectory');
      assert(detail.apc.filter(r => r.in_focus).length === 6, id + ' all six APC tail records preserved');
      const windows = Array.isArray(detail.gtfs.windows) ? detail.gtfs.windows : detail.gtfs.windows?.windows || [];
      assert(detail.gtfs.candidates.length === 0 && windows.length > 0 && windows.every(w => (w.status || w.quality?.status) === 'NO_EVIDENCE'), id + ' APC-only child has no accepted schedule candidates and all available windows abstain NO_EVIDENCE');
      assert(await cdp.evaluate("document.querySelectorAll('.gtfs-window-row').length>0 && [...document.querySelectorAll('.gtfs-window-row')].every(e=>e.dataset.status==='NO_EVIDENCE')"), id + ' actual browser window list exposes NO_EVIDENCE throughout');
      assert(await cdp.evaluate("document.querySelector('#case-title').textContent.includes('13:34:26') && document.querySelector('#case-title').textContent.includes('13:54:20') && !document.querySelector('#case-title').textContent.includes('00:00')"), id + ' APC tail title shows actual observed time not midnight');
      assert(await cdp.evaluate("document.querySelectorAll('#gtfs-progress-svg circle[data-source=avl]').length===0 && document.querySelector('#gtfs-result-status').dataset.status!=='MATCHED'"), id + ' no AVL points or fabricated MATCHED result');
      await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})"); await screenshot('apc-only-timeline.png');
    }
  } else {
    assert(await cdp.evaluate("document.querySelector('#gap-segmentation-panel').hidden"), 'unchanged case hides child-only gap panel');
    assert(detail.gtfs.candidates.length === 0 && detail.gtfs.diagnostic_candidates.length === 3, 'unchanged 9028 case retains zero finals and three separate diagnostics');
    assert(await cdp.evaluate("document.querySelectorAll('.gtfs-diagnostic-card').length===3 && document.querySelectorAll('.gtfs-candidate-card').length===0 && [...document.querySelectorAll('.gtfs-diagnostic-card')].every(e=>e.dataset.diagnosticOnly==='true')"), 'diagnostic panel remains separate from final candidates');
  }
  assert(await cdp.evaluate("['timeline-svg','progress-svg','map-svg','gtfs-schedule-svg','gtfs-progress-svg'].every(id=>document.getElementById(id).dataset.viewStart===document.querySelector('#timeline-pan').dataset.viewStart && document.getElementById(id).dataset.viewEnd===document.querySelector('#timeline-pan').dataset.viewEnd)"), id + ' all five views share child browsing bounds');
  assert(await cdp.evaluate("[...document.querySelectorAll('svg *')].every(e=>[...e.attributes].every(a=>!['d','x','y','cx','cy','x1','x2','y1','y2'].includes(a.name)||!/(NaN|Infinity)/.test(a.value)))"), id + ' all SVG geometry finite');
  cases.push({ id, vehicle: label, status: detail.gtfs.result.status, counts: detail.counts, window: detail.window, final_candidates: detail.gtfs.candidates.length, diagnostic_candidates: detail.gtfs.diagnostic_candidates?.length || 0 });
  return detail;
}
try {
  const meta = await read('/api/meta?run=' + run), before = await read('/api/reviews/export?run=' + run);
  assert(meta.gap_segmentation_used === true, 'run declares joint gap segmentation');
  await startBrowser(); await cdp.send('Page.navigate', { url: origin }); await ready(); await switchRun(run);
  const left = await inspectCase(meta, '9027', 'direction:9027:0001:gap:0001'), right = await inspectCase(meta, '9027', 'direction:9027:0001:gap:0002');
  const leftIds = new Set(left.case.source_rids.map(identity)); assert(right.case.source_rids.every(r => !leftIds.has(identity(r))), 'long-gap siblings have disjoint exact source membership');
  assert(left.window.end < right.window.start, 'long gap remains absent between child browsing windows');
  const parentQuery = new URLSearchParams({ run: legacy, method: 'gtfs_trip_matching', vehicle: meta.vehicles.find(v => String(v.label) === '9027').id, case_id: 'direction:9027:0001', context_s: '0' }), parent = await read('/api/case?' + parentQuery), original = new Map(parent.observations.filter(r => r.in_focus).map(r => [identity(r), r])), combined = [...left.observations, ...right.observations].filter(r => r.in_focus);
  assert(combined.length === original.size && combined.every(r => original.has(identity(r))), 'all parent source identities conserved exactly once across the long-gap siblings');
  assert(combined.every(r => JSON.stringify(r.source_fields) === JSON.stringify(original.get(identity(r)).source_fields)), 'child source payload fields remain identical to pinned parent evidence');
  await inspectCase(meta, '9147', 'direction:9147:0002:gap:0002');
  await cdp.evaluate("document.querySelector('#gap-segmentation-panel').scrollIntoView({block:'start'})"); await screenshot('gap-child-mobile.png', true);
  assert(await cdp.evaluate("document.documentElement.scrollWidth<=innerWidth+2"), 'gap lineage and boundary evidence fit mobile without horizontal overflow');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
  await inspectCase(meta, '9028', 'direction:9028:0004');
  await switchRun(legacy); assert(await cdp.evaluate("document.querySelector('#gap-segmentation-panel').hidden && document.querySelector('#direction-candidate-note').textContent.includes('Direction boundaries are inherited from the parent')"), 'legacy run preserves original boundary interpretation and hides gap panel');
  const after = await read('/api/reviews/export?run=' + run); assert(JSON.stringify(before.reviews) === JSON.stringify(after.reviews), 'review notes unchanged');
  assert(!requests.some(r => r.method !== 'GET'), 'zero non-GET browser requests; no experiment or note mutation');
  assert(errors.length === 0 && external.length === 0, 'zero console HTTP or external-request errors', { errors, external });
} catch (error) { failure = error.stack || String(error); }
finally {
  if (cdp && failure) try { await screenshot('failure.png'); } catch {}
  if (cdp) try { await cdp.send('Browser.close'); } catch {}
  if (browser) browser.kill('SIGTERM');
  await writeFile(join(output, 'browser-gap-results.json'), JSON.stringify({ passed: !failure, run, legacy, checks, cases, errors, external, requests, failure }, null, 2), { mode: 0o600 });
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, output, failure }));
  if (failure) process.exitCode = 1;
}
