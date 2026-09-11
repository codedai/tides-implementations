'use strict';
const words = {
  zh: { title:'实验05 · 运行工作台',subtitle:'从原始观测到三轮锁定分配与TIDES导出',newRun:'运行实验',date:'数据日期',route:'线路',mode:'匹配模式',all:'全部四种模式',global:'全局',early:'前期',middle:'中期',late:'后期',run:'开始处理',force:'强制重新处理（保留已有结果）',notice:'每次只运行一个任务；可复用的成功结果由同一处理管线验证后读取。分配结果不等于经过校准的真实trip确认。',availability:'原始数据与GTFS可用性',progress:'处理进度',idle:'尚未运行',progressEmpty:'开始后显示各阶段的实际状态与行数。',history:'已保存的运行',refresh:'刷新',result:'结果与导出',stageCounts:'阶段行数',downloads:'下载CSV / Parquet / JSON',format:'格式',allFormats:'全部格式',rawSummary:'完整摘要',footer:'仅本机访问 · 不使用外部资源 · 下载前校验发布文件SHA-256',loadingDates:'加载可用日期…',chooseDate:'请选择日期',noDates:'暂无可处理日期',available:'可用',unavailable:'不可用',reason:'原因',queued:'已排队',running:'处理中',complete:'已完成',failed:'失败',cached:'已复用验证通过的缓存',fresh:'已生成新的处理结果',pass:'校验通过',fail:'校验失败',pending:'未发布完整结果',performed:'分割后的trip',assigned:'已分配',unmatched:'未分配',emptyRuns:'尚无保存的运行。',zip:'下载本模式TIDES ZIP',noOutputs:'尚无已发布的可下载文件。',field:'字段',count:'数量',noReasons:'无未分配原因',connection:'连接暂时中断，正在重试…' },
  en: { title:'Experiment 05 · Workbench',subtitle:'Raw observations → three locked assignment rounds → TIDES export',newRun:'Run experiment',date:'Data date',route:'Route',mode:'Matching mode',all:'All four modes',global:'Global',early:'Early',middle:'Middle',late:'Late',run:'Start processing',force:'Force a fresh run (keep existing results)',notice:'One job at a time. Successful cached results are verified by the same pipeline. An assignment is not a calibrated confirmation of trip identity.',availability:'Raw data and GTFS availability',progress:'Processing progress',idle:'Not started',progressEmpty:'Actual stage states and row counts appear after starting.',history:'Saved runs',refresh:'Refresh',result:'Results and exports',stageCounts:'Stage row counts',downloads:'Download CSV / Parquet / JSON',format:'Format',allFormats:'All formats',rawSummary:'Full summary',footer:'Localhost only · No external resources · Published SHA-256 checked before download',loadingDates:'Loading available dates…',chooseDate:'Select a date',noDates:'No processable dates',available:'Available',unavailable:'Unavailable',reason:'Reason',queued:'Queued',running:'Processing',complete:'Complete',failed:'Failed',cached:'Reused verified cached results',fresh:'Created fresh processing results',pass:'Validation passed',fail:'Validation failed',pending:'Complete results not published',performed:'Performed segments',assigned:'Assigned',unmatched:'Unassigned',emptyRuns:'No saved runs yet.',zip:'Download TIDES ZIP for this mode',noOutputs:'No published downloads are available.',field:'Field',count:'Count',noReasons:'No unmatched reasons',connection:'Connection interrupted; retrying…' }
};
let lang = localStorage.getItem('experiment05-language') === 'en' ? 'en' : 'zh';
let availabilityData = null, savedRuns = [], currentDetail = null, currentJob = null, busy = false, summaryRequestSerial = 0;
const $ = id => document.getElementById(id);
const t = key => words[lang][key] || key;
const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = String(value ?? '—'); if (className) node.className = className; return node; };
const count = value => Number.isFinite(value) ? value.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US') : '—';
function error(message) { $('error').hidden = !message; $('error').textContent = message || ''; }
async function api(path, body) {
  const response = await fetch(path, { cache:'no-store', ...(body ? { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) } : {}) });
  const payload = await response.json();
  if (!response.ok) { const exception = new Error(payload.message || payload.error || String(response.status)); exception.status = response.status; throw exception; }
  return payload;
}
function table(headers, rows) {
  const element = document.createElement('table'), head = document.createElement('thead'), heading = document.createElement('tr'), body = document.createElement('tbody');
  headers.forEach(value => heading.append(text('th', value))); head.append(heading); element.append(head);
  rows.forEach(values => { const row = document.createElement('tr'); values.forEach(value => row.append(text('td', value))); body.append(row); }); element.append(body); return element;
}
function renderAvailability() {
  const selected = $('service-date').value; $('service-date').replaceChildren();
  const placeholder = text('option', availabilityData?.dates?.some(day => day.available) ? t('chooseDate') : t('noDates')); placeholder.value = ''; $('service-date').append(placeholder);
  for (const day of availabilityData?.dates || []) { const option = text('option', day.service_date + (day.available ? '' : ' · ' + t('unavailable'))); option.value = day.service_date; option.disabled = !day.available; $('service-date').append(option); }
  $('service-date').value = selected;
  $('availability-table').replaceChildren(table([t('date'),'AVL','APC','GTFS',t('reason')], (availabilityData?.dates || []).map(day => [day.service_date,count(day.avl_rows),count(day.apc_rows),day.gtfs_available ? t('available') : t('unavailable'),day.reason || '—'])));
  renderAvailabilityNote();
}
function renderAvailabilityNote() {
  const day = availabilityData?.dates?.find(row => row.service_date === $('service-date').value);
  $('availability-note').textContent = day ? `AVL ${count(day.avl_rows)} · APC ${count(day.apc_rows)} · GTFS ${day.gtfs_available ? t('available') : t('unavailable')}` : t('chooseDate');
  $('run-button').disabled = busy || day?.available !== true;
  $('run-button').textContent = busy ? t('running') : t('run');
}
function renderRuns() {
  $('run-list').replaceChildren();
  if (!savedRuns.length) $('run-list').append(text('p', t('emptyRuns'), 'muted'));
  for (const run of savedRuns) { const button = document.createElement('button'); button.type = 'button'; button.append(text('span', run.run_id), text('span', t(run.status), 'badge ' + run.status)); button.addEventListener('click', () => loadSummary(run.run_id)); $('run-list').append(button); }
}
function renderJob() {
  if (!currentJob) return;
  $('job-status').textContent = t(currentJob.state); $('job-status').className = 'badge ' + currentJob.state;
  $('job-id').textContent = currentJob.job_id + (currentJob.result ? ' · ' + t(currentJob.result.cached ? 'cached' : 'fresh') : '');
  $('stage-progress').replaceChildren();
  for (const event of currentJob.progress || []) {
    const counts = event.row_counts || event.stage_row_counts || {};
    const extras = Object.keys(counts).length ? ' · ' + JSON.stringify(counts) : event.completed !== undefined && event.total !== undefined ? ` · ${count(event.completed)} / ${count(event.total)}` : event.performed_trip_count !== undefined ? ' · trips: ' + count(event.performed_trip_count) : '';
    $('stage-progress').append(text('li', `${event.stage ?? 'pipeline'} · ${event.state ?? ''}${extras}`));
  }
  if (!(currentJob.progress || []).length) $('stage-progress').append(text('li', t('progressEmpty'), 'muted'));
}
function metric(value, label) { const box = text('div', '', 'metric'); box.append(text('strong', count(value)), text('span', label)); return box; }
function renderDownloads() {
  $('downloads').replaceChildren(); if (!currentDetail) return;
  const files = (currentDetail.downloads || []).filter(row => $('download-filter').value === 'all' || row.format === $('download-filter').value);
  for (const row of files) { const link = text('a', row.file); link.href = `/api/runs/${encodeURIComponent(currentDetail.run_id)}/download?` + new URLSearchParams({file:row.file}); $('downloads').append(link); }
  if (!files.length) $('downloads').append(text('p', t('noOutputs'), 'muted'));
}
function renderSummary() {
  if (!currentDetail) return;
  $('result-panel').hidden = false;
  const summary = currentDetail.summary;
  $('result-run-id').textContent = currentDetail.run_id;
  const validated = summary?.validation_ok;
  $('validator-status').textContent = t(validated === true ? 'pass' : validated === false ? 'fail' : 'pending');
  $('validator-status').className = 'badge ' + (validated === true ? 'pass' : validated === false ? 'fail' : '');
  $('summary-metrics').replaceChildren(metric(summary?.performed_trip_count, t('performed')));
  for (const [name,value] of Object.entries(summary?.source_counts || {})) if (Number.isFinite(value)) $('summary-metrics').append(metric(value, name));
  $('mode-results').replaceChildren();
  for (const [mode, value] of Object.entries(summary?.modes || {})) {
    const card = text('article', '', 'mode-card'); card.dataset.mode = mode; card.append(text('h3', t(mode)));
    const counts = text('div', '', 'counts'); for (const [key,label] of [['matched_count','assigned'],['unmatched_count','unmatched']]) { const item = text('div',''); item.append(text('strong',count(value[key])),text('span',t(label))); counts.append(item); } card.append(counts);
    card.append(text('p','TIDES · '+t(value.tides_validation_passed ? 'pass' : 'fail')));
    const reasons = document.createElement('ul'); for (const [reason, amount] of Object.entries(value.unmatched_reasons || {})) reasons.append(text('li',`${reason}: ${count(amount)}`));
    if (!reasons.childElementCount) reasons.append(text('li',t('noReasons'))); card.append(reasons);
    if (value.tides_validation_passed && currentDetail.status === 'complete') { const link = text('a',t('zip')); link.href = `/api/runs/${encodeURIComponent(currentDetail.run_id)}/download?` + new URLSearchParams({mode}); card.append(link); }
    if (value.table_row_counts) card.append(table([t('field'),t('count')],Object.entries(value.table_row_counts).map(([key,n]) => [key,count(n)])));
    $('mode-results').append(card);
  }
  $('stage-counts').replaceChildren(table([t('field'),t('count')],Object.entries(summary?.stage_row_counts || {}).map(([key,value]) => [key,typeof value === 'object' ? JSON.stringify(value) : count(value)])));
  $('summary-json').textContent = JSON.stringify(summary || currentDetail.run_manifest,null,2); renderDownloads();
  window.Experiment05Viz?.setRun(currentDetail, lang);
}
function translate() {
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  document.querySelectorAll('[data-i18n]').forEach(node => node.textContent = t(node.dataset.i18n));
  $('language-switch').textContent = lang === 'zh' ? 'English' : '中文';
  if (availabilityData) renderAvailability(); renderRuns(); renderJob(); renderSummary();
  window.Experiment05Viz?.setLanguage(lang);
}
async function loadRuns() { savedRuns = (await api('/api/runs')).runs || []; renderRuns(); }
async function loadSummary(runId) {
  const serial = ++summaryRequestSerial;
  try { error(''); const detail = await api(`/api/runs/${encodeURIComponent(runId)}/summary`); if(serial !== summaryRequestSerial)return; currentDetail = detail; renderSummary(); }
  catch (exception) { if(serial === summaryRequestSerial)error(exception.message); }
}
async function poll(jobId) {
  try {
    currentJob = await api(`/api/jobs/${jobId}`); renderJob();
    if (['complete','failed'].includes(currentJob.state)) { busy = false; renderAvailabilityNote(); await loadRuns(); if (currentJob.error) error(currentJob.error); if (currentJob.result) await loadSummary(currentJob.result.run_id); return; }
    setTimeout(() => poll(jobId),1500);
  } catch (exception) { if (exception.status === 404) { busy = false; renderAvailabilityNote(); error(exception.message); } else { error(t('connection')); setTimeout(() => poll(jobId),3000); } }
}
$('run-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return; error(''); busy = true; renderAvailabilityNote();
  try { currentJob = await api('/api/runs',{service_date:$('service-date').value,route_id:'29',match_mode:$('match-mode').value,force:$('force').checked}); renderJob(); poll(currentJob.job_id); }
  catch (exception) { busy = false; renderAvailabilityNote(); error(exception.message); }
});
$('service-date').addEventListener('change',renderAvailabilityNote);
$('download-filter').addEventListener('change',renderDownloads);
$('refresh-runs').addEventListener('click', () => loadRuns().catch(exception => error(exception.message)));
$('language-switch').addEventListener('click', () => { lang = lang === 'zh' ? 'en' : 'zh'; localStorage.setItem('experiment05-language',lang); translate(); });
translate();
Promise.all([api('/api/availability').then(data => { availabilityData = data; renderAvailability(); }),loadRuns()]).catch(exception => error(exception.message));
