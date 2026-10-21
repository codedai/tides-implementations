/** VehicleID-only UI acceptance, exclusively against disposable localhost:8768.
 * node tools/merge_review/tests/browser_vehicle_stream_smoke.mjs --allow-test-writes
 * Creates one test run and a run-scoped review; existing run notes stay unchanged.
 * Uses local Chrome/CDP, blocks external requests, needs no browser dependency.
 */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const existingRun = option('--existing-run', null);
const appUrl = new URL(option('--url', 'http://127.0.0.1:8768'));
if (appUrl.origin !== 'http://127.0.0.1:8768' || !args.includes('--allow-test-writes')) {
  throw new Error('Use only disposable http://127.0.0.1:8768 with --allow-test-writes.');
}
const output = resolve(option('--output', 'artifacts/merge-debug-20231106/vehicle-stream-browser-acceptance'));
await mkdir(output, { recursive: true, mode: 0o700 }); await chmod(output, 0o700);
const profile = await mkdtemp(join(tmpdir(), 'vehicle-stream-chrome-'));
const startedAt = new Date().toISOString();
const checks = [], consoleErrors = [], runtimeErrors = [], externalRequests = [], responseErrors = [], requests = [], dialogs = [];
let browser, cdp, failure = null, summary = {};
function assert(condition, name, details) {
  checks.push({ name, passed: Boolean(condition), ...(details === undefined ? {} : { details }) });
  if (!condition) throw new Error(`Acceptance check failed: ${name}`);
}
class CDP {
  constructor(socket) {
    this.socket = socket; this.id = 0; this.pending = new Map(); this.handlers = new Map();
    socket.addEventListener('message', ({ data }) => {
      const message = JSON.parse(data);
      if (message.id) {
        const pending = this.pending.get(message.id); if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error))); else pending.resolve(message.result);
      } else for (const handler of this.handlers.get(message.method) || []) Promise.resolve(handler(message.params)).catch(error => runtimeErrors.push(String(error)));
    });
  }
  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((yes, no) => { socket.addEventListener('open', yes, { once: true }); socket.addEventListener('error', no, { once: true }); });
    return new CDP(socket);
  }
  on(method, handler) { this.handlers.set(method, [...(this.handlers.get(method) || []), handler]); }
  send(method, params = {}) {
    const id = ++this.id;
    const result = new Promise((resolveResponse, reject) => this.pending.set(id, { resolve: resolveResponse, reject }));
    this.socket.send(JSON.stringify({ id, method, params })); return result;
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value;
  }
}
async function waitFor(expression, timeout = 45000) {
  const started = Date.now(); let lastLog = started;
  while (Date.now() - started < timeout) {
    const value = await cdp.evaluate(expression); if (value) return value;
    if (Date.now() - lastLog > 30000) { console.log(JSON.stringify({ waiting_seconds: Math.round((Date.now() - started) / 1000), job: await cdp.evaluate("document.querySelector('#debug-job-message')?.textContent") })); lastLog = Date.now(); }
    await delay(120);
  }
  throw new Error(`Timeout waiting for ${expression}`);
}
async function click(selector) {
  if (selector === '#observation-first' && await cdp.evaluate("document.querySelector('#observation-first')?.disabled")) {
    assert(await cdp.evaluate("document.querySelector('#observation-page').value") === '1', 'already at first source page');
    return;
  }
  assert(await cdp.evaluate(`(() => {const e=document.querySelector(${JSON.stringify(selector)});if(!e||e.disabled)return false;if(e.click)e.click();else e.dispatchEvent(new MouseEvent('click',{bubbles:true}));return true})()`), `click ${selector}`);
}
async function setValue(selector, value) {
  assert(await cdp.evaluate(`(() => {const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;e.value=${JSON.stringify(String(value))};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`), `set ${selector}`);
}
async function read(path) { const response = await fetch(new URL(path, appUrl)); assert(response.ok, `GET ${path.split('?')[0]}`); return response.json(); }
async function ready() { await waitFor("!document.querySelector('#case-workspace').hidden && document.querySelector('#case-loading').hidden"); }
async function runId() { return cdp.evaluate("document.querySelector('#debug-run-select').value"); }
async function selectRun(id) {
  await setValue('#debug-run-select', id);
  await waitFor(`document.querySelector('#run-label').textContent.includes(${JSON.stringify(id)}) && document.querySelector('#case-loading').hidden`); await ready();
}
async function screenshot(name, width = 1500, height = 1050, mobile = false) {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile }); await delay(250);
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false, fromSurface: true });
  await writeFile(join(output, name), Buffer.from(shot.data, 'base64'), { mode: 0o600 });
}
async function startBrowser() {
  browser = spawn(option('--chrome', 'google-chrome'), [
    '--headless=new','--no-sandbox','--disable-dev-shm-usage','--disable-background-networking','--disable-component-update',
    '--disable-domain-reliability','--disable-extensions','--disable-sync','--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication',
    '--metrics-recording-only','--no-first-run','--no-default-browser-check','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
    '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1',`--user-data-dir=${profile}`,'about:blank',
  ], { stdio: ['ignore','ignore','pipe'] });
  let log = '';
  const websocket = await new Promise((yes, no) => {
    const timer = setTimeout(() => no(new Error(`Chrome startup timeout: ${log.slice(-2000)}`)), 15000);
    browser.stderr.on('data', value => { log += value; const found = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if(found){clearTimeout(timer);yes(found[1]);} });
    browser.on('error', error => { clearTimeout(timer); no(error); });
  });
  const origin = websocket.replace(/^ws:/, 'http:').split('/devtools/')[0];
  const targets = await fetch(`${origin}/json/list`).then(response => response.json());
  cdp = await CDP.connect(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  cdp.on('Runtime.consoleAPICalled', ({ type, args: values }) => { if(type==='error')consoleErrors.push(values.map(value=>value.value??value.description).join(' ')); });
  cdp.on('Runtime.exceptionThrown', value => runtimeErrors.push(value));
  cdp.on('Page.javascriptDialogOpening', async value => { dialogs.push({ ...value, accepted: false }); await cdp.send('Page.handleJavaScriptDialog',{accept:false}); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({url:request.url,method:request.method,postData:request.url.endsWith('/api/debug/runs')?request.postData:undefined}); if(/^https?:/.test(request.url)&&new URL(request.url).origin!==appUrl.origin)externalRequests.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if(response.status>=400)responseErrors.push({url:response.url,status:response.status}); });
  cdp.on('Fetch.requestPaused', async ({ requestId, request }) => { const allowed=/^(data:|blob:|about:)/.test(request.url)||(/^https?:/.test(request.url)&&new URL(request.url).origin===appUrl.origin); await cdp.send(allowed?'Fetch.continueRequest':'Fetch.failRequest',{requestId,...(allowed?{}:{errorReason:'BlockedByClient'})}); });
  for(const method of ['Runtime.enable','Network.enable','Page.enable'])await cdp.send(method);
  await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*'}]});
  await cdp.send('Emulation.setDeviceMetricsOverride',{width:1500,height:1050,deviceScaleFactor:1,mobile:false});
}

async function runChecks() {
  const debug = await read('/api/debug');
  assert(debug.enabled && debug.date === '2023-11-06', 'fixed single-day workspace');
  const baseline = debug.runs.find(run => run.config?.method && run.config.method !== 'vehicle_only');
  assert(Boolean(baseline), 'an existing matching run is available');
  const base = baseline.id || baseline.run_id;
  const oldReviews = await read(`/api/reviews/export?run=${encodeURIComponent(base)}`);
  await cdp.send('Page.navigate',{url:appUrl.href}); await ready();
  if(await runId()!==base)await selectRun(base);
  assert(await cdp.evaluate("document.querySelector('#method-select').value!=='vehicle_only'"), 'starts from actual matching representation');
  await setValue('#debug-method','vehicle_only');
  assert(await cdp.evaluate("[...document.querySelectorAll('#debug-segmentation-parameters input,#debug-segmentation-parameters select,#debug-merge-parameters input,#debug-merge-parameters select')].every(e=>e.disabled)"), 'split and merge parameters are disabled in VehicleID-only mode');
  if (existingRun) {
    const resumed = existingRun === 'latest'
      ? debug.runs.filter(run => run.config?.method === 'vehicle_only').at(-1)
      : debug.runs.find(run => (run.id || run.run_id) === existingRun);
    assert(resumed?.config?.method === 'vehicle_only', 'resume an existing completed test stream without another experiment POST');
    await selectRun(resumed.id || resumed.run_id);
  } else {
    await setValue('#debug-label',`VehicleID stream browser ${startedAt}`);
    await click('#debug-submit');
    await waitFor("document.querySelector('#debug-job-title').textContent.includes('Reprocessing complete') || document.querySelector('#debug-job-title').textContent.includes('Reprocessing failed')",600000);
    assert(await cdp.evaluate("document.querySelector('#debug-job-title').textContent.includes('Reprocessing complete')"), 'pure source stream experiment completes');
    await waitFor(`document.querySelector('#debug-run-select').value!==${JSON.stringify(base)} && document.querySelector('#case-loading').hidden`); await ready();
    const experimentPosts = requests.filter(request=>request.method==='POST'&&request.url.endsWith('/api/debug/runs'));
    assert(experimentPosts.length===1,'exactly one stream experiment submitted');
    const submitted = JSON.parse(experimentPosts[0].postData);
    assert(submitted.config.method==='vehicle_only' && Object.keys(submitted.config.segmentation||{}).length===0 && Object.keys(submitted.config.merge||{}).length===0,'submitted stream config has no active segmentation or merge thresholds');
  }
  const current = await runId();
  const meta = await read(`/api/meta?run=${encodeURIComponent(current)}`);
  assert(meta.methods.length===1 && meta.methods[0]==='vehicle_only' && meta.representation==='vehicle_observation_stream','metadata identifies pure vehicle stream');
  assert(meta.trip_segmentation_used===false && meta.observation_matching_used===false,'metadata explicitly disables both segmentation and matching');
  const vehicle = await cdp.evaluate("document.querySelector('#vehicle-select').value");
  const params = new URLSearchParams({run:current,method:'vehicle_only',vehicle});
  const catalog = await read(`/api/catalog?${params}`);
  assert(catalog.cases.length===1 && catalog.cases[0].kind==='vehicle_stream','exactly one source case per vehicle-day, no inferred trip or APC orphan bucket');
  const caseId = catalog.cases[0].id; params.set('case_id',caseId);
  const detail = await read(`/api/case?${params}`);
  const observations = detail.observations;
  assert(observations.length===detail.avl.length+detail.apc.length,'UNION ALL conserves every source row');
  assert(observations.every((row,index)=>row.display_order===index),'persisted display order is complete and stable');
  assert(new Set(observations.map(row=>`${row.source}|${row.rid}`)).size===observations.length,'no duplicate source identities in returned stream');
  assert(detail.counts.duplicate_source_identities===0 && detail.provenance.deduplication_performed===false,'raw duplicate payloads are not silently deduplicated');
  for(const source of ['avl','apc'])assert(observations.filter(row=>row.source===source).length===detail[source].length,`${source} source count preserved`);
  assert(detail.apc.every(row=>row.status==='not_evaluated' && row.progress_m===null && !['witnesses','score','segment_ids','ping_ids'].some(key=>Object.hasOwn(row.decision,key))),'all APC rows remain unevaluated without witness, score, owner or GPS projection');
  assert(detail.segments.length===0 && [...detail.avl,...detail.apc].every(row=>row.in_focus&&!row.is_segment_member),'all rows are in-focus source evidence but none becomes a trip member');
  assert(detail.window.start===meta.scope.start && detail.window.end===meta.scope.end,'case spans the full day, not only AVL coverage');
  assert(observations.every(row=>row.source_rid===row.rid && row.time_basis===row.event_time_basis && row.source_fields),'every event exposes original source identity, clock basis and fields');
  assert(meta.json_integer_encoding && detail.provenance.json_integer_encoding,'large-integer display encoding is explicitly disclosed');
  await cdp.send('Page.reload',{ignoreCache:true}); await ready();
  const firstTimed = observations.find(row=>Number.isFinite(row.event_time)&&row.event_time>=meta.scope.start&&row.event_time<meta.scope.end);
  const firstHourStart = Math.max(meta.scope.start,Math.min(firstTimed.event_time,meta.scope.end-3600));
  const firstClock = new Date(firstHourStart*1000).toISOString().slice(11,16);
  const lastClock = new Date((firstHourStart+3600)*1000).toISOString().slice(11,16);
  assert(await cdp.evaluate("document.querySelector('#window-size').value")==='3600','fresh stream page defaults to one-hour graphical view');
  assert(await cdp.evaluate(`document.querySelector('#window-label').textContent.includes(${JSON.stringify(firstClock)})&&document.querySelector('#window-label').textContent.includes(${JSON.stringify(lastClock)})`),'default graphical hour starts at first observed source time, not an empty day prefix');

  await waitFor("!document.querySelector('#observation-stream-panel').hidden && document.querySelector('#observation-table-body tr[data-rid]')");
  assert(await cdp.evaluate("!document.querySelector('#observation-window-only').checked"),'source table starts with the whole day, not the current chart slice');
  await setValue('#observation-page-size','100');
  await click('#observation-first');
  assert(await cdp.evaluate("Number(document.querySelector('#observation-table-body tr[data-rid]').dataset.displayOrder)")===0,'first source page begins with first persisted observation');
  assert(observations.length>100,'real vehicle stream exercises pagination');
  await click('#observation-next');
  assert(await cdp.evaluate("Number(document.querySelector('#observation-table-body tr[data-rid]').dataset.displayOrder)")===100,'next page continues exact source order without skipping');
  await click('#observation-last');
  const finalOrder = await cdp.evaluate("Number([...document.querySelectorAll('#observation-table-body tr[data-rid]')].at(-1).dataset.displayOrder)");
  assert(finalOrder===observations.length-1,'last raw source observation is reachable through pagination');
  assert(await cdp.evaluate("document.querySelector('#observation-next').disabled"),'last page has no phantom next page');
  const last = observations.at(-1);
  await setValue('#observation-search',last.rid);
  await waitFor(`document.querySelectorAll('#observation-table-body tr[data-rid]').length===1 && document.querySelector('#observation-table-body tr[data-rid]').dataset.rid===${JSON.stringify(last.rid)}`);
  assert(true,'exact source-ID search reaches the last row independently of chart window');
  await setValue('#observation-search','');
  const allFilter = await cdp.evaluate("document.querySelector('#observation-source-filter').value");
  for(const source of ['avl','apc']){
    await setValue('#observation-source-filter',source);
    await waitFor(`document.querySelector('#observation-table-body tr[data-rid]')?.dataset.source===${JSON.stringify(source)}`);
    assert(await cdp.evaluate(` [...document.querySelectorAll('#observation-table-body tr[data-rid]')].every(row=>row.dataset.source===${JSON.stringify(source)})`),`${source} filter retains correct source identities`);
  }
  await setValue('#observation-source-filter',allFilter);
  await setValue('#window-size','full');
  const validSource = source => observations.find(row=>row.source===source && Number.isFinite(row.event_time)&&Number.isFinite(row.lat)&&Number.isFinite(row.lon)&&Math.abs(row.lat)>1);
  const chosenApc = validSource('apc'), chosenAvl = validSource('avl');
  assert(chosenApc&&chosenAvl,'both real sources have selectable timed geographic rows');
  for(const row of [chosenApc,chosenAvl]){
    await setValue('#observation-search',row.rid);
    await waitFor(`document.querySelector('#observation-table-body tr[data-rid]')?.dataset.rid===${JSON.stringify(row.rid)}`);
    await click(`#observation-table-body tr[data-rid="${row.rid}"]`);
    assert(await cdp.evaluate(`document.querySelector('#inspector').textContent.includes(${JSON.stringify(row.rid)})`),`${row.source} source click shows original rid in inspector`);
    assert(await cdp.evaluate(`document.querySelector('#inspector .record-heading').textContent.includes(${JSON.stringify(row.source.toUpperCase())})`),`${row.source} inspector does not relabel source identity`);
    assert(await cdp.evaluate(`document.querySelector('#observation-table-body tr.selected')?.dataset.source===${JSON.stringify(row.source)}`),`${row.source} selected table state identifies source`);
    const originalTime = row.source==='avl' ? row.time : (row.arrival ?? row.departure);
    const readableOriginalTime = new Date(originalTime*1000).toISOString().slice(0,19).replace('T',' ');
    assert(await cdp.evaluate(`document.querySelector('#observation-table-body tr.selected td:last-child').textContent.includes(${JSON.stringify(readableOriginalTime)})`),`${row.source} original-time table column shows a readable complete local date and clock, not epoch seconds`);
    const originalTimeLabel = row.source==='avl' ? 'Source location time' : (row.arrival==null ? 'Source departure' : 'Source arrival');
    assert(await cdp.evaluate(`[...document.querySelectorAll('#inspector .record-dl dt')].find(e=>e.textContent===${JSON.stringify(originalTimeLabel)})?.nextElementSibling?.textContent.includes(${JSON.stringify(readableOriginalTime)})`),`${row.source} inspector original timestamp is human-readable without timezone conversion`);
    assert(typeof row.source_fields.payload_hash==='string' && /^\d+$/.test(row.source_fields.payload_hash),`${row.source} UInt64 payload hash is encoded as an exact decimal string`);
    const inspectorFields = await cdp.evaluate("JSON.parse(document.querySelector('#inspector pre').textContent)");
    assert(inspectorFields.payload_hash===row.source_fields.payload_hash,`${row.source} inspector preserves the exact API payload hash without JS rounding`);
    if(row.source==='apc'){
      assert(await cdp.evaluate("document.querySelector('#selected-status').textContent.includes('Matching not performed')"),'APC inspector clearly says matching was not performed');
      assert(await cdp.evaluate(`!document.querySelector('#progress-svg [data-rid="${row.rid}"]')`),'APC is not projected onto GPS progress');
      assert(await cdp.evaluate(` [...document.querySelectorAll('#map-svg [data-rid="${row.rid}"]')].some(e=>e.getAttribute('fill')==='#8296a4')`),'APC geographic observation has neutral unmatched-evaluation styling');
      assert(await cdp.evaluate(`(() => {const root=document.querySelector('#map-svg'),dot=root.querySelector('[data-rid="${row.rid}"][data-selected="true"]'),v=root.viewBox.baseVal;return dot&&Math.abs(Number(dot.getAttribute('cx'))-v.width/2)<1&&Math.abs(Number(dot.getAttribute('cy'))-v.height/2)<1})()`),'APC click centers the map on its own raw coordinates, not nearest AVL');
    }else{
      const clock = new Date(row.event_time*1000).toISOString().slice(11,19);
      assert(await cdp.evaluate(`document.querySelector('#scrubber-time').textContent.includes(${JSON.stringify(clock)})`),'AVL source click positions the actual observation time cursor');
    }
  }
  assert(await cdp.evaluate("document.querySelector('.progress-panel .chart-caption').textContent.includes('APC is not projected')"),'progress chart explicitly excludes APC projection');
  await setValue('#observation-search',''); await click('#observation-first');
  await cdp.evaluate("document.querySelector('#observation-stream-panel').scrollIntoView({block:'start'})"); await screenshot('vehicle-source-stream.png');
  await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})"); await screenshot('vehicle-time-progress.png');
  await setValue('#debug-base-select',base);
  const diff = await read(`/api/debug/compare?base=${encodeURIComponent(base)}&run=${encodeURIComponent(current)}&vehicle=${encodeURIComponent(vehicle)}`);
  assert(diff.comparable===false && diff.not_comparable===true && diff.summary===null,'stream versus matching correctly refuses algorithm-quality comparison');
  await waitFor("document.querySelector('#debug-diff-status').textContent.includes('not comparable') || document.querySelector('#debug-diff-status').textContent.includes('Not matching performance') || document.querySelector('#debug-diff-status').textContent.includes('Matching not performed')");
  assert(await cdp.evaluate("!document.querySelector('#debug-diff-summary .diff-stat')"),'incomparable representations do not show fabricated improvement statistics');
  const hiddenBoundary = await cdp.evaluate("(() => {const svg=document.querySelector('#debug-boundary-svg');return {display:getComputedStyle(svg).display,height:svg.getBoundingClientRect().height,hiddenAttribute:svg.hasAttribute('hidden')}})()");
  assert(hiddenBoundary.height===0,'incomparable boundary SVG is actually removed from layout, not merely assigned a JS hidden property',hiddenBoundary);
  await cdp.evaluate("document.querySelector('#debug-diff').scrollIntoView({block:'start'})"); await screenshot('incomparable-representations.png');
  const note = `Disposable VehicleID stream review ${startedAt}`;
  await setValue('#review-notes',note); await click('#review-verdict input[value="uncertain"]'); await click('#save-review');
  await waitFor("!document.querySelector('#review-save-state').classList.contains('dirty')");
  const notes = await read(`/api/reviews/export?run=${encodeURIComponent(current)}`);
  assert(notes.reviews.some(row=>row.case_id===caseId&&row.notes===note),'vehicle source case supports run-scoped review persistence');
  await selectRun(base);
  assert(await cdp.evaluate("document.querySelector('#method-select').value!=='vehicle_only' && document.querySelector('#observation-stream-panel').hidden"),'old matching representation remains usable after stream inspection');
  const afterReviews = await read(`/api/reviews/export?run=${encodeURIComponent(base)}`);
  assert(JSON.stringify(afterReviews.reviews)===JSON.stringify(oldReviews.reviews),'existing matching review notes remain exactly unchanged');
  await selectRun(current); await cdp.send('Page.reload',{ignoreCache:true}); await ready();
  await waitFor(`document.querySelector('#review-notes').value===${JSON.stringify(note)}`);
  assert(true,'stream review survives real page reload');
  await cdp.evaluate('window.scrollTo(0,0)'); await screenshot('vehicle-stream-desktop.png');
  await cdp.evaluate("document.querySelector('#observation-stream-panel').scrollIntoView({block:'start'})"); await screenshot('vehicle-stream-mobile.png',390,844,true);
  assert(await cdp.evaluate('document.documentElement.scrollWidth<=window.innerWidth+2'),'mobile source stream has no document overflow');
  assert(await cdp.evaluate("[...document.querySelectorAll('svg *')].every(e=>[...e.attributes].every(a=>!/\\b(NaN|Infinity)\\b/.test(a.value)))"),'SVG geometry stays finite');
  assert(externalRequests.length===0,'no external page requests',externalRequests);
  assert(consoleErrors.length===0,'no console errors',consoleErrors);
  assert(runtimeErrors.length===0,'no uncaught runtime errors',runtimeErrors);
  assert(responseErrors.length===0,'no HTTP errors',responseErrors);
  summary={base,current,vehicle,caseId,observations:observations.length,avl:detail.avl.length,apc:detail.apc.length,matchingPerformed:false,segmentationPerformed:false,resumed:existingRun,experimentPosts:requests.filter(request=>request.method==='POST'&&request.url.endsWith('/api/debug/runs')).length};
}
try { await startBrowser(); await runChecks(); }
catch(error){failure=String(error.stack||error);if(cdp)try{await screenshot('failure.png');}catch{}}
finally{
  if(cdp)cdp.socket.close();if(browser)browser.kill('SIGTERM');
  await writeFile(join(output,'browser-vehicle-stream-results.json'),JSON.stringify({startedAt,completedAt:new Date().toISOString(),url:appUrl.href,checks,summary,failure,consoleErrors,runtimeErrors,externalRequests,responseErrors,requests,dialogs,profile,note:'Source-conservation and UI verification, not matching accuracy evaluation.'},null,2),{mode:0o600});
  console.log(JSON.stringify({passed:!failure,checks:checks.length,output,failure},null,2));if(failure)process.exitCode=1;
}
