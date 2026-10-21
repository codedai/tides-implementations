/** Real-browser Experiment05 acceptance. Only completed all-mode runs may be POSTed for cache reuse. */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2), option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
const origin = option('--origin', 'http://127.0.0.1:8780'), output = resolve(option('--output', 'outputs/experiment_05/acceptance/browser-preflight'));
const runIds = args.flatMap((value, index) => value === '--run' ? [args[index + 1]] : []);
if (!/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(origin)) throw new Error('Only an explicit localhost origin is allowed');
await mkdir(output, { recursive: true, mode: 0o700 });
const profile = await mkdtemp(join(tmpdir(), 'experiment05-chrome-'));
const checks = [], errors = [], requests = [], external = [], reports = [], blocked = [], warnings = [];
let browser, cdp, failure, allowedDate = null;
const assert = (value, name, details) => { checks.push({ name, passed: Boolean(value), ...(details === undefined ? {} : { details }) }); if (!value) throw new Error(name); };
class CDP {
  constructor(socket) { this.socket = socket; this.id = 0; this.pending = new Map(); this.handlers = new Map(); socket.addEventListener('message', ({ data }) => { const msg = JSON.parse(data); if (msg.id) { const item = this.pending.get(msg.id); this.pending.delete(msg.id); if (item) msg.error ? item.reject(new Error(JSON.stringify(msg.error))) : item.resolve(msg.result); } else for (const fn of this.handlers.get(msg.method) || []) Promise.resolve(fn(msg.params)).catch(error => errors.push(String(error))); }); }
  on(name, fn) { this.handlers.set(name, [...(this.handlers.get(name) || []), fn]); }
  send(method, params = {}) { const id = ++this.id, result = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject })); this.socket.send(JSON.stringify({ id, method, params })); return result; }
  async evaluate(expression) { const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value; }
}
async function waitFor(expression, timeout = 120000) { const start = Date.now(); while (Date.now() - start < timeout) { if (await cdp.evaluate(expression)) return; await delay(200); } throw new Error('Timeout: ' + expression); }
async function json(path) { const response = await fetch(origin + path); assert(response.ok, 'GET ' + path); return response.json(); }
async function set(id, value) { await cdp.evaluate(`(() => { const el=document.getElementById(${JSON.stringify(id)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('change',{bubbles:true})); })()`); }
async function screenshot(name, mobile = false) { await cdp.send('Emulation.setDeviceMetricsOverride', { width: mobile ? 390 : 1500, height: mobile ? 844 : 1100, deviceScaleFactor: 1, mobile }); await delay(250); const result = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false }); await writeFile(join(output, name), Buffer.from(result.data, 'base64'), { mode: 0o600 }); }
async function startBrowser() {
  browser = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability', '--disable-extensions', '--disable-sync', '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const endpoint = await new Promise((resolve, reject) => { let log = ''; const timer = setTimeout(() => reject(new Error(log)), 15000); browser.on('error', reject); browser.stderr.on('data', chunk => { log += chunk; const found = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (found) { clearTimeout(timer); resolve(found[1]); } }); });
  const tabs = await fetch(endpoint.replace(/^ws:/, 'http:').split('/devtools/')[0] + '/json/list').then(response => response.json());
  const socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  cdp = new CDP(socket);
  cdp.on('Runtime.exceptionThrown', error => errors.push(error));
  cdp.on('Runtime.consoleAPICalled', event => { if (event.type === 'error') errors.push(event); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({ method: request.method, url: request.url, ...(request.postData ? { postData: request.postData } : {}) }); if (/^https?:/.test(request.url) && new URL(request.url).origin !== origin) external.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if (response.status >= 400) (response.url === origin + '/favicon.ico' && response.status === 404 ? warnings : errors).push({ url: response.url, status: response.status }); });
  cdp.on('Fetch.requestPaused', ({ requestId, request }) => {
    let allowed = request.method === 'GET' && (/^(about:|data:|blob:)/.test(request.url) || new URL(request.url).origin === origin);
    if (request.method === 'POST' && request.url === origin + '/api/runs' && allowedDate) {
      try { const body = JSON.parse(request.postData); allowed = body.service_date === allowedDate && body.route_id === '29' && body.match_mode === 'all' && body.force === false && Object.keys(body).length === 4; } catch { allowed = false; }
    }
    if (!allowed) blocked.push({ url: request.url, method: request.method });
    return cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', { requestId, ...(allowed ? {} : { errorReason: 'BlockedByClient' }) });
  });
  for (const method of ['Page.enable', 'Runtime.enable', 'Network.enable']) await cdp.send(method);
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
}
async function downloadFile(detail, file) {
  const path = `/api/runs/${encodeURIComponent(detail.run_id)}/download?` + new URLSearchParams({ file });
  const response = await fetch(origin + path);
  assert(response.ok, 'download ' + detail.run_id + ' / ' + file);
  const hash = createHash('sha256'); let bytes = 0;
  for await (const chunk of response.body) { hash.update(chunk); bytes += chunk.length; }
  const digest = hash.digest('hex');
  assert(bytes > 0 && digest === detail.run_manifest.output_sha256[file], 'download original-byte SHA-256 ' + file, { bytes, sha256: digest });
  assert(response.headers.get('content-disposition')?.includes('attachment'), 'download attachment header ' + file);
  return { file, bytes, sha256: digest };
}
try {
  const health = await json('/api/health'), availability = await json('/api/availability');
  assert(health.ok && health.experiment === '05' && health.local_only, 'temporary server is local Experiment05');
  assert(JSON.stringify(availability.route_ids) === '["29"]', 'availability is limited to route29');
  assert(JSON.stringify(availability.match_modes) === '["global","early","middle","late"]', 'availability exposes exact four decision modes');
  for (const date of ['2023-11-06', '2023-11-20']) assert(availability.dates.some(day => day.service_date === date && day.available), date + ' has real raw rows and a GTFS archive');
  await startBrowser(); await cdp.send('Page.navigate', { url: origin });
  await waitFor("document.querySelector('#service-date')?.options.length>1 && document.querySelector('#run-list')?.childElementCount>0");
  assert(await cdp.evaluate("document.documentElement.lang==='en' && document.querySelector('#run-button').disabled"), 'initial UI requires an explicit available date');
  assert(await cdp.evaluate("JSON.stringify([...document.querySelector('#match-mode').options].map(option=>option.value))==='[\"all\",\"global\",\"early\",\"middle\",\"late\"]'"), 'UI has all four modes plus all-mode batch');
  assert(await cdp.evaluate("document.querySelector('#route').value==='29'"), 'UI route is29');
  await set('service-date', '2023-11-06');
  assert(await cdp.evaluate("!document.querySelector('#run-button').disabled && document.querySelector('#availability-note').textContent.includes('AVL')"), 'available real date enables processing and shows source counts');
  for (const mode of ['global', 'early', 'middle', 'late', 'all']) { await set('match-mode', mode); assert(await cdp.evaluate(`document.querySelector('#match-mode').value===${JSON.stringify(mode)}`), 'select mode ' + mode); }
  await cdp.evaluate("document.querySelector('#force').checked=true;document.querySelector('#force').checked=false");
  assert(await cdp.evaluate("!document.querySelector('#force').checked"), 'force control exists and remains off; no recomputation is authorized');
  assert(await cdp.evaluate("document.querySelector('#run-button').textContent==='Start processing' && document.querySelector('#service-date').value==='2023-11-06'"), 'English launcher retains chosen date and mode');
  await screenshot('launcher-mobile.png', true);
  assert(await cdp.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2"), 'mobile launcher has no page-level horizontal overflow');
  await screenshot('launcher-desktop.png');
  for (const [path, status] of [['/api/runs/not-existing/summary',404],['/api/runs/invalid%2Fid/summary',404]]) {
    const response = await fetch(origin + path), error = await response.json();
    assert(response.status === status && typeof error.message === 'string', 'explicit API error ' + path, error);
  }
  const invalid = await fetch(origin + '/api/runs', { method: 'POST', headers: { 'Content-Type':'application/json' }, body: JSON.stringify({ service_date:'not-a-date', route_id:'29', match_mode:'all', force:false }) });
  assert(invalid.status === 400 && (await invalid.json()).error === 'INVALID_DATE', 'malformed date rejected before pipeline initialization');
  assert(!availability.dates.some(day => day.service_date === '2023-12-01' && day.available), 'missing-input test date has no available raw day');
  const beforeMissingInput = (await json('/api/runs')).runs.map(run => run.run_id).sort();
  const missingInput = await fetch(origin + '/api/runs', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({service_date:'2023-12-01',route_id:'29',match_mode:'all',force:false}) });
  const missingError = await missingInput.json();
  assert(missingInput.status === 422 && missingError.error === 'INPUT_UNAVAILABLE' && typeof missingError.message === 'string', 'real missing-input date rejected before pipeline initialization', missingError);
  assert(JSON.stringify((await json('/api/runs')).runs.map(run => run.run_id).sort()) === JSON.stringify(beforeMissingInput), 'missing-input request creates no run directory');
  for (const id of runIds) {
    const detail = await json(`/api/runs/${encodeURIComponent(id)}/summary`), date = detail.summary?.service_date;
    assert(detail.status === 'complete' && detail.summary?.validation_ok === true && detail.run_manifest.validation_ok === true, 'requested run is validated complete ' + id);
    assert(JSON.stringify(detail.summary.match_modes) === '["global","early","middle","late"]', 'requested cache contains all modes ' + id);
    const runsBefore = (await json('/api/runs')).runs.map(run => run.run_id).sort();
    assert(id === `run-${date}-${detail.run_manifest.cache_key.slice(0,16)}`, 'only default non-force cache run may be replayed ' + id);
    allowedDate = date;
    await set('service-date', date); await set('match-mode', 'all');
    const previousJobLabel = await cdp.evaluate("document.querySelector('#job-id').textContent");
    await cdp.evaluate("document.querySelector('#run-button').click()");
    await waitFor(`document.querySelector('#job-id').textContent!==${JSON.stringify(previousJobLabel)}`);
    await waitFor("document.querySelector('#job-status').textContent==='Complete' || document.querySelector('#job-status').textContent==='Failed'", 180000);
    allowedDate = null;
    assert(await cdp.evaluate("document.querySelector('#job-status').textContent==='Complete' && document.querySelector('#job-id').textContent.includes('Reused verified cached results')"), 'real UI submission reused verified cache ' + date);
    await waitFor(`document.querySelector('#result-run-id').textContent===${JSON.stringify(id)} && document.querySelectorAll('.mode-card').length===4`);
    const rendered = await cdp.evaluate("JSON.parse(document.querySelector('#summary-json').textContent)");
    assert(JSON.stringify(rendered) === JSON.stringify(detail.summary), 'DOM summary exactly matches published JSON ' + date);
    assert(await cdp.evaluate("document.querySelector('#validator-status').textContent==='Validation passed' && !document.querySelector('#error').textContent"), 'validation status and no UI error ' + date);
    assert(JSON.stringify((await json('/api/runs')).runs.map(run => run.run_id).sort()) === JSON.stringify(runsBefore), 'cache replay created no new run ' + date);
    for (const [mode, value] of Object.entries(detail.summary.modes)) {
      assert(value.matched_count + value.unmatched_count === detail.summary.performed_trip_count, 'assignment conservation ' + date + ' / ' + mode);
      assert(await cdp.evaluate(`(() => {const card=document.querySelector('[data-mode=${mode}]');return card && [...card.querySelectorAll('.counts strong')].map(el=>Number(el.textContent.replaceAll(',',''))).join(',')===${JSON.stringify([value.matched_count,value.unmatched_count].join(','))} && card.querySelector('a')?.href.includes('mode=${mode}');})()`), 'mode card exact assigned/unassigned counts and ZIP ' + date + ' / ' + mode);
    }
    await cdp.evaluate("document.querySelector('#result-panel').scrollIntoView({block:'start'})");
    await screenshot(`${date}-results-mobile.png`, true);
    assert(await cdp.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2"), 'mobile results no page-level overflow ' + date);
    await screenshot(`${date}-results-desktop.png`);
    for (const format of ['csv','parquet','json']) {
      await set('download-filter', format);
      assert(await cdp.evaluate(`document.querySelectorAll('#downloads a').length>0 && [...document.querySelectorAll('#downloads a')].every(link=>link.textContent.endsWith('.${format}'))`), 'download filter ' + date + ' / ' + format);
    }
    await set('download-filter','all');
    const downloads = [];
    for (const file of ['summary.json','assignments.parquet',...['global','early','middle','late'].map(mode=>`tides/${mode}/trips_performed.csv`)]) downloads.push(await downloadFile(detail,file));
    const zips = [];
    for (const mode of ['global','early','middle','late']) {
      const response = await fetch(origin + `/api/runs/${encodeURIComponent(id)}/download?mode=${mode}`), payload = Buffer.from(await response.arrayBuffer());
      assert(response.ok && response.headers.get('content-type') === 'application/zip' && payload.subarray(0,2).toString() === 'PK' && payload.includes(Buffer.from('datapackage.json')) && payload.includes(Buffer.from('trips_performed.csv')), 'validated complete TIDES ZIP ' + date + ' / ' + mode, { bytes:payload.length });
      zips.push({ mode, bytes:payload.length, sha256:createHash('sha256').update(payload).digest('hex') });
    }
    reports.push({ run_id:id, service_date:date, feed:detail.run_manifest.gtfs_feed, cache_key:detail.run_manifest.cache_key, performed_trip_count:detail.summary.performed_trip_count, modes:detail.summary.modes, downloads, zips });
    process.stdout.write(JSON.stringify({ progress:'completed_cache_browser_acceptance',service_date:date,checks:checks.length })+'\n');
  }
  assert(external.length === 0, 'browser made no external requests');
  assert(errors.length === 0, 'browser has no JavaScript or unexpected HTTP errors', errors);
  assert(blocked.length === 0, 'browser attempted no unauthorized write or external request', blocked);
  assert(requests.filter(request=>request.method==='POST').length === runIds.length, 'only one non-force validated-cache POST per specified run');
} catch (error) { failure = String(error.stack || error); }
finally {
  if (cdp) { try { await cdp.send('Browser.close'); } catch {} cdp.socket.close(); }
  if (browser) browser.kill('SIGTERM');
  const report = { schema:'experiment05-browser-acceptance-v1', status:failure?'FAIL':'PASS', origin, run_ids:runIds, checks, errors, warnings, external, blocked, browser_requests:requests, runs:reports, failure:failure||null, force_recompute_performed:false, expected_invalid_date_post:true, created_at:new Date().toISOString() };
  await writeFile(join(output,'browser-acceptance.json'), JSON.stringify(report,null,2)+'\n', { mode:0o600 });
  process.stdout.write(JSON.stringify({ status:report.status, checks:checks.length, runs:reports.length, output })+'\n');
}
if (failure) { process.stderr.write(failure+'\n'); process.exitCode=1; }
