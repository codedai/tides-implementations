/** Read-only real-browser acceptance: in-place Chinese / English workbench switch. */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const args = process.argv.slice(2), option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
const run = option('--run', 'run-20231106-20260909T183705-d67ffa30723d');
const origin = option('--origin', 'http://127.0.0.1:8768'), output = resolve(option('--output', 'artifacts/merge-debug-20231106/i18n-browser'));
const vehicleLabel = option('--vehicle', '9021'), caseId = option('--case', 'direction:9021:0000');
await mkdir(output, { recursive: true, mode: 0o700 });
const profile = await mkdtemp(join(tmpdir(), 'tides-i18n-chrome-'));
const checks = [], errors = [], requests = [], external = [], untranslated = [], summaries = [];
let browser, cdp, failure;
const check = (value, name, details) => { checks.push({ name, passed: Boolean(value), ...(details === undefined ? {} : { details }) }); return Boolean(value); };
const assert = (value, name, details) => { if (!check(value, name, details)) throw new Error(name); };
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
async function reload() { const previous = await cdp.evaluate('performance.timeOrigin'); await cdp.send('Page.reload', { ignoreCache:true }); await waitFor(`performance.timeOrigin!==${previous}`); await ready(); await delay(250); }
async function screenshot(name, mobile = false) { await cdp.send('Emulation.setDeviceMetricsOverride', { width: mobile ? 390 : 1500, height: mobile ? 844 : 1100, deviceScaleFactor: 1, mobile }); await delay(350); const r = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true }); await writeFile(join(output, name), Buffer.from(r.data, 'base64'), { mode: 0o600 }); }
async function startBrowser() {
  browser = spawn('google-chrome', ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking', '--disable-component-update', '--disable-domain-reliability', '--disable-extensions', '--disable-sync', '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication', '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const endpoint = await new Promise((resolve, reject) => { let log = ''; const timer = setTimeout(() => reject(new Error(log)), 15000); browser.on('error', reject); browser.stderr.on('data', chunk => { log += chunk; const m = log.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (m) { clearTimeout(timer); resolve(m[1]); } }); });
  const pages = await fetch(endpoint.replace(/^ws:/, 'http:').split('/devtools/')[0] + '/json/list').then(r => r.json()), socket = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); }); cdp = new CDP(socket);
  cdp.on('Runtime.exceptionThrown', e => errors.push(e)); cdp.on('Runtime.consoleAPICalled', e => { if (e.type === 'error') errors.push(e); });
  cdp.on('Network.requestWillBeSent', ({ request }) => { requests.push({ url: request.url, method: request.method }); if (/^https?:/.test(request.url) && new URL(request.url).origin !== origin) external.push(request.url); });
  cdp.on('Network.responseReceived', ({ response }) => { if (response.status >= 400) errors.push({ url: response.url, status: response.status }); });
  cdp.on('Page.javascriptDialogOpening', () => cdp.send('Page.handleJavaScriptDialog', { accept: true }));
  // A broken UI must not persist drafts, start an experiment, or fetch map tiles.
  cdp.on('Fetch.requestPaused', ({ requestId, request }) => { const allowed = request.method === 'GET' && (/^(about:|data:|blob:)/.test(request.url) || new URL(request.url).origin === origin); return cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', { requestId, ...(allowed ? {} : { errorReason: 'BlockedByClient' }) }); });
  for (const method of ['Page.enable', 'Runtime.enable', 'Network.enable']) await cdp.send(method);
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
}
async function language(expected, keyboard = false) {
  if (keyboard) {
    await cdp.send('Page.bringToFront');
    await cdp.evaluate("document.querySelector('#language-toggle').focus()");
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: '\r', unmodifiedText: '\r' });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  } else await cdp.evaluate("document.querySelector('#language-toggle').click()");
  await waitFor(`document.documentElement.lang===${JSON.stringify(expected)} && window.TidesI18n.getLanguage()===${JSON.stringify(expected)}`);
  await delay(250);
  check(await cdp.evaluate(`localStorage.getItem('tides-review-language')===${JSON.stringify(expected)}`), expected + ' preference persisted in localStorage');
  check(await cdp.evaluate(`document.querySelector('#language-toggle').getAttribute('aria-checked')===${JSON.stringify(String(expected==='en'))}`), expected + ' accessible switch state agrees with selected language');
  if(keyboard)check(await cdp.evaluate("document.activeElement===document.querySelector('#language-toggle')"), expected + ' native keyboard switch retains focus');
}
const stateExpression = `(() => {
  const q=s=>document.querySelector(s), all=s=>[...document.querySelectorAll(s)];
  return {
    timeOrigin:performance.timeOrigin,
    run:q('#debug-run-select').value,mode:q('#matching-mode-select').value,vehicle:q('#vehicle-select').value,
    caseId:q('#case-list .case-item.active')?.dataset.caseId, reference:q('#gtfs-reference-select').value,
    counts:['metric-avl','metric-apc','metric-support','metric-attention'].map(id=>q('#'+id).textContent),
    candidates:all('.gtfs-candidate-card,.gtfs-diagnostic-card').map(e=>({id:e.dataset.candidateId,trip:e.dataset.tripId,color:e.style.getPropertyValue('--candidate-color'),visible:e.querySelector('input[type=checkbox]').checked})),
    view:['timeline-svg','progress-svg','gtfs-schedule-svg','gtfs-progress-svg'].map(id=>({id,start:q('#'+id).dataset.viewStart,end:q('#'+id).dataset.viewEnd})),
    cursor:q('#time-scrubber').value,cursorTime:q('#scrubber-time').textContent,windowSize:q('#window-size').value,
    sourceRows:all('#observation-table-body tr').map(e=>({...e.dataset})),
    controls:all('#debug-form input,#debug-form select,#review-notes,#review-verdict input,#layer-avl,#layer-apc,#layer-context,#direction-conflicts-visible').map(e=>({id:e.id,name:e.name,value:e.value,checked:e.checked})),
    json:all('#gtfs-diagnostics,#debug-provenance,#debug-diff-json,#gap-segmentation-audit,#case-provenance,.gtfs-candidate-card pre,.gtfs-diagnostic-card pre').map(e=>({id:e.id,text:e.textContent})),
    jsonTitles:all('[title]').map(e=>e.getAttribute('title')).filter(t=>t.trim().startsWith('{'))
  };
})()`;
async function snapshot() { return cdp.evaluate(stateExpression); }
function compareState(before, after, phase) { for (const key of Object.keys(before)) check(JSON.stringify(before[key]) === JSON.stringify(after[key]), phase + ' preserves ' + key); }
async function collectChinese(phase) {
  const rows = await cdp.evaluate(`(() => {
    const rows=[], han=/[\\u3400-\\u9fff]/, skip='script,style,pre,code,textarea,#language-toggle,[translate=no],[data-i18n-skip],#debug-run-select,#debug-base-select,#observation-table-body,#apc-table-body';
    const visible=e=>e && !e.closest('[hidden]') && e.getClientRects().length>0 && getComputedStyle(e).display!=='none' && getComputedStyle(e).visibility!=='hidden';
    const selector=e=>e.id?'#'+e.id:e.tagName.toLowerCase()+(e.classList.length?'.'+[...e.classList].join('.'):'');
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()){const n=walker.currentNode,e=n.parentElement,t=n.nodeValue.trim();if(t&&han.test(t)&&visible(e)&&!e.closest(skip)) rows.push({selector:selector(e),kind:'text',text:t.slice(0,700)});}
    for(const e of document.querySelectorAll('[aria-label],[title],[placeholder]')) if(visible(e)&&!e.closest(skip)) for(const a of ['aria-label','title','placeholder']){const t=e.getAttribute(a); if(t&&han.test(t)&&!t.trim().startsWith('{'))rows.push({selector:selector(e),kind:a,text:t.slice(0,700)});}
    return rows;
  })()`);
  untranslated.push({ phase, rows }); return rows;
}
async function inspectEnglish(phase) {
  const selectors = ['.sidebar-heading h1','#debug-mode-title','#trip-detail-tab','#day-overview-tab','#matching-mode-note','#gtfs-result-summary','#gtfs-uncertainty-note','#gtfs-candidate-count','#gtfs-candidate-list','#gtfs-diagnostic-list','.timeline-panel h3','.progress-panel h3','#gtfs-progress-panel h3'];
  const labels = await cdp.evaluate(`(() => {const han=/[\\u3400-\\u9fff]/;return ${JSON.stringify(selectors)}.map(selector=>{const e=document.querySelector(selector);if(!e)return {selector,missing:true};const copy=e.cloneNode(true);copy.querySelectorAll('pre,code').forEach(n=>n.remove());return {selector,text:copy.textContent.trim(),hasChinese:han.test(copy.textContent)};});})()`);
  for (const label of labels) check(!label.missing && !label.hasChinese && label.text.length > 0, phase + ' English ' + label.selector, label.text?.slice(0,250));
  check(await cdp.evaluate("[...document.querySelector('#matching-mode-select').options].every(e=>!/[\\u3400-\\u9fff]/.test(e.textContent))"), phase + ' all four decision option labels in English');
  check(await cdp.evaluate("['timeline-svg','progress-svg','gtfs-schedule-svg','gtfs-progress-svg','time-scrubber','timeline-pan'].every(id=>{const t=document.getElementById(id).getAttribute('aria-label');return t&&!/[\\u3400-\\u9fff]/.test(t)})"), phase + ' accessible chart and browsing labels in English');
  check(await cdp.evaluate("[...document.querySelectorAll('.gtfs-delay-penalty')].length>0 && [...document.querySelectorAll('.gtfs-delay-penalty')].every(e=>!/[\\u3400-\\u9fff]/.test(e.textContent)&&+e.dataset.weight===1)"), phase + ' experiment 03d time-shift weight and penalty displayed in English');
  summaries.push({ phase, labels }); await collectChinese(phase);
}

try {
  const meta = await get('/api/meta?' + new URLSearchParams({ run }));
  await startBrowser(); await cdp.send('Page.navigate', { url: origin }); await ready();
  assert(await cdp.evaluate("typeof window.TidesI18n?.getLanguage==='function' && typeof window.TidesI18n?.setLanguage==='function' && typeof window.TidesI18n?.translate==='function'"), 'public localization API available');
  assert(await cdp.evaluate("document.querySelector('#language-toggle')?.tagName==='BUTTON' && document.querySelector('#language-toggle').type==='button'"), 'switch is a native non-submit keyboard-accessible button');
  assert(await cdp.evaluate("document.documentElement.lang==='zh-CN' && window.TidesI18n.getLanguage()==='zh-CN'"), 'clean profile defaults to Chinese');
  await set('debug-run-select', run); await waitFor(`document.querySelector('#run-label').textContent.includes(${JSON.stringify(run)})`); await ready();
  const vehicle = meta.vehicles.find(v => String(v.label) === vehicleLabel) || meta.vehicles[0]; await set('vehicle-select', vehicle.id); await ready();
  const catalog = await get('/api/catalog?' + new URLSearchParams({ run, method:'gtfs_trip_matching', vehicle:vehicle.id, matching_mode:'global' }));
  const chosen = catalog.cases.find(c => c.id === caseId) || catalog.cases[0];
  await set('case-search', chosen.id); await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId===${JSON.stringify(chosen.id)} && document.querySelector('#case-loading').hidden`);
  await set('window-size', 600); await set('time-scrubber', 643);
  await set('review-notes', '未保存的中文审查笔记 · DO NOT SAVE — language must not change this draft.');
  await set('debug-label', '未提交实验参数 · DO NOT RUN');
  await set('debug-gtfs-score_delay_prior_weight', 1.75);
  await cdp.evaluate("(() => {document.querySelector('#review-verdict input[value=uncertain]').click();const box=document.querySelector('.gtfs-candidate-card input[type=checkbox]');if(box)box.click();const color=document.querySelector('.gtfs-candidate-card input[type=color]');if(color){color.value='#a855f7';color.dispatchEvent(new Event('input',{bubbles:true}));}window.__i18nNotesNode=document.querySelector('#review-notes');window.__i18nParameterNode=document.querySelector('#debug-gtfs-score_delay_prior_weight');})()");
  await delay(200); const before = await snapshot(), requestCount = requests.length;
  await language('en'); compareState(before, await snapshot(), 'Chinese → English');
  check(requests.length===requestCount, 'language change performs no network request or data reload');
  check(await cdp.evaluate("window.__i18nNotesNode===document.querySelector('#review-notes')&&window.__i18nParameterNode===document.querySelector('#debug-gtfs-score_delay_prior_weight')"), 'language change preserves actual draft and configuration input nodes');
  await inspectEnglish('initial detail');
  await cdp.evaluate("document.querySelector('#matching-view-toolbar').scrollIntoView({block:'start'})"); await screenshot('english-detail.png');
  await cdp.evaluate("document.querySelector('#gtfs-candidate-panel').scrollIntoView({block:'start'})"); await screenshot('english-candidates.png');
  await cdp.evaluate("document.querySelector('.topbar').scrollIntoView({block:'start'})"); await screenshot('english-mobile.png', true);
  check(await cdp.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2"), 'English detail mobile has no page-level horizontal overflow');
  await screenshot('english-header.png');
  await language('zh-CN', true); compareState(before, await snapshot(), 'English → Chinese keyboard');
  check(await cdp.evaluate("/[\\u3400-\\u9fff]/.test(document.querySelector('#trip-detail-tab').textContent)&&/[\\u3400-\\u9fff]/.test(document.querySelector('.gtfs-candidate-score').textContent)"), 'Chinese static and dynamic card labels restored');
  await language('en', true);
  for (const mode of ['early','middle','late']) {
    await set('matching-mode-select', mode); await waitFor(`document.querySelector('#matching-view-toolbar').dataset.matchingMode===${JSON.stringify(mode)}&&!document.querySelector('#matching-mode-select').disabled`); await delay(200);
    await inspectEnglish('dynamic '+mode);
    check(await cdp.evaluate("document.querySelector('#review-notes').value.includes('未保存的中文审查笔记')&&document.querySelector('#debug-gtfs-score_delay_prior_weight').value==='1.75'"), mode + ' new translated content leaves draft text and unsaved config intact');
  }
  await cdp.evaluate("document.querySelector('#day-overview-tab').click()");
  await waitFor("document.querySelectorAll('.overview-case-card').length>0&&document.querySelector('#day-overview-page').getAttribute('aria-busy')!=='true'"); await delay(250);
  const overviewBefore = await cdp.evaluate("({ids:[...document.querySelectorAll('.overview-case-card')].map(e=>[e.dataset.caseId,e.dataset.candidateSource]),paths:[...document.querySelectorAll('.overview-actual,.overview-scheduled')].map(e=>[e.dataset.caseId,e.getAttribute('d'),e.getAttribute('stroke')])})");
  check(await cdp.evaluate("['#day-overview-page h2','#day-overview-status','.overview-legend','#overview-case-list'].every(s=>{const e=document.querySelector(s);return e?.textContent&&!/[\\u3400-\\u9fff]/.test(e.textContent)})"), 'dynamic English all-day overview heading status legend and all case cards');
  check(await cdp.evaluate("[...document.querySelectorAll('.overview-chart')].every(e=>!/[\\u3400-\\u9fff]/.test(e.getAttribute('aria-label')||''))"), 'all-day overview chart accessibility translated');
  await collectChinese('all-day overview');
  await set('overview-window-size',10800); await set('overview-pan',450);
  const overviewView = await cdp.evaluate("[...document.querySelectorAll('.overview-chart')].map(e=>[e.dataset.viewStart,e.dataset.viewEnd])");
  await language('zh-CN'); await language('en');
  check(JSON.stringify(overviewView)===JSON.stringify(await cdp.evaluate("[...document.querySelectorAll('.overview-chart')].map(e=>[e.dataset.viewStart,e.dataset.viewEnd])")), 'overview language toggles preserve custom browsing bounds and pan');
  check(JSON.stringify(overviewBefore.ids)===JSON.stringify(await cdp.evaluate("[...document.querySelectorAll('.overview-case-card')].map(e=>[e.dataset.caseId,e.dataset.candidateSource])")), 'overview language toggles preserve all case IDs and candidate acceptance categories');
  await cdp.evaluate("document.querySelector('#day-overview-page').scrollIntoView({block:'start'})"); await screenshot('english-overview.png'); await screenshot('english-overview-mobile.png',true);
  check(await cdp.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2"), 'English all-day overview mobile has no page-level horizontal overflow');
  await screenshot('english-overview-desktop.png');
  const next = await cdp.evaluate(`[...document.querySelectorAll('.overview-case-card')].find(e=>e.dataset.caseId!==${JSON.stringify(chosen.id)})?.dataset.caseId`);
  assert(next, 'second case available for dynamic translation');
  await cdp.evaluate(`document.querySelector('.overview-case-card[data-case-id="${next}"]').click()`); await ready();
  await waitFor(`document.querySelector('#case-list .case-item.active')?.dataset.caseId===${JSON.stringify(next)}`); await delay(250);
  check(await cdp.evaluate("window.TidesI18n.getLanguage()==='en'&&!/[\\u3400-\\u9fff]/.test(document.querySelector('#case-subtitle').textContent)&&!/[\\u3400-\\u9fff]/.test(document.querySelector('#gtfs-result-summary').textContent)"), 'newly loaded case is translated while retaining raw identity');
  await collectChinese('new case');
  await reload();
  check(await cdp.evaluate("document.documentElement.lang==='en'&&window.TidesI18n.getLanguage()==='en'&&localStorage.getItem('tides-review-language')==='en'&&!/[\\u3400-\\u9fff]/.test(document.querySelector('#trip-detail-tab').textContent)"), 'English preference survives full browser reload');
  await language('zh-CN'); await reload();
  check(await cdp.evaluate("document.documentElement.lang==='zh-CN'&&window.TidesI18n.getLanguage()==='zh-CN'&&/[\\u3400-\\u9fff]/.test(document.querySelector('#trip-detail-tab').textContent)"), 'Chinese preference survives full browser reload');
  check(!requests.some(r=>r.method!=='GET'), 'all browser requests GET; no experiments or review writes');
  check(!external.length, 'no external resource requests');
  check(!errors.length, 'no browser JavaScript or HTTP errors',errors);
} catch(e) { failure=String(e.stack||e); }
finally {
  if(!failure&&checks.some(c=>!c.passed))failure=checks.filter(c=>!c.passed).map(c=>c.name).join('\n');
  await writeFile(join(output,'browser-i18n-results.json'),JSON.stringify({run,checks,summaries,untranslated,errors,external,requests,failure},null,2),{mode:0o600});
  if(cdp){try{await cdp.send('Browser.close');}catch{}}if(browser&&browser.exitCode==null)browser.kill('SIGTERM');
}
if(failure){process.stderr.write(failure+'\n');process.exitCode=1;}
else process.stdout.write(JSON.stringify({passed:checks.length,untranslated:untranslated.map(r=>({phase:r.phase,count:r.rows.length})),output})+'\n');
