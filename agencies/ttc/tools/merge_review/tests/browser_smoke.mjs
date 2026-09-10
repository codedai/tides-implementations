/**
 * Dependency-free local-browser acceptance check.
 *
 * Start the app with a dedicated, disposable review database before running:
 *   node tools/merge_review/tests/browser_smoke.mjs --url http://127.0.0.1:8765 \
 *     --output artifacts/merge-review-website-20260908/browser --final-ui
 * Pass --allow-test-writes ONLY for an explicitly disposable review store.
 *
 * Node >=22 and a locally installed Chrome are the only dependencies. No package
 * install, remote script, map tile, or other external request is necessary.
 * Generated screenshots/results are private. Source/frozen evidence is not edited.
 */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2);
function option(name, fallback) {
  const index = args.indexOf(name);
  return index < 0 ? fallback : args[index + 1];
}
const appUrl = new URL(option('--url', 'http://127.0.0.1:8765'));
if (!['127.0.0.1', 'localhost', '[::1]'].includes(appUrl.hostname)) {
  throw new Error('This test only accepts a loopback app URL.');
}
const output = resolve(option('--output', 'artifacts/merge-review-website-20260908/browser'));
const allowTestWrites = args.includes('--allow-test-writes');
await mkdir(output, { recursive: true, mode: 0o700 });
await chmod(output, 0o700);
const profile = await mkdtemp(join(tmpdir(), 'merge-review-chrome-'));
const checks = [];
const consoleErrors = [];
const runtimeErrors = [];
const networkRequests = [];
const externalRequests = [];
const responseErrors = [];
const dialogs = [];
let apiSummary = null;
let apiData = null;
let browser;
let cdp;

function assert(condition, name, details = undefined) {
  checks.push({ name, passed: Boolean(condition), ...(details === undefined ? {} : { details }) });
  if (!condition) throw new Error(`Acceptance check failed: ${name}`);
}

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    socket.addEventListener('message', ({ data }) => {
      const message = JSON.parse(data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result);
      } else {
        for (const callback of this.handlers.get(message.method) || []) {
          Promise.resolve(callback(message.params)).catch(error => runtimeErrors.push(String(error)));
        }
      }
    });
    socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) pending.reject(new Error('Chrome socket closed'));
      this.pending.clear();
    });
  }
  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolveConnection, rejectConnection) => {
      socket.addEventListener('open', resolveConnection, { once: true });
      socket.addEventListener('error', rejectConnection, { once: true });
    });
    return new CDP(socket);
  }
  on(method, callback) {
    this.handlers.set(method, [...(this.handlers.get(method) || []), callback]);
  }
  async send(method, params = {}) {
    const id = ++this.id;
    const response = new Promise((resolveResponse, rejectResponse) => {
      this.pending.set(id, { resolve: resolveResponse, reject: rejectResponse });
    });
    this.socket.send(JSON.stringify({ id, method, params }));
    return response;
  }
  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
  close() { this.socket.close(); }
}

async function waitFor(expression, timeout = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const value = await cdp.evaluate(expression);
    if (value) return value;
    await delay(120);
  }
  throw new Error(`Timeout waiting for ${expression}`);
}

async function click(selector) {
  const found = await cdp.evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element || element.disabled) return false;
    if (typeof element.click === 'function') element.click();
    else element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return true;
  })()`);
  assert(found, `click ${selector}`);
}

async function setValue(selector, value) {
  return cdp.evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    element.value = ${JSON.stringify(value)};
    element.dispatchEvent(new Event('input', {bubbles:true}));
    element.dispatchEvent(new Event('change', {bubbles:true}));
    return true;
  })()`);
}

async function screenshot(name, width, height, mobile = false) {
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width, height, deviceScaleFactor: 1, mobile,
  });
  await delay(350);
  const shot = await cdp.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: false, fromSurface: true,
  });
  await writeFile(join(output, name), Buffer.from(shot.data, 'base64'), { mode: 0o600 });
}

async function startBrowser() {
  browser = spawn(option('--chrome', 'google-chrome'), [
    '--headless=new',
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-domain-reliability',
    '--disable-extensions',
    '--disable-sync',
    '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication',
    '--metrics-recording-only',
    '--no-first-run',
    '--no-default-browser-check',
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=0',
    '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1',
    `--user-data-dir=${profile}`,
    'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let logs = '';
  const websocketUrl = await new Promise((resolveBrowser, rejectBrowser) => {
    const timer = setTimeout(() => rejectBrowser(new Error(`Chrome startup timeout: ${logs.slice(-2000)}`)), 15000);
    browser.stderr.on('data', data => {
      logs += data.toString();
      const match = logs.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) { clearTimeout(timer); resolveBrowser(match[1]); }
    });
    browser.on('error', error => { clearTimeout(timer); rejectBrowser(error); });
    browser.on('exit', code => {
      clearTimeout(timer);
      rejectBrowser(new Error(`Chrome exited ${code}: ${logs.slice(-2000)}`));
    });
  });
  const discoveryOrigin = websocketUrl.replace(/^ws:/, 'http:').split('/devtools/')[0];
  const targets = await fetch(`${discoveryOrigin}/json/list`).then(response => response.json());
  const target = targets.find(item => item.type === 'page');
  cdp = await CDP.connect(target.webSocketDebuggerUrl);
  cdp.on('Runtime.consoleAPICalled', ({ type, args: values }) => {
    if (type === 'error') consoleErrors.push(values.map(value => value.value ?? value.description).join(' '));
  });
  cdp.on('Runtime.exceptionThrown', ({ exceptionDetails }) => runtimeErrors.push(exceptionDetails));
  cdp.on('Page.javascriptDialogOpening', async ({ type, message }) => {
    dialogs.push({ type, message, accepted: false });
    await cdp.send('Page.handleJavaScriptDialog', { accept: false });
  });
  cdp.on('Network.requestWillBeSent', ({ request }) => {
    networkRequests.push({ url: request.url, method: request.method });
    if (/^https?:/.test(request.url) && new URL(request.url).origin !== appUrl.origin) {
      externalRequests.push(request.url);
    }
  });
  cdp.on('Network.responseReceived', ({ response }) => {
    if (response.status >= 400) responseErrors.push({ url: response.url, status: response.status });
  });
  // Refuse external requests before they leave the inspected page.
  cdp.on('Fetch.requestPaused', async ({ requestId, request }) => {
    const isLocal = /^https?:/.test(request.url) && new URL(request.url).origin === appUrl.origin;
    const isInternal = /^(data:|blob:|about:)/.test(request.url);
    await cdp.send(isLocal || isInternal ? 'Fetch.continueRequest' : 'Fetch.failRequest', {
      requestId,
      ...(isLocal || isInternal ? {} : { errorReason: 'BlockedByClient' }),
    });
  });
  await cdp.send('Runtime.enable');
  await cdp.send('Network.enable');
  await cdp.send('Page.enable');
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false,
  });
}

async function runChecks() {
  await cdp.send('Page.navigate', { url: appUrl.href });
  await waitFor("document.readyState === 'complete'");
  await waitFor("!document.querySelector('#case-workspace').hidden && document.querySelector('#apc-table-body tr')", 45000);
  assert(await cdp.evaluate("['map-svg','timeline-svg','progress-svg'].every(id => document.getElementById(id).childElementCount > 3)"),
    'map, timeline, and progress SVGs contain rendered evidence');
  assert(await cdp.evaluate("!document.querySelector('#global-error').textContent.trim()"), 'no visible startup error');
  assert(await cdp.evaluate("document.querySelector('#map-mode').textContent.includes('离线')"), 'map starts in offline mode');
  assert(await cdp.evaluate("!document.querySelector('#map-svg image')"), 'no map tiles created by default');
  await click('#map-basemap');
  await delay(150);
  assert(await cdp.evaluate("document.querySelector('#map-basemap').getAttribute('aria-pressed') === 'false' && !document.querySelector('#map-svg image')"),
    'declining street-tile consent leaves map offline');
  const moderate = apiData.catalog.cases.find(row => row.kind === 'segment'
    && row.apc_count >= 5 && row.apc_count <= 80 && row.avl_count >= 50 && row.avl_count <= 1000);
  if (moderate) {
    await setValue('#case-search', moderate.id);
    await waitFor(`document.querySelector('#case-list .case-item[data-case-id=${JSON.stringify(moderate.id)}]')`);
    await click(`#case-list .case-item[data-case-id="${moderate.id}"]`);
    await waitFor("document.querySelector('#case-loading').hidden && !document.querySelector('#case-workspace').hidden");
    await delay(400);
    assert(await cdp.evaluate(`document.querySelector('#case-list .case-item.active')?.dataset.caseId === ${JSON.stringify(moderate.id)}`),
      'exact-ID queue search selects intended review unit');
  }
  await click('#apc-table-body tr[data-rid]');
  const selectedRid = await cdp.evaluate("document.querySelector('#apc-table-body tr.selected')?.dataset.rid");
  assert(Boolean(selectedRid), 'table click selects APC record');
  assert(await cdp.evaluate(`document.querySelector('#inspector').textContent.includes(${JSON.stringify(selectedRid)})`),
    'inspector displays selected source record ID');
  assert(await cdp.evaluate(`['map-svg','timeline-svg'].every(id => [...document.querySelectorAll('#'+id+' [data-rid]')].some(element => element.dataset.rid === ${JSON.stringify(selectedRid)}))`),
    'selected APC has linked map and timeline marks');
  if (args.includes('--final-ui')) {
    const linkedRids = await cdp.evaluate(`(() => {
      const maps = ['map-svg','timeline-svg','progress-svg'].map(id => new Set([...document.querySelectorAll('#'+id+' [data-rid]')].map(el => el.dataset.rid)));
      return [...maps[2]].filter(id => maps.every(set => set.has(id))).slice(0,3);
    })()`);
    assert(linkedRids.length > 0, 'at least one APC has selectable evidence in all three views');
    for (const [index, view] of ['map-svg', 'timeline-svg', 'progress-svg'].entries()) {
      const targetRid = linkedRids[index % linkedRids.length];
      await click(`#${view} [data-rid="${targetRid}"]`);
      assert(await cdp.evaluate(`document.querySelector('#apc-table-body tr.selected')?.dataset.rid === ${JSON.stringify(targetRid)}`),
        `${view} click selects matching APC table row`);
      assert(await cdp.evaluate(`['map-svg','timeline-svg','progress-svg'].every(id => {
        const selected = [...document.querySelectorAll('#'+id+' [data-selected="true"]')];
        return selected.length > 0 && selected.every(el => el.dataset.rid === ${JSON.stringify(targetRid)});
      })`), `${view} selection is visibly linked across all three views`);
    }
  }
  assert(await cdp.evaluate("[...document.querySelectorAll('svg *')].every(el => [...el.attributes].every(a => !/\\b(NaN|Infinity)\\b/.test(a.value)))"),
    'visualization geometry contains no NaN or Infinity');
  const beforeZoom = await cdp.evaluate("document.querySelector('#map-svg').innerHTML");
  await click('#map-zoom-in');
  assert(await cdp.evaluate("document.querySelector('#map-svg').innerHTML") !== beforeZoom, 'map zoom changes rendered geometry');
  await click('#map-fit');
  const cursorBefore = await cdp.evaluate("document.querySelector('#scrubber-time').textContent");
  await setValue('#time-scrubber', '777');
  await waitFor(`document.querySelector('#scrubber-time').textContent !== ${JSON.stringify(cursorBefore)}`);
  assert(await cdp.evaluate("document.querySelector('#scrubber-time').textContent") !== cursorBefore,
    'time scrubber updates linked clock readout');
  await screenshot('desktop.png', 1600, 1100);
  await screenshot('desktop-1440.png', 1440, 1050);
  await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})");
  await screenshot('desktop-timeline-progress.png', 1440, 1050);
  await cdp.evaluate('window.scrollTo(0,0)');
  if (args.includes('--final-ui')) {
    const longCase = apiData.catalog.cases.find(row => row.kind === 'segment'
      && row.end - row.start > 4 * 3600 && row.apc_count > 30);
    assert(Boolean(longCase), 'real-data stress case spans more than four hours');
    await setValue('#case-search', longCase.id);
    await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId === ${JSON.stringify(longCase.id)} && document.querySelector('#case-loading').hidden`);
    assert(await cdp.evaluate("document.querySelector('#window-size').value") === '3600',
      'long chunk opens in a bounded sixty-minute view');
    const sourceMetrics = await cdp.evaluate("['metric-avl','metric-apc','metric-support'].map(id=>document.getElementById(id).textContent)");
    await setValue('#window-size', '1800');
    const firstWindowLabel = await cdp.evaluate("document.querySelector('#window-label').textContent");
    await click('#window-next');
    assert(await cdp.evaluate("document.querySelector('#window-label').textContent") !== firstWindowLabel,
      'next thirty-minute window changes time range');
    await click('#window-previous');
    assert(await cdp.evaluate("document.querySelector('#window-label').textContent") === firstWindowLabel,
      'previous window restores exact time range');
    await click('#window-focus');
    assert(await cdp.evaluate("document.querySelector('#window-label').textContent.includes('局部浏览')"),
      'APC focus provides a local evidence view');
    assert(JSON.stringify(await cdp.evaluate("['metric-avl','metric-apc','metric-support'].map(id=>document.getElementById(id).textContent)")) === JSON.stringify(sourceMetrics),
      'window browsing does not change full-case counts or segmentation');
    const initialProvenance = await cdp.evaluate("JSON.parse(document.querySelector('#case-provenance').textContent)");
    const contextCounts = {};
    for (const context of [0, 900, 1800, 180]) {
      await setValue('#context-size', String(context));
      await waitFor("document.querySelector('#case-loading').hidden && !document.querySelector('#case-workspace').hidden");
      const provenance = await cdp.evaluate("JSON.parse(document.querySelector('#case-provenance').textContent)");
      assert(provenance.case_id === longCase.id && provenance.counts.apc_focus === initialProvenance.counts.apc_focus,
        `${context}s context reload preserves review-unit identity and focus rows`);
      assert(provenance.window.start === initialProvenance.window.start + 180 - context
          && provenance.window.end === initialProvenance.window.end - 180 + context,
        `${context}s context reload changes actual returned time bounds`);
      contextCounts[context] = provenance.counts.apc_context;
    }
    assert(contextCounts[0] <= contextCounts[180] && contextCounts[180] <= contextCounts[900]
        && contextCounts[900] <= contextCounts[1800],
      'wider context retains at least as many context APC observations', contextCounts);
    await screenshot('long-chunk-local-window.png', 1440, 1050);
    await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})");
    await screenshot('long-chunk-local-charts.png', 1440, 1050);
    await cdp.evaluate('window.scrollTo(0,0)');
  }

  await setValue('#case-search', '__qa_no_such_candidate__');
  await waitFor("document.querySelectorAll('#case-list .case-item').length === 0");
  assert(await cdp.evaluate("!document.querySelector('#no-case').hidden"), 'empty queue filter has explicit empty state');
  await setValue('#case-search', '');
  await waitFor("document.querySelectorAll('#case-list .case-item').length > 1 && document.querySelector('#case-loading').hidden");
  const beforeNext = await cdp.evaluate("document.querySelector('#case-list .case-item.active')?.dataset.caseId");
  await click('#next-case');
  await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId !== ${JSON.stringify(beforeNext)} && document.querySelector('#case-loading').hidden`);
  assert(await cdp.evaluate("document.querySelector('#case-list .case-item.active')?.dataset.caseId") !== beforeNext,
    'next-trip navigation changes review unit');
  await click('#previous-case');
  await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId === ${JSON.stringify(beforeNext)} && document.querySelector('#case-loading').hidden`);
  assert(true, 'previous-trip navigation restores review unit');
  await setValue('#status-filter', 'ambiguous');
  await waitFor("document.querySelector('#case-loading').hidden");
  const shownAmbiguousIds = await cdp.evaluate("[...document.querySelectorAll('#case-list .case-item')].map(el => el.dataset.caseId)");
  const expectedAmbiguous = new Set(apiData.catalog.cases.filter(row => row.status_counts.ambiguous > 0).map(row => row.id));
  assert(shownAmbiguousIds.length > 0 && shownAmbiguousIds.every(id => expectedAmbiguous.has(id)),
    'ambiguous-status queue filter matches persisted status counts');
  await setValue('#status-filter', '');
  await waitFor("document.querySelector('#case-loading').hidden && !document.querySelector('#case-workspace').hidden");
  const originalMethod = apiData.meta.default_method;
  await setValue('#method-select', originalMethod === 'gap_only' ? 'avl_context' : 'gap_only');
  await waitFor("document.querySelector('#case-loading').hidden && !document.querySelector('#case-workspace').hidden");
  await delay(500);
  assert(await cdp.evaluate("document.querySelector('#method-select').value") !== originalMethod,
    'segmentation method can be switched');
  await setValue('#method-select', originalMethod);
  await waitFor("document.querySelector('#case-loading').hidden && !document.querySelector('#case-workspace').hidden");
  await delay(500);
  if (allowTestWrites) {
    const note = `Browser QA only ${startedAt}: <img src=x onerror="window.__mergeReviewXss=true">`;
    const reviewId = await cdp.evaluate("document.querySelector('#case-list .case-item.active')?.dataset.caseId");
    await click('#review-verdict input[value="uncertain"]');
    await setValue('#review-notes', note);
    await cdp.evaluate("document.querySelector('#review-notes').focus()");
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'j', code: 'KeyJ' });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'j', code: 'KeyJ' });
    assert(await cdp.evaluate("document.querySelector('#case-list .case-item.active')?.dataset.caseId") === reviewId,
      'J shortcut does not navigate while editing notes');
    await setValue('#review-notes', note);
    await click('#next-case');
    await delay(150);
    assert(await cdp.evaluate("document.querySelector('#case-list .case-item.active')?.dataset.caseId") === reviewId,
      'declining discard confirmation preserves dirty review and current case');
    if (args.includes('--final-ui')) {
      const previousContext = await cdp.evaluate("document.querySelector('#context-size').value");
      await setValue('#context-size', '900');
      await delay(150);
      assert(await cdp.evaluate("document.querySelector('#context-size').value") === previousContext
          && await cdp.evaluate("document.querySelector('#review-notes').value") === note,
        'declining context reload preserves unsaved note and previous context choice');
    }
    await click('#save-review');
    await waitFor("!document.querySelector('#review-save-state').classList.contains('dirty')");
    await delay(350);
    const saved = await fetch(new URL('/api/reviews/export', appUrl)).then(response => response.json());
    assert(saved.reviews.some(row => row.case_id === reviewId && row.notes === note && row.verdict === 'uncertain'),
      'review verdict and harmless HTML-like note persist in dedicated test store');
    assert(saved.unblinded === true && saved.ground_truth === false,
      'review export is explicitly unblinded and not independent ground truth');
    await cdp.send('Page.reload', { ignoreCache: true });
    await waitFor("!document.querySelector('#case-workspace').hidden && document.querySelector('#case-loading').hidden");
    await delay(350);
    await setValue('#case-search', reviewId);
    await waitFor(`document.querySelector('#case-list .case-item[data-case-id=${JSON.stringify(reviewId)}]')`);
    await click(`#case-list .case-item[data-case-id="${reviewId}"]`);
    await waitFor(`document.querySelector('#review-notes').value === ${JSON.stringify(note)}`);
    assert(await cdp.evaluate('window.__mergeReviewXss !== true && !document.querySelector(\'img[src="x"]\')'),
      'saved HTML-like note is inert after reload');
    assert(await cdp.evaluate("document.querySelector('#review-verdict input:checked').value") === 'uncertain',
      'saved verdict survives page reload');
    await setValue('#case-search', '');
    await waitFor("document.querySelectorAll('#case-list .case-item').length > 1 && document.querySelector('#case-loading').hidden");
    await click('#save-next');
    await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId !== ${JSON.stringify(reviewId)} && document.querySelector('#case-loading').hidden`);
    assert(true, 'save-and-next persists then advances review queue');
  }
  await cdp.evaluate('window.scrollTo(0,0)');
  await screenshot('mobile.png', 390, 844, true);
  assert(await cdp.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"),
    'mobile page has no horizontal document overflow');
  await cdp.evaluate("document.querySelector('#map-stage').scrollIntoView({block:'start'})");
  await screenshot('mobile-map.png', 390, 844, true);
  await cdp.evaluate('window.scrollTo(0,0)');
  await screenshot('desktop-final.png', 1600, 1100);
  assert(externalRequests.length === 0, 'no external page requests', externalRequests);
  assert(consoleErrors.length === 0, 'no console errors', consoleErrors);
  assert(runtimeErrors.length === 0, 'no uncaught runtime errors', runtimeErrors);
  assert(responseErrors.length === 0, 'no HTTP error responses', responseErrors);
}

async function runApiChecks() {
  const read = async path => {
    const response = await fetch(new URL(path, appUrl));
    assert(response.ok, `API ${path.split('?')[0]} succeeds`);
    return response.json();
  };
  const meta = await read('/api/meta');
  assert(meta.methods.includes('avl_context') && meta.vehicles.length > 0, 'API exposes frozen methods and vehicles');
  const params = new URLSearchParams({ method: meta.default_method, vehicle: meta.default_vehicle });
  const catalog = await read(`/api/catalog?${params}`);
  assert(catalog.cases.length > 0, 'real-data review catalog is non-empty');
  const chosen = catalog.cases.find(row => row.kind === 'segment' && row.apc_count >= 3 && row.avl_count >= 3)
    ?? catalog.cases.find(row => row.apc_count > 0) ?? catalog.cases[0];
  params.set('case_id', chosen.id);
  const detail = await read(`/api/case?${params}`);
  assert(detail.avl.length === detail.counts.avl_returned, 'AVL payload count matches explicit count');
  assert(detail.apc.length === detail.counts.apc_returned, 'APC payload count matches explicit count');
  assert(new Set(detail.avl.map(row => row.rid)).size === detail.avl.length, 'no duplicate raw AVL IDs in detail');
  assert(new Set(detail.apc.map(row => row.rid)).size === detail.apc.length, 'no duplicate raw APC IDs in detail');
  const witnessIds = new Set();
  const visit = value => {
    if (Array.isArray(value)) value.forEach(visit);
    else if (value && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) {
        if (key === 'ping_ids' && Array.isArray(item)) item.forEach(id => witnessIds.add(id));
        else visit(item);
      }
    }
  };
  detail.apc.forEach(row => visit(row.decision));
  const availableIds = new Set(detail.avl.map(row => row.rid));
  assert([...witnessIds].every(id => availableIds.has(id)), 'all persisted positive/counter/candidate witnesses present in detail');
  assert(detail.provenance.gtfs_used === false && detail.provenance.matcher_recomputed === false,
    'detail reads frozen evidence without GTFS or matcher recomputation');
  assert(detail.provenance.trip_identity_verified === false && detail.provenance.counts_aggregated === false,
    'detail preserves trip and count uncertainty');
  apiSummary = {
    vehicleCount: meta.vehicles.length, methodCount: meta.methods.length,
    catalogCount: catalog.cases.length, detailAvlCount: detail.avl.length,
    detailApcCount: detail.apc.length, uniqueWitnessIds: witnessIds.size,
    caseKind: detail.case.kind, sampling: detail.provenance.sampling,
  };
  apiData = { meta, catalog, detail };
  return apiData;
}

let failure = null;
const startedAt = new Date().toISOString();
try {
  if (args.includes('--api-only')) {
    await runApiChecks();
  } else {
    await startBrowser();
  }
  if (args.includes('--browser-self-test')) {
    assert(await cdp.evaluate('2 + 2') === 4, 'Chrome DevTools connection works');
  } else if (!args.includes('--api-only')) {
    await runApiChecks();
    await runChecks();
  }
} catch (error) {
  failure = String(error.stack || error);
  if (cdp) {
    try { await screenshot('failure.png', 1600, 1100); } catch { /* original error wins */ }
  }
} finally {
  if (cdp) cdp.close();
  if (browser) browser.kill('SIGTERM');
  const result = {
    startedAt, completedAt: new Date().toISOString(), appUrl: appUrl.href,
    allowTestWrites, chromeProfile: profile, checks, consoleErrors, runtimeErrors,
    externalRequests, responseErrors, networkRequests, dialogs, apiSummary, failure,
    note: 'This is interaction/contract verification, not merge-accuracy evaluation.',
  };
  await writeFile(join(output, 'browser-results.json'), JSON.stringify(result, null, 2), { mode: 0o600 });
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, output, failure }, null, 2));
  if (failure) process.exitCode = 1;
}
