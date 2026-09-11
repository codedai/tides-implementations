/** Read-only real-browser acceptance: independent decision modes + full-day overview. */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2), option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
const run = option('--run', null), legacy = option('--legacy-run', 'run-20231106-20260909T155703-8358896eb468');
const skipLegacyGlobal = args.includes('--skip-legacy-global');
const vehicleLabel = option('--vehicle', '9028'), caseId = option('--case', 'direction:9028:0003');
if (!run) throw new Error('--run must name a completed four-mode run. No experiment or review writes are performed.');
const origin = option('--origin', 'http://127.0.0.1:8768'), output = resolve(option('--output', 'artifacts/merge-debug-20231106/partial-modes-browser'));
await mkdir(output, { recursive: true, mode: 0o700 });
const profile = await mkdtemp(join(tmpdir(), 'gtfs-modes-chrome-')), checks = [], errors = [], requests = [], external = [], summaries = [], performanceChecks = [];
let browser, cdp, failure;
const assert = (value, name, details) => { checks.push({ name, passed: Boolean(value), ...(details === undefined ? {} : { details }) }); if (!value) throw new Error(name); };
class CDP {
  constructor(socket) { this.socket = socket; this.id = 0; this.pending = new Map(); this.handlers = new Map(); socket.addEventListener('message', ({ data }) => { const message = JSON.parse(data); if (message.id) { const p = this.pending.get(message.id); this.pending.delete(message.id); if (p) message.error ? p.reject(new Error(JSON.stringify(message.error))) : p.resolve(message.result); } else for (const f of this.handlers.get(message.method) || []) Promise.resolve(f(message.params)).catch(e => errors.push(String(e))); }); }
  on(method, f) { this.handlers.set(method, [...(this.handlers.get(method) || []), f]); }
  send(method, params = {}) { const id = ++this.id, p = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject })); this.socket.send(JSON.stringify({ id, method, params })); return p; }
  async evaluate(expression) { const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; }
}
async function waitFor(expression, timeout = 90000) { const start = Date.now(); while (Date.now() - start < timeout) { if (await cdp.evaluate(expression)) return; await delay(100); } throw new Error('Timeout: ' + expression); }
async function get(path) { const response = await fetch(origin + path); assert(response.ok, 'GET ' + path.split('?')[0]); return response.json(); }
async function set(id, value) { await cdp.evaluate(`(() => {const e=document.getElementById(${JSON.stringify(id)});e.value=${JSON.stringify(String(value))};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`); }
async function ready() { await waitFor("document.querySelector('#case-workspace') && !document.querySelector('#case-workspace').hidden && document.querySelector('#case-loading').hidden && !document.querySelector('#debug-form').inert"); }
async function switchRun(id) { await set('debug-run-select', id); await waitFor(`document.querySelector('#run-label').textContent.includes(${JSON.stringify(id)})`); await ready(); }
async function screenshot(name, mobile = false) { await cdp.send('Emulation.setDeviceMetricsOverride', { width: mobile ? 390 : 1500, height: mobile ? 844 : 1100, deviceScaleFactor: 1, mobile }); await delay(350); const r = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true }); await writeFile(join(output, name), Buffer.from(r.data, 'base64'), { mode: 0o600 }); }
async function inspectContinuousCards(detail, mode) {
  const cards = [...detail.gtfs.candidates, ...(detail.gtfs.diagnostic_candidates || [])];
  const components = ['absolute_delay_level_cost', 'conditioned_fit_cost', 'delay_dynamics_cost', 'APC_stop_time_cost', 'projection_and_coverage_cost', 'pattern_progress_cost', 'vehicle_sequence_cost'];
  const expected = cards.map(candidate => ({ id: candidate.candidate_id, components: candidate.score_components, raw: candidate.score_components_unweighted, weights: candidate.score_component_weights, coverage: candidate.coverage_metrics, calibrated: candidate.calibrated, probability: candidate.probability, diagnostic: candidate.diagnostic_only === true }));
  assert(await cdp.evaluate(`(() => { const cards=[...document.querySelectorAll('#gtfs-candidate-list .gtfs-candidate-card,#gtfs-diagnostic-list .gtfs-diagnostic-card')], expected=${JSON.stringify(expected)}, keys=${JSON.stringify(components)}; return cards.length===expected.length && cards.every((card,index)=>{ const e=expected[index],rows=[...card.querySelectorAll('.gtfs-score-components tbody tr')]; return card.dataset.candidateId===e.id && card.dataset.scorer==='continuous_passage_v1' && rows.length===7 && rows.every((row,i)=>{const key=keys[i],cells=[...row.querySelectorAll('td')];return row.dataset.component===key && cells.length===3 && [e.raw?.[key],e.weights?.[key],e.components?.[key]].every((value,j)=>Number.isFinite(value)?Number(cells[j].title)===value:cells[j].textContent.includes('未知'));}); }); })()`), mode + ' continuous cards show all seven persisted raw costs, weights and weighted contributions in candidate order');
  assert(await cdp.evaluate(`(() => { const cards=[...document.querySelectorAll('#gtfs-candidate-list .gtfs-candidate-card,#gtfs-diagnostic-list .gtfs-diagnostic-card')], expected=${JSON.stringify(expected)}; return cards.every((card,index)=>{ const rows=[...card.querySelectorAll('.gtfs-continuous-coverage tbody tr')];return rows.length===3 && ['projection','schedule','end_to_end'].every((key,i)=>{const row=rows[i],coverage=expected[index].coverage[key];return row.dataset.coverage===key && ['point_ratio','duration_ratio'].every(metric=>{const cell=row.querySelector('[data-metric='+metric+']');return cell && (Number.isFinite(coverage[metric])?Number(cell.title)===coverage[metric]:cell.textContent.includes('未知'));});});}); })()`), mode + ' three explicit point and adjacent-interval coverage denominators agree with persisted values');
  assert(await cdp.evaluate("document.querySelectorAll('.gtfs-delay-penalty').length===0 && [...document.querySelectorAll('[data-scorer=continuous_passage_v1] .gtfs-continuous-method')].every(line=>line.textContent.includes('不使用重采样 GTFS'))"), mode + ' continuous scorer does not display legacy hourly penalty or claim sampled DTW scoring');
  assert(await cdp.evaluate("[...document.querySelectorAll('#gtfs-diagnostic-list .gtfs-diagnostic-card')].every(card=>card.querySelector('.gtfs-diagnostic-warning')?.textContent.includes('质量或校准可信集门槛未通过') && card.querySelector('.gtfs-continuous-delay')?.textContent.includes('非校准置信区间'))"), mode + ' diagnostics distinguish quality or calibration abstention and uncalibrated delay uncertainty');
  if (!detail.gtfs.result.calibration?.calibrated) {
    assert(detail.gtfs.candidates.length === 0 && cards.every(candidate => candidate.probability == null && candidate.calibrated !== true), mode + ' no independent calibration means zero final candidates and null probabilities');
    assert(await cdp.evaluate("document.querySelectorAll('#gtfs-candidate-list .gtfs-candidate-card').length===0 && [...document.querySelectorAll('#gtfs-diagnostic-list .gtfs-diagnostic-card')].every(card=>card.querySelector('[data-metric=calibrated_credible_set][data-passed=false]'))"), mode + ' every uncalibrated displayed hypothesis remains diagnostic with an explicit credible-set failure');
  } else {
    const credible = new Set(detail.gtfs.result.calibration.credible_set || []);
    assert(detail.gtfs.candidates.every(candidate => credible.has(candidate.candidate_id) && candidate.calibrated && Number.isFinite(candidate.probability)), mode + ' final continuous candidates require actual calibrated credible-set membership');
  }
}
async function inspectContinuousEnglish(mode) {
  const snapshot = `JSON.stringify({notes:document.querySelector('#review-notes').value,mode:document.querySelector('#matching-mode-select').value,source:[document.querySelector('#metric-avl').textContent,document.querySelector('#metric-apc').textContent],view:[document.querySelector('#timeline-svg').dataset.viewStart,document.querySelector('#timeline-svg').dataset.viewEnd],cards:[...document.querySelectorAll('[data-scorer=continuous_passage_v1]')].map(card=>[card.dataset.candidateId,card.querySelector('input[type=checkbox]').checked])})`;
  const before = await cdp.evaluate(snapshot), requestCount = requests.length;
  const rawDigest = `crypto.subtle.digest('SHA-256',new TextEncoder().encode(document.querySelector('#gtfs-diagnostics').textContent)).then(bytes=>Array.from(new Uint8Array(bytes)).map(value=>value.toString(16).padStart(2,'0')).join(''))`;
  const rawBefore = await cdp.evaluate(rawDigest);
  await cdp.evaluate("document.querySelector('#language-toggle').click()");
  await waitFor("document.documentElement.lang==='en' && window.TidesI18n.getLanguage()==='en'");
  const untranslated = await cdp.evaluate("[...document.querySelectorAll('.gtfs-score-components,.gtfs-continuous-coverage,.gtfs-continuous-delay,.gtfs-continuous-note,.gtfs-continuous-method,.gtfs-failed-gates')].map(node=>node.textContent).filter(text=>/[\\u3400-\\u9fff]/.test(text))");
  assert(untranslated.length === 0, mode + ' continuous component tables, coverage, uncertainty and failed gates fully translate to English', untranslated);
  assert(await cdp.evaluate(snapshot) === before, mode + ' language switch preserves review draft, selected mode, raw counts, window and visible candidates');
  assert(await cdp.evaluate(rawDigest) === rawBefore, mode + ' language switch does not translate or alter raw diagnostic JSON');
  assert(requests.length === requestCount, mode + ' language switch requests no data or matching computation');
  await cdp.evaluate("document.querySelector('#gtfs-candidate-panel').scrollIntoView({block:'start'})"); await screenshot('continuous-candidates-english.png');
  await screenshot('continuous-candidates-english-mobile.png', true);
  assert(await cdp.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2"), mode + ' English continuous cards have no mobile page-level horizontal overflow');
  await cdp.evaluate("document.querySelector('#language-toggle').click()"); await waitFor("document.documentElement.lang==='zh-CN'");
  assert(await cdp.evaluate(snapshot) === before, mode + ' switching back to Chinese retains inspected evidence and unsaved draft');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
}
async function startBrowser() {
  browser = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability', '--disable-extensions', '--disable-sync', '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const endpoint = await new Promise((resolve, reject) => { let log = ''; const timer = setTimeout(() => reject(new Error(log)), 15000); browser.on('error', reject); browser.stderr.on('data', chunk => { log += chunk; const m = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (m) { clearTimeout(timer); resolve(m[1]); } }); });
  const pages = await fetch(endpoint.replace(/^ws:/, 'http:').split('/devtools/')[0] + '/json/list').then(r => r.json()), socket = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); }); cdp = new CDP(socket);
  cdp.on('Runtime.exceptionThrown', e => errors.push(e)); cdp.on('Runtime.consoleAPICalled', e => { if (e.type === 'error') errors.push(e); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({ url: request.url, method: request.method }); if (/^https?:/.test(request.url) && new URL(request.url).origin !== origin) external.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if (response.status >= 400) errors.push({ url: response.url, status: response.status }); });
  cdp.on('Page.javascriptDialogOpening', () => cdp.send('Page.handleJavaScriptDialog', { accept: true }));
  // Defense in depth: a faulty UI cannot save a review, run an experiment or
  // contact an external map service during this acceptance check.
  cdp.on('Fetch.requestPaused', ({ requestId, request }) => { const allowed = request.method === 'GET' && (/^(about:|data:|blob:)/.test(request.url) || new URL(request.url).origin === origin); return cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', { requestId, ...(allowed ? {} : { errorReason: 'BlockedByClient' }) }); });
  for (const method of ['Page.enable', 'Runtime.enable', 'Network.enable']) await cdp.send(method); await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] }); await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
}
async function inspectOverview(mode, expectedCases) {
  const started = performance.now();
  await cdp.evaluate("document.querySelector('#day-overview-tab').click()");
  await waitFor(`document.querySelector('#day-overview-page').dataset.matchingMode===${JSON.stringify(mode)} && document.querySelectorAll('.overview-case-card').length===${expectedCases} && document.querySelector('#day-overview-page').getAttribute('aria-busy')!=='true'`);
  const readyMs = Math.round(performance.now() - started);
  const data = await get('/api/day-overview?' + new URLSearchParams({ run, matching_mode: mode }));
  performanceChecks.push({ mode, overview_ready_ms: readyMs, overview_ready_and_verification_fetch_ms: Math.round(performance.now() - started), compact_payload_bytes: Buffer.byteLength(JSON.stringify(data)) });
  assert(data.cases.length === expectedCases, mode + ' overview includes every candidate segment');
  assert(data.summary.omitted_valid_primary_point_count === 0, mode + ' all valid primary observations retained');
  assert(await cdp.evaluate(`document.querySelectorAll('.overview-facet').length===${data.reference_facets.length} && document.querySelectorAll('.overview-case-card').length===${data.cases.length}`), mode + ' DOM preserves reference facets and all cases including no-evidence');
  assert(await cdp.evaluate("[...document.querySelectorAll('.overview-chart')].every(root=>[...root.querySelectorAll('[data-reference-id]')].every(e=>e.dataset.referenceId===root.dataset.referenceId))"), mode + ' no mixing of canonical reference distance axes');
  assert(await cdp.evaluate("[...document.querySelectorAll('.overview-actual')].every(e=>e.dataset.fragmentId && +e.dataset.renderedPointCount<=+e.dataset.pointCount && e.getAttribute('d').trim().startsWith('M'))"), mode + ' every actual path is a separate persisted fragment with bounded sampling');
  const expectedPaths = data.cases.reduce((sum, c) => sum + c.fragments.length, 0);
  assert(await cdp.evaluate(`document.querySelectorAll('.overview-actual').length===${expectedPaths}`), mode + ' exact fragment path count prevents silent missing traces');
  const fallback = data.cases.filter(c => c.candidate?.source === 'fallback').length, accepted = data.cases.filter(c => c.candidate?.source === 'accepted').length;
  const continuousOverlays = data.cases.map(row => row.candidate).filter(candidate => candidate?.scorer === 'continuous_passage_v1');
  if (continuousOverlays.length) assert(continuousOverlays.every(candidate => candidate.source !== 'accepted' || (candidate.calibrated === true && Number.isFinite(candidate.probability) && candidate.gate_checks.some(gate => gate.metric === 'calibrated_credible_set' && gate.passed))), mode + ' continuous overview never promotes numerical fit without calibrated credible-set membership');
  assert(await cdp.evaluate(`document.querySelectorAll('.overview-scheduled[data-candidate-source=fallback]').length===${fallback} && document.querySelectorAll('.overview-scheduled[data-candidate-source=accepted]').length===${accepted}`), mode + ' exactly one passed candidate or explicitly failed fallback per case');
  assert(await cdp.evaluate("[...document.querySelectorAll('.overview-case-card[data-candidate-source=fallback]')].every(e=>e.textContent.includes('未过门槛'))"), mode + ' fallback cannot be read as accepted match');
  if (mode !== 'global') {
    assert(await cdp.evaluate("[...document.querySelectorAll('.overview-chart [data-kind=apc-stop-interval][data-selected-for-decision=true]')].every(e=>e.dataset.selectionClipped==='true')"), mode + ' APC selected interval spans explicitly clipped, full original retained as context');
    assert(await cdp.evaluate("document.querySelector('#matching-mode-note').textContent.includes('不是实时前缀预测')"), mode + ' explicitly offline full-projection experiment, not real-time forecasting');
  }
  assert(await cdp.evaluate("[...document.querySelectorAll('svg *')].every(e=>[...e.attributes].every(a=>!['d','x','y','cx','cy','x1','x2','y1','y2'].includes(a.name)||!/(NaN|Infinity)/.test(a.value)))"), mode + ' finite SVG geometry');
  const candidateCase = data.cases.find(c => c.candidate?.trajectory?.length);
  if (candidateCase) {
    const point = candidateCase.candidate.trajectory.find(p => Number.isFinite(p.t) && Number.isFinite(p.s)), ref = candidateCase.candidate.reference_id;
    assert(await cdp.evaluate(`(() => { const root=[...document.querySelectorAll('.overview-chart')].find(e=>e.dataset.referenceId===${JSON.stringify(ref)}), mark=[...root.querySelectorAll('.overview-case')].find(e=>e.dataset.caseId===${JSON.stringify(candidateCase.id)}).querySelector('.overview-scheduled'), d=root.viewBox.baseVal, a=+root.dataset.viewStart,b=+root.dataset.viewEnd,left=d.width<500?44:64; const expected=left+(${point.t}-a)/(b-a)*(d.width-left-23), actual=+mark.getAttribute('d').match(/^M([\\d.-]+),/)[1]; return Math.abs(actual-expected)<.02; })()`), mode + ' scheduled curve uses original clock with no estimated-delay shift');
  }
  summaries.push({ mode, cases: data.cases.length, statuses: data.summary.statuses, accepted, fallback, fragments: expectedPaths });
  return data;
}

try {
  const meta = await get('/api/meta?' + new URLSearchParams({ run }));
  assert(['global', 'early', 'middle', 'late'].every(mode => meta.available_matching_modes?.includes(mode)), 'new run advertises all four computed modes');
  await startBrowser(); await cdp.send('Page.navigate', { url: origin }); await ready(); await switchRun(run);
  const vehicle = meta.vehicles.find(v => String(v.label) === vehicleLabel) || meta.vehicles[0]; await set('vehicle-select', vehicle.id); await ready();
  const catalog = await get('/api/catalog?' + new URLSearchParams({ run, method: 'gtfs_trip_matching', vehicle: vehicle.id, matching_mode: 'global' }));
  const chosen = catalog.cases.find(c => c.id === caseId) || catalog.cases.find(c => c.avl_count > 50) || catalog.cases[0];
  await set('case-search', chosen.id); await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId===${JSON.stringify(chosen.id)} && document.querySelector('#case-loading').hidden`); await set('window-size', 'full');
  const initialSource = await cdp.evaluate("[document.querySelector('#metric-avl').textContent,document.querySelector('#metric-apc').textContent,document.querySelector('#timeline-svg').dataset.viewStart,document.querySelector('#timeline-svg').dataset.viewEnd]");
  const draft = 'READ-ONLY ACCEPTANCE DRAFT — must survive all four decision switches'; await set('review-notes', draft);
  for (const mode of ['early', 'middle', 'late', 'global']) {
    await set('matching-mode-select', mode); await waitFor(`document.querySelector('#matching-view-toolbar').dataset.matchingMode===${JSON.stringify(mode)} && !document.querySelector('#matching-mode-select').disabled`);
    const detail = await get('/api/case?' + new URLSearchParams({ run, method: 'gtfs_trip_matching', vehicle: vehicle.id, case_id: chosen.id, context_s: 180, matching_mode: mode }));
    assert(await cdp.evaluate(`document.querySelector('#gtfs-result-status').dataset.status===${JSON.stringify(detail.gtfs.result.status)} && JSON.parse(document.querySelector('#gtfs-diagnostics').textContent).matching_mode===${JSON.stringify(mode)}`), mode + ' displays separately computed mode result and diagnostics');
    assert(await cdp.evaluate(`JSON.stringify([...document.querySelectorAll('#gtfs-candidate-list .gtfs-candidate-card')].map(card => card.dataset.candidateId)) === ${JSON.stringify(JSON.stringify(detail.gtfs.candidates.map(c => c.candidate_id)))}`), mode + ' candidate card order matches the persisted mode ranking');
    assert(await cdp.evaluate(`JSON.stringify([...document.querySelectorAll('#gtfs-diagnostic-list .gtfs-diagnostic-card')].map(card=>card.dataset.candidateId))===${JSON.stringify(JSON.stringify((detail.gtfs.diagnostic_candidates || []).map(candidate=>candidate.candidate_id)))}`), mode + ' diagnostic card order matches persisted diagnostic ranking');
    if (detail.gtfs.result.scorer === 'continuous_passage_v1') {
      await inspectContinuousCards(detail, mode);
      if (mode === 'early') await inspectContinuousEnglish(mode);
    } else {
      const delayWeight = detail.gtfs.config.score_delay_prior_weight, expectedPenalties = [...detail.gtfs.candidates, ...(detail.gtfs.diagnostic_candidates || [])].filter(c => Number.isFinite(c.estimated_delay_s)).map(c => [String(c.trip_id), delayWeight * Math.abs(c.estimated_delay_s) / 3600]);
      assert(await cdp.evaluate(`(() => { const expected = new Map(${JSON.stringify(expectedPenalties)}), lines = [...document.querySelectorAll('.gtfs-delay-penalty')]; return lines.length === expected.size && lines.every(line => +line.dataset.weight === ${delayWeight} && Math.abs(+line.dataset.penalty - expected.get(line.closest('[data-trip-id]').dataset.tripId)) < 1e-10) && +document.querySelector('#debug-gtfs-score_delay_prior_weight').value === ${delayWeight}; })()`), mode + ' current-run time weight and aggregate delay contribution shown for every scored card');
    }
    assert(await cdp.evaluate(`document.querySelector('#review-notes').value===${JSON.stringify(draft)}`), mode + ' unsaved review draft preserved without POST');
    assert(JSON.stringify(await cdp.evaluate("[document.querySelector('#metric-avl').textContent,document.querySelector('#metric-apc').textContent,document.querySelector('#timeline-svg').dataset.viewStart,document.querySelector('#timeline-svg').dataset.viewEnd]")) === JSON.stringify(initialSource), mode + ' raw counts and browse bounds unchanged');
    if (mode !== 'global') {
      const selected = detail.gtfs.decision_selection.selected_avl_ids || [];
      assert(await cdp.evaluate(`(() => {const selected=new Set(${JSON.stringify(selected)});return [...document.querySelectorAll('#gtfs-progress-svg [data-source=avl][data-selected-for-decision=true]')].every(e=>selected.has('avl|'+(e.dataset.rid||e.dataset.toRid)));})()`), mode + ' highlighted source IDs agree with backend distance-third selection');
      assert(await cdp.evaluate("['timeline-svg','progress-svg','gtfs-schedule-svg','gtfs-progress-svg'].every(id=>document.getElementById(id).querySelector('.decision-selection-bands'))"), mode + ' four synchronized charts expose decision selection');
      assert(detail.gtfs.decision_selection.basis === 'valid_projected_travel_distance', mode + ' uses projected distance not raw duration or point count');
      assert(await cdp.evaluate("[...document.querySelectorAll('#gtfs-progress-svg [data-source=apc][data-selected-for-decision=true]')].every(e=>e.dataset.selectionClipped==='true'&&e.hasAttribute('clip-path'))"), mode + ' detail APC evidence clipped at the decision boundary');
      if (mode === 'middle') { await cdp.evaluate("document.querySelector('#gtfs-progress-panel').scrollIntoView({block:'start'})"); await screenshot('detail-middle-progress.png'); await cdp.evaluate("document.querySelector('.timeline-panel').scrollIntoView({block:'start'})"); await screenshot('detail-middle-timeline.png'); }
    }
  }
  await cdp.evaluate("document.querySelector('#matching-view-toolbar').scrollIntoView({block:'start'})"); await screenshot('detail-global.png');
  const all = await get('/api/day-overview?' + new URLSearchParams({ run, matching_mode: 'global' })), expectedCases = all.cases.length;
  await inspectOverview('global', expectedCases);
  assert(await cdp.evaluate("document.querySelector('#overview-pan').disabled && document.querySelector('#overview-window-label').textContent==='00:00 – 24:00'"), 'full-day default viewport and explicit 24:00 endpoint');
  for (const mode of ['early', 'middle', 'late']) { await set('matching-mode-select', mode); await waitFor(`document.querySelector('#day-overview-page').dataset.matchingMode===${JSON.stringify(mode)} && !document.querySelector('#matching-mode-select').disabled`); await inspectOverview(mode, expectedCases); }
  await cdp.evaluate("document.querySelector('#day-overview-page').scrollIntoView({block:'start'})"); await screenshot('day-overview-late.png');
  await set('overview-window-size', '10800'); const before = await cdp.evaluate("document.querySelector('.overview-chart').dataset.viewStart"); await set('overview-pan', 600);
  assert(await cdp.evaluate(`!document.querySelector('#overview-pan').disabled && document.querySelector('.overview-chart').dataset.viewStart!==${JSON.stringify(before)} && [...document.querySelectorAll('.overview-chart')].every(root=>+root.dataset.viewEnd- +root.dataset.viewStart===10800)`), 'overview time pan changes all reference facets together');
  await set('overview-window-size', 'custom'); await set('overview-custom-minutes', 75);
  assert(await cdp.evaluate("[...document.querySelectorAll('.overview-chart')].every(root=>+root.dataset.viewEnd- +root.dataset.viewStart===4500)"), 'custom overview window length honored');
  await set('overview-window-size', '86400'); await screenshot('day-overview-mobile.png', true);
  assert(await cdp.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2"), 'mobile overview has no page-level horizontal overflow');
  await screenshot('day-overview-desktop.png');
  const next = all.cases.find(c => c.vehicle !== vehicle.id && c.candidate) || all.cases[0];
  await cdp.evaluate(`(() => {const e=[...document.querySelectorAll('.overview-case')].find(e=>e.dataset.caseId===${JSON.stringify(next.id)});e.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()`); await ready();
  await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId===${JSON.stringify(next.id)}`);
  assert(await cdp.evaluate("document.querySelector('#day-overview-page').hidden && !document.querySelector('#trip-detail-page').hidden && document.querySelector('#matching-mode-select').value==='late' && JSON.parse(document.querySelector('#gtfs-diagnostics').textContent).matching_mode==='late'"), 'clicking all-day line opens exact vehicle/case preserving selected mode');
  if (!skipLegacyGlobal) {
    await switchRun(legacy);
    assert(await cdp.evaluate("document.querySelector('#matching-mode-select').value==='global' && [...document.querySelector('#matching-mode-select').options].filter(o=>o.value!=='global').every(o=>o.disabled&&o.textContent.includes('未计算'))"), 'historical run explicitly disables absent modes instead of relabeling global');
  }
  assert(!requests.some(r => r.method !== 'GET'), 'all browser requests were GET; no reviews or experiments written');
  assert(!external.length, 'no external resource requests');
  assert(!errors.length, 'no browser JS or HTTP errors', errors);
} catch (e) { failure = String(e.stack || e); }
finally {
  await writeFile(join(output, 'browser-modes-results.json'), JSON.stringify({ run, legacy, legacy_global_skipped: skipLegacyGlobal, legacy_skip_reason: skipLegacyGlobal ? 'Explicit --skip-legacy-global; retained four-mode checks still run in full' : null, checks, summaries, performanceChecks, errors, external, requests, failure }, null, 2), { mode: 0o600 });
  if (cdp) { try { await cdp.send('Browser.close'); } catch {} } if (browser && browser.exitCode == null) browser.kill('SIGTERM');
}
if (failure) { process.stderr.write(failure + '\n'); process.exitCode = 1; }
else process.stdout.write(JSON.stringify({ passed: checks.length, summaries, performanceChecks, output }) + '\n');
