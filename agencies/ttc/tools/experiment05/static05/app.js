'use strict';
const labels = { queued:'Queued', running:'Processing', complete:'Complete', failed:'Failed', global:'Global', early:'Early', middle:'Middle', late:'Late' };
let availabilityData = null, savedRuns = [], currentDetail = null, currentJob = null, busy = false, summaryRequestSerial = 0;
const $ = id => document.getElementById(id);
const label = key => labels[key] || key;
const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = String(value ?? '—'); if (className) node.className = className; return node; };
const count = value => Number.isFinite(value) ? value.toLocaleString('en-US') : '—';
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
  const placeholder = text('option', availabilityData?.dates?.some(day => day.available) ? 'Select a date' : 'No processable dates'); placeholder.value = ''; $('service-date').append(placeholder);
  for (const day of availabilityData?.dates || []) { const option = text('option', day.service_date + (day.available ? '' : ' · Unavailable')); option.value = day.service_date; option.disabled = !day.available; $('service-date').append(option); }
  $('service-date').value = selected;
  $('availability-table').replaceChildren(table(['Data date','AVL','APC','GTFS','Reason'], (availabilityData?.dates || []).map(day => [day.service_date,count(day.avl_rows),count(day.apc_rows),day.gtfs_available ? 'Available' : 'Unavailable',day.reason || '—'])));
  renderAvailabilityNote();
}
function renderAvailabilityNote() {
  const day = availabilityData?.dates?.find(row => row.service_date === $('service-date').value);
  $('availability-note').textContent = day ? `AVL ${count(day.avl_rows)} · APC ${count(day.apc_rows)} · GTFS ${day.gtfs_available ? 'Available' : 'Unavailable'}` : 'Select a date';
  $('run-button').disabled = busy || day?.available !== true;
  $('run-button').textContent = busy ? 'Processing' : 'Start processing';
}
function renderRuns() {
  $('run-list').replaceChildren();
  if (!savedRuns.length) $('run-list').append(text('p', 'No saved runs yet.', 'muted'));
  for (const run of savedRuns) { const button = document.createElement('button'); button.type = 'button'; button.append(text('span', run.run_id), text('span', label(run.status), 'badge ' + run.status)); button.addEventListener('click', () => loadSummary(run.run_id)); $('run-list').append(button); }
}
function renderJob() {
  if (!currentJob) return;
  $('job-status').textContent = label(currentJob.state); $('job-status').className = 'badge ' + currentJob.state;
  $('job-id').textContent = currentJob.job_id + (currentJob.result ? ' · ' + (currentJob.result.cached ? 'Reused verified cached results' : 'Created fresh processing results') : '');
  $('stage-progress').replaceChildren();
  for (const event of currentJob.progress || []) {
    const counts = event.row_counts || event.stage_row_counts || {};
    const extras = Object.keys(counts).length ? ' · ' + JSON.stringify(counts) : event.completed !== undefined && event.total !== undefined ? ` · ${count(event.completed)} / ${count(event.total)}` : event.performed_trip_count !== undefined ? ' · trips: ' + count(event.performed_trip_count) : '';
    $('stage-progress').append(text('li', `${event.stage ?? 'pipeline'} · ${event.state ?? ''}${extras}`));
  }
  if (!(currentJob.progress || []).length) $('stage-progress').append(text('li', 'Actual stage states and row counts appear after starting.', 'muted'));
}
function metric(value, label) { const box = text('div', '', 'metric'); box.append(text('strong', count(value)), text('span', label)); return box; }
function renderDownloads() {
  $('downloads').replaceChildren(); if (!currentDetail) return;
  const files = (currentDetail.downloads || []).filter(row => $('download-filter').value === 'all' || row.format === $('download-filter').value);
  for (const row of files) { const link = text('a', row.file); link.href = `/api/runs/${encodeURIComponent(currentDetail.run_id)}/download?` + new URLSearchParams({file:row.file}); $('downloads').append(link); }
  if (!files.length) $('downloads').append(text('p', 'No published downloads are available.', 'muted'));
}
function renderSummary() {
  if (!currentDetail) return;
  $('result-panel').hidden = false;
  const summary = currentDetail.summary;
  $('result-run-id').textContent = currentDetail.run_id;
  const validated = summary?.validation_ok;
  $('validator-status').textContent = validated === true ? 'Validation passed' : validated === false ? 'Validation failed' : 'Complete results not published';
  $('validator-status').className = 'badge ' + (validated === true ? 'pass' : validated === false ? 'fail' : '');
  $('summary-metrics').replaceChildren(metric(summary?.performed_trip_count, 'Performed segments'));
  for (const [name,value] of Object.entries(summary?.source_counts || {})) if (Number.isFinite(value)) $('summary-metrics').append(metric(value, name));
  $('mode-results').replaceChildren();
  for (const [mode, value] of Object.entries(summary?.modes || {})) {
    const card = text('article', '', 'mode-card'); card.dataset.mode = mode; card.append(text('h3', label(mode)));
    const counts = text('div', '', 'counts'); for (const [key,name] of [['matched_count','Assigned'],['unmatched_count','Unassigned']]) { const item = text('div',''); item.append(text('strong',count(value[key])),text('span',name)); counts.append(item); } card.append(counts);
    card.append(text('p','TIDES · '+(value.tides_validation_passed ? 'Validation passed' : 'Validation failed')));
    const reasons = document.createElement('ul'); for (const [reason, amount] of Object.entries(value.unmatched_reasons || {})) reasons.append(text('li',`${reason}: ${count(amount)}`));
    if (!reasons.childElementCount) reasons.append(text('li','No unmatched reasons')); card.append(reasons);
    if (value.tides_validation_passed && currentDetail.status === 'complete') { const link = text('a','Download TIDES ZIP for this mode'); link.href = `/api/runs/${encodeURIComponent(currentDetail.run_id)}/download?` + new URLSearchParams({mode}); card.append(link); }
    if (value.table_row_counts) card.append(table(['Field','Count'],Object.entries(value.table_row_counts).map(([key,n]) => [key,count(n)])));
    $('mode-results').append(card);
  }
  $('stage-counts').replaceChildren(table(['Field','Count'],Object.entries(summary?.stage_row_counts || {}).map(([key,value]) => [key,typeof value === 'object' ? JSON.stringify(value) : count(value)])));
  $('summary-json').textContent = JSON.stringify(summary || currentDetail.run_manifest,null,2); renderDownloads();
  window.Experiment05Viz?.setRun(currentDetail);
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
  } catch (exception) { if (exception.status === 404) { busy = false; renderAvailabilityNote(); error(exception.message); } else { error('Connection interrupted; retrying…'); setTimeout(() => poll(jobId),3000); } }
}
$('run-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return; error(''); busy = true; renderAvailabilityNote();
  try { currentJob = await api('/api/runs',{service_date:$('service-date').value,route_id:'29',match_mode:$('match-mode').value,force:$('force').checked}); renderJob(); poll(currentJob.job_id); }
  catch (exception) { busy = false; renderAvailabilityNote(); error(exception.message); }
});
$('service-date').addEventListener('change',renderAvailabilityNote);
$('download-filter').addEventListener('change',renderDownloads);
$('refresh-runs').addEventListener('click', () => loadRuns().catch(exception => error(exception.message)));
renderRuns();
Promise.all([api('/api/availability').then(data => { availabilityData = data; renderAvailability(); }),loadRuns()]).catch(exception => error(exception.message));
