/* Read-only visualizations of persisted Experiment05 results. No matching or clock shifting. */
(function (global) {
  'use strict';
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const validGeo = point => finite(point?.lat) && finite(point?.lon) && Math.abs(point.lat) <= 90 && Math.abs(point.lon) <= 180;
  function viewport(bounds, span, position = 0) {
    let start = finite(bounds?.start_t) ? bounds.start_t : 0;
    let end = finite(bounds?.end_t) && bounds.end_t >= start ? bounds.end_t : start;
    if (end === start) { start -= .5; end += .5; }
    const total = end - start;
    const width = span === 'full' || !finite(Number(span)) || Number(span) <= 0 ? total : Math.min(total, Number(span));
    const pan = Math.max(0, Math.min(1000, Number(position) || 0)) / 1000;
    return { start: start + (total - width) * pan, end: start + (total - width) * pan + width, total, width, full: width >= total };
  }
  function geographicFrame(points, width = 1000, height = 420, padding = 36) {
    const valid = points.filter(validGeo);
    if (!valid.length) return null;
    const latitude = valid.reduce((sum, point) => sum + point.lat, 0) / valid.length;
    const longitude = valid.reduce((sum, point) => sum + point.lon, 0) / valid.length;
    const radians = Math.PI / 180, radius = 6371008.8, cosine = Math.max(.001, Math.cos(latitude * radians));
    const project = point => ({ x: (point.lon - longitude) * radians * radius * cosine, y: (point.lat - latitude) * radians * radius });
    const projected = valid.map(project);
    const xs = projected.map(point => point.x), ys = projected.map(point => point.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const scale = Math.min((width - 2 * padding) / Math.max(100, maxX - minX), (height - 2 * padding) / Math.max(100, maxY - minY));
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    return { latitude, longitude, scale, project, pixel: point => { const p = project(point); return { x: width / 2 + (p.x - cx) * scale, y: height / 2 - (p.y - cy) * scale }; } };
  }
  function pathData(points, x, y, xField = 't', yField = 's_m') {
    let previous = false;
    return points.map(point => {
      if (!finite(point?.[xField]) || !finite(point?.[yField])) { previous = false; return ''; }
      const px = x(point[xField]), py = y(point[yField]);
      if (!finite(px) || !finite(py)) { previous = false; return ''; }
      const command = previous ? 'L' : 'M'; previous = true;
      return `${command}${px.toFixed(2)},${py.toFixed(2)}`;
    }).join(' ');
  }
  function interval(row) {
    const a = finite(row.arrival) ? row.arrival : finite(row.t) ? row.t : row.departure;
    const b = finite(row.departure) ? row.departure : a;
    return finite(a) && finite(b) && b >= a ? [a, b] : null;
  }
  const math = Object.freeze({ finite, validGeo, viewport, geographicFrame, pathData, interval });
  global.Experiment05VizMath = math;
  if (typeof document === 'undefined') return;

  const MODES = ['global', 'early', 'middle', 'late'], palette = ['#426bb2', '#8b58a8', '#b3781d'];
  const labels = {
    zh: { eyebrow:'已保存的观测 · 只读检查',title:'Trip 可视化检查',tripView:'逐趟检查',dayView:'全天总览',decisionMode:'已计算的匹配模式',global:'全局',early:'前期',middle:'中期',late:'后期',identityNotice:'已分配不等于已校准的真实身份；未分配时展示的候选仅用于诊断。时间轴使用原始时钟，不按估计延误平移。',loading:'正在读取已保存的可视化数据…',vehicle:'车辆',direction:'方向',search:'搜索 Trip / 车辆 / 时间',previous:'上一趟',next:'下一趟',performedTrip:'分割后的 Trip',viewSpan:'浏览窗口',fullTrip:'完整 Trip',fullDay:'全天',map:'地理位置与线路形状',fitMap:'适合线路范围',mapNote:'真实经纬度、北向上；不使用外部底图。APC仅显示有依据的站点位置，不制造GPS轨迹。可滚轮缩放、拖动地图。',timeline:'AVL · APC · GTFS 时间线',progress:'时间—线路进度',progressNote:'只连接同一连续有效投影片段。OFF_REFERENCE、被截尾部分和真实时间缺口不会被伪装成连续服务轨迹。',diagnostics:'本趟诊断与证据说明',dayNote:'按方向和canonical reference分面；每趟只显示已分配班次，或明确标记为诊断的排名1候选。点击任意Trip进入逐趟检查。',all:'全部',unknown:'未知',assigned:'已分配',unassigned:'未分配',rank:'排名',cost:'评分（越低越好）',delay:'估计延误',invalid:'硬约束未通过 · 诊断',diagnostic:'未分配候选 · 仅诊断',alternative:'备选候选 · 未被分配',noCandidates:'没有保存的候选；保留原始观测供检查。',noTrips:'当前筛选下没有 Trip。',noProjection:'没有可展示的有效投影片段。',noCoordinates:'没有真实空间坐标，无法绘制地理地图。',reference:'参考形状',validAvl:'有效AVL',offReference:'OFF_REFERENCE',censored:'截尾 / 排除',apcStop:'APC站点位置',rawBoundary:'原始边界',effectiveBoundary:'有效边界',selected:'当前模式证据范围',scheduled:'GTFS计划',distance:'线路进度 / km',time:'当地时间',notComputed:'未计算',retry:'重试',noEvidence:'NO_EVIDENCE：当前部分没有足够匹配证据',selectionNote:'按有效投影行驶距离选择；非实时前缀预测',rawExtent:'原始范围',effectiveExtent:'有效范围',noApC:'APC未提供有效时间区间',sourceDisplay:'总览显示点 / 原始点',detailFull:'逐趟保留全部点；总览仅作显示简化',minutes:'分钟',boundary:'边界',boarding:'上车',alighting:'下车',apcPosition:'站点坐标，非车辆GPS',selectedForDecision:'参与当前模式',notSelected:'仅上下文',mapMissing:'无坐标记录',sourceRows:'源观测',invalidTime:'无有效时间',apcRange:'APC站点进度区间',assignmentReason:'未分配原因',pattern:'形状 / 方向',source:'来源',clickTrip:'点击打开本趟',validation:'已验证的保存结果',candidateOnly:'候选不是已确认班次',mapScale:'近似地面距离',noOverview:'没有全天投影数据。',candidateDetails:'候选评分与约束',noTimeline:'没有可绘制的有效时间记录。' },
    en: { eyebrow:'Saved observations · read-only inspection',title:'Trip visual inspection',tripView:'Trip detail',dayView:'Full-day overview',decisionMode:'Computed matching mode',global:'Global',early:'Early',middle:'Middle',late:'Late',identityNotice:'An assignment is not calibrated confirmation of trip identity. Unassigned candidates are diagnostics only. All timelines retain original clocks; schedules are never delay-shifted.',loading:'Reading saved visualization evidence…',vehicle:'Vehicle',direction:'Direction',search:'Search trip / vehicle / time',previous:'Previous',next:'Next',performedTrip:'Performed segment',viewSpan:'View window',fullTrip:'Full trip',fullDay:'Full day',map:'Geographic positions and route shapes',fitMap:'Fit route extent',mapNote:'Real longitude/latitude, north up, no external basemap. APC symbols are supported stop positions, never fabricated GPS pings. Wheel to zoom; drag to pan.',timeline:'AVL · APC · GTFS timeline',progress:'Time–distance progress',progressNote:'Only continuous valid projection fragments are connected. Rejected points, censored tails and real gaps are not depicted as continuous service.',diagnostics:'Trip diagnostics and evidence notes',dayNote:'Separate panels for each direction and canonical reference. Each segment shows its assigned schedule, or an explicitly diagnostic rank1 fallback. Click a trip to inspect it.',all:'All',unknown:'Unknown',assigned:'Assigned',unassigned:'Unassigned',rank:'Rank',cost:'Cost (lower is better)',delay:'Estimated delay',invalid:'Hard constraints failed · diagnostic',diagnostic:'Unassigned candidate · diagnostic only',alternative:'Alternative candidate · not assigned',noCandidates:'No persisted candidates; original observations remain available for inspection.',noTrips:'No trips satisfy these filters.',noProjection:'No valid projected service fragments to display.',noCoordinates:'No real coordinates are available for a geographic map.',reference:'Reference shape',validAvl:'Valid AVL',offReference:'OFF_REFERENCE',censored:'Censored / excluded',apcStop:'APC stop position',rawBoundary:'Raw boundary',effectiveBoundary:'Effective boundary',selected:'Evidence for selected mode',scheduled:'GTFS schedule',distance:'Canonical progress / km',time:'Local time',notComputed:'Not computed',retry:'Retry',noEvidence:'NO_EVIDENCE: this portion has insufficient matching evidence',selectionNote:'Selected by valid projected travel distance; not real-time prefix prediction',rawExtent:'Raw extent',effectiveExtent:'Effective extent',noApC:'APC has no valid time interval',sourceDisplay:'Overview display / source points',detailFull:'Trip detail keeps every point; overview simplification is display-only',minutes:'min',boundary:'Boundary',boarding:'Boarding',alighting:'Alighting',apcPosition:'Stop coordinate, not vehicle GPS',selectedForDecision:'Selected for this mode',notSelected:'Context only',mapMissing:'Records without coordinates',sourceRows:'Source observations',invalidTime:'No valid time',apcRange:'APC stop-progress interval',assignmentReason:'Unassigned reason',pattern:'Shape / direction',source:'Source',clickTrip:'Open this trip',validation:'Validated saved results',candidateOnly:'Candidates are not confirmed schedules',mapScale:'Approximate ground distance',noOverview:'No full-day projection evidence.',candidateDetails:'Candidate costs and constraints',noTimeline:'No valid timestamps are available to plot.' }
  };
  Object.assign(labels.zh,{unattachedApc:'未附着 APC',sharedDiagnostics:'见下方共享诊断下载',emptyFacet:'此参考形状没有可展示的观测或计划班次。',emptyWindow:'当前浏览窗口内没有观测或计划班次；可调整窗口或横向移动。',actualLegend:'实际有效轨迹 · 实线',assignedLegend:'已分配计划 · 绿色虚线',fallbackLegend:'排名1诊断计划 · 虚线',apcUnused:'APC证据未用于匹配',apcUsed:'APC证据已纳入匹配',geometryBasis:'几何来源：裁剪到候选进度范围的canonical映射，不是独立原始shape绘图',mobileSwipe:'小屏幕图表可左右滑动；上方时间滑块控制数据浏览窗口。'});
  Object.assign(labels.en,{unattachedApc:'Unattached APC',sharedDiagnostics:'See shared diagnostic downloads below',emptyFacet:'No observations or scheduled candidate are available on this reference.',emptyWindow:'No observations or schedules in this view window; resize or pan the window.',actualLegend:'Actual valid trajectory · solid',assignedLegend:'Assigned schedule · green dashed',fallbackLegend:'Rank1 diagnostic schedule · dashed',apcUnused:'APC evidence not used for matching',apcUsed:'APC evidence included for matching',geometryBasis:'Geometry: canonical mapping cropped to candidate progress, not an independent original-shape drawing',mobileSwipe:'Swipe small-screen charts horizontally; the time slider above controls the data window.'});
  labels.zh.fullTrip='完整对比范围';labels.en.fullTrip='Full comparison range';
  const state = { run:null, runId:null, language:'zh', mode:'global', catalog:null, trip:null, overview:null, tripId:null, page:'trip', generation:0, tripRequest:0, controllers:new Set(), selected:new Set(), map:{scale:1,dx:0,dy:0}, timezone:'America/Toronto', serviceDate:null };
  const $ = id => document.getElementById(id), tr = key => labels[state.language][key] || key;
  const node = (tag, value, className) => { const el = document.createElement(tag); if (value !== undefined) el.textContent = String(value ?? '—'); if (className) el.className = className; return el; };
  const svgNode = (tag, attributes = {}, value) => { const el = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, item] of Object.entries(attributes)) if (item !== undefined && item !== null) el.setAttribute(key, String(item)); if (value !== undefined) el.textContent = value; return el; };
  const number = (value, decimals = 0) => finite(value) ? value.toLocaleString(state.language === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits:decimals }) : '—';
  function clock(t, seconds = false) {
    if (!finite(t)) return '—';
    return new Intl.DateTimeFormat('en-GB', { timeZone:state.timezone, hour:'2-digit', minute:'2-digit', ...(seconds ? { second:'2-digit' } : {}), hourCycle:'h23' }).format(new Date(t * 1000));
  }
  function datedClock(t) {
    if (!finite(t)) return '—';
    const day = new Intl.DateTimeFormat('en-CA',{timeZone:state.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(t * 1000));
    return (day === state.serviceDate ? '' : day + ' ') + clock(t, true);
  }
  const title = (element, value) => { element.append(svgNode('title', {}, value)); return element; };
  const color = candidate => candidate.assigned ? '#23815c' : palette[Math.max(0, Math.min(2, (candidate.candidate_rank || 1) - 1))];
  const candidateKey = candidate => String(candidate.candidate_id ?? candidate.trip_id_scheduled ?? candidate.trip_id ?? candidate.candidate_rank);
  const candidateTrip = candidate => String(candidate.trip_id_scheduled ?? candidate.trip_id ?? candidate.candidate_id ?? '—');
  const role = candidate => candidate.assigned ? tr('assigned') : candidate.hard_constraints_valid === false ? tr('invalid') : state.trip?.assignment?.trip_id_scheduled ? tr('alternative') : tr('diagnostic');
  function blank(target, key) { $(target).replaceChildren(node('p',tr(key),'viz-empty')); }
  function error(message) {
    const el = $('viz-error'); el.hidden = !message; el.replaceChildren();
    if (message) { el.append(node('span',message),document.createTextNode(' ')); const retry=node('button',tr('retry'),'secondary'); retry.type='button'; retry.addEventListener('click',reload); el.append(retry); }
  }
  function busy(value) { $('viz-loading').hidden = !value; $('viz-panel').setAttribute('aria-busy',String(value)); }
  async function request(kind, extra = {}) {
    const controller = new AbortController(); state.controllers.add(controller);
    try {
      const query = new URLSearchParams({mode:state.mode,...extra});
      const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/visualization/${kind}?${query}`,{cache:'no-store',signal:controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || data.error || String(response.status));
      if (data.schema !== 'experiment05-visualization-v1') throw new Error('Unsupported visualization schema');
      return data;
    } finally { state.controllers.delete(controller); }
  }
  function stopRequests() { for (const controller of state.controllers) controller.abort(); state.controllers.clear(); }
  function translate() {
    document.querySelectorAll('[data-viz-i18n]').forEach(el => { el.textContent = tr(el.dataset.vizI18n); });
    $('viz-run-status').textContent = tr('validation');
    $('viz-pan').setAttribute('aria-label',tr('viewSpan')); $('viz-day-pan').setAttribute('aria-label',tr('viewSpan'));
  }
  function updateModes() {
    for (const option of $('viz-mode').options) { option.disabled = !state.run?.summary?.match_modes?.includes(option.value); option.textContent = tr(option.value) + (option.disabled ? ' · '+tr('notComputed') : ''); }
    $('viz-mode').value = state.mode;
  }
  function tripLabel(trip) { return `${trip.vehicle_id ?? '—'} · ${trip.direction_label || trip.direction_id || tr('unknown')} · ${clock(trip.raw_start_t)}–${clock(trip.raw_end_t)} · ${trip.assignment_status === 'ASSIGNED' ? tr('assigned') : tr('unassigned')}`; }
  function filteredTrips() {
    const vehicle = $('viz-vehicle').value, direction = $('viz-direction').value, search = $('viz-search').value.trim().toLowerCase();
    return (state.catalog?.trips || []).filter(trip => (!vehicle || String(trip.vehicle_id) === vehicle) && (!direction || String(trip.direction_id) === direction) && (!search || [trip.trip_id_performed,tripLabel(trip),trip.vehicle_id,trip.direction_id,trip.direction_label].join(' ').toLowerCase().includes(search)));
  }
  function options(select, values, chosen) { select.replaceChildren(...values.map(([value,label]) => { const item=node('option',label); item.value=value; return item; })); if (values.some(([value])=>value===chosen)) select.value=chosen; }
  function renderFilters() {
    const vehicle = $('viz-vehicle').value, direction = $('viz-direction').value;
    const vehicles=[...new Set((state.catalog?.trips || []).map(trip=>String(trip.vehicle_id ?? '')))].filter(Boolean).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
    options($('viz-vehicle'),[['',tr('all')],...vehicles.map(value=>[value,value])],vehicle);
    options($('viz-direction'),[['',tr('all')],...(state.catalog?.directions || []).map(item=>[String(item.direction_id),item.direction_label || String(item.direction_id)])],direction);
    renderTripOptions();
  }
  function renderTripOptions() {
    const trips=filteredTrips();
    options($('viz-trip'),trips.map(trip=>[trip.trip_id_performed,tripLabel(trip)]),state.tripId);
    if (!trips.length) options($('viz-trip'),[['',tr('noTrips')]],'');
    const index=trips.findIndex(trip=>trip.trip_id_performed===$('viz-trip').value);
    $('viz-previous').disabled=index<=0; $('viz-next').disabled=index<0 || index>=trips.length-1;
    $('viz-trip-count').textContent=`${tr('performedTrip')}: ${index<0?0:index+1} / ${number(trips.length)} · ${number(state.catalog?.trips?.length)} ${tr('all')}`;
    if(finite(state.catalog?.counts?.unattached_apc))$('viz-trip-count').textContent+=` · ${tr('unattachedApc')}: ${number(state.catalog.counts.unattached_apc)} (${tr('sharedDiagnostics')})`;
  }
  function clearTrip() {
    state.trip=null; $('viz-trip-metadata').replaceChildren(); $('viz-candidate-list').replaceChildren(); $('viz-selection-note').textContent=''; $('viz-diagnostics').textContent='';
    for (const id of ['viz-map','viz-timeline','viz-progress']) blank(id,'noTrips');
  }
  async function reload() {
    if (!state.runId) return;
    const generation=++state.generation; clearTimeout(searchTimer); stopRequests(); error(null); busy(true); state.catalog=null; state.overview=null; clearTrip(); $('viz-day-charts').replaceChildren();
    try {
      const catalog=await request('catalog'); if(generation!==state.generation)return;
      state.catalog=catalog; state.timezone=catalog.timezone || 'America/Toronto'; state.serviceDate=catalog.service_date;
      renderFilters();
      if(state.page==='day') await loadOverview();
      else if($('viz-trip').value) await loadTrip($('viz-trip').value);
      else busy(false);
    } catch(exception) { if(exception.name!=='AbortError' && generation===state.generation) { error(exception.message); busy(false); } }
  }
  async function loadTrip(identity) {
    if(!identity){++state.tripRequest;state.tripId=null;clearTrip();error(null);busy(false);return;}
    const generation=state.generation, serial=++state.tripRequest, changed=identity!==state.tripId;
    state.tripId=identity; renderTripOptions(); clearTrip(); error(null); busy(true);
    if(changed){$('viz-pan').value='0';state.map={scale:1,dx:0,dy:0};}
    try {
      const data=await request('trip',{trip_id:identity}); if(generation!==state.generation || serial!==state.tripRequest)return;
      state.trip=data; state.selected=new Set((data.candidates || []).filter(candidate=>candidate.assigned).map(candidateKey));
      if(!state.selected.size && data.candidates?.length)state.selected.add(candidateKey(data.candidates[0]));
      renderTrip();busy(false);
    }catch(exception){if(exception.name!=='AbortError'&&generation===state.generation&&serial===state.tripRequest){error(exception.message);busy(false);}}
  }
  async function loadOverview() {
    if(state.overview){renderOverview();busy(false);return;}
    const generation=state.generation;error(null);busy(true);
    try{const data=await request('overview');if(generation!==state.generation)return;state.overview=data;renderOverview();busy(false);}catch(exception){if(exception.name!=='AbortError'&&generation===state.generation){error(exception.message);busy(false);}}
  }
  function showPage(page) {
    state.page=page;$('viz-trip-page').hidden=page!=='trip';$('viz-day-page').hidden=page!=='day';
    $('viz-trip-tab').setAttribute('aria-selected',String(page==='trip'));$('viz-day-tab').setAttribute('aria-selected',String(page==='day'));
    if(page==='day')loadOverview();else if(!state.trip && $('viz-trip').value)loadTrip($('viz-trip').value);else if(state.trip)renderTripCharts();
  }
  function openTrip(identity) {
    $('viz-vehicle').value='';$('viz-direction').value='';$('viz-search').value='';state.tripId=identity;renderTripOptions();
    state.page='trip';$('viz-trip-page').hidden=false;$('viz-day-page').hidden=true;$('viz-trip-tab').setAttribute('aria-selected','true');$('viz-day-tab').setAttribute('aria-selected','false');
    state.map={scale:1,dx:0,dy:0};$('viz-pan').value='0';loadTrip(identity);$('viz-panel').scrollIntoView({block:'start'});
  }
  function metadata(target, values) { $(target).replaceChildren(...values.filter(([,value])=>value!==undefined&&value!==null&&value!=='').map(([label,value])=>node('span',`${label}: ${value}`))); }
  function renderTrip() {
    const data=state.trip;if(!data)return;
    const trip=data.trip;
    metadata('viz-trip-metadata',[[tr('vehicle'),trip.vehicle_id],[tr('direction'),trip.direction_label||trip.direction_id],[tr('rawExtent'),`${datedClock(trip.raw_start_t)} – ${datedClock(trip.raw_end_t)}`],[tr('effectiveExtent'),`${datedClock(trip.effective_start_t)} – ${datedClock(trip.effective_end_t)}`],['AVL',number(trip.avl_count)],['APC',number(trip.apc_count)],[tr('validAvl'),number(trip.valid_avl_count)],[tr('offReference'),number(trip.off_reference_count)],[tr('censored'),number(trip.censored_count)],[tr('assignmentReason'),trip.unmatched_reason],['Trip ID',trip.trip_id_performed]]);
    renderCandidates();
    const selection=data.selection || {};
    $('viz-selection-note').textContent=state.mode==='global' ? `${tr('global')} · ${tr('selected')}` : `${selection.status==='NO_EVIDENCE'?tr('noEvidence'):tr('selectionNote')} · ${tr('selected')}: ${(selection.time_ranges||[]).map(range=>`${clock(range.start,true)}–${clock(range.end,true)}`).join(', ')||'—'} · AVL ${number(selection.selected_valid_avl_count)}`;
    $('viz-diagnostics').textContent=JSON.stringify({trip:data.trip,assignment:data.assignment,selection:data.selection,source_counts:{avl:data.avl?.length,apc:data.apc?.length},references:(data.references||[]).map(reference=>({reference_id:reference.reference_id,family_id:reference.family_id,direction_id:reference.direction_id,length_m:reference.length_m})),fragments:(data.fragments||[]).map(fragment=>({fragment_id:fragment.fragment_id,reference_id:fragment.reference_id,source_point_count:fragment.source_point_count,display_point_count:fragment.display_point_count})),bounds:data.bounds},null,2);
    renderTripCharts();
  }
  function renderCandidates() {
    const root=$('viz-candidate-list');root.replaceChildren();
    for(const candidate of state.trip?.candidates || []){
      const key=candidateKey(candidate),card=node('article',undefined,'viz-candidate'+(candidate.assigned?' assigned':'')+(candidate.hard_constraints_valid===false?' invalid':''));
      card.dataset.candidateId=key;card.dataset.rank=String(candidate.candidate_rank??'');card.dataset.assigned=String(candidate.assigned===true);card.style.setProperty('--candidate-color',color(candidate));
      const label=node('label'),check=node('input');check.type='checkbox';check.checked=state.selected.has(key);check.addEventListener('change',()=>{if(check.checked)state.selected.add(key);else state.selected.delete(key);renderTripCharts();});
      label.append(check,node('strong',`${tr('rank')} ${candidate.candidate_rank??'—'} · ${candidateTrip(candidate)}`));card.append(label,node('p',role(candidate),candidate.assigned?'':'viz-reason'),node('p',`${tr('cost')}: ${number(candidate.score,5)} · ${tr('delay')}: ${number(candidate.estimated_delay_s??candidate.delay_s,1)} s`));
      const reasons=[...(candidate.hard_constraint_reasons||[]),...(candidate.reasons||[])];if(reasons.length)card.append(node('p',reasons.join(' · '),'viz-reason'));
      const details=node('details'),summary=node('summary',tr('candidateDetails'));details.append(summary,node('pre',JSON.stringify(Object.fromEntries(Object.entries(candidate).filter(([name])=>!['trajectory','schedule','shape_geo'].includes(name))),null,2)));card.append(details);root.append(card);
    }
    if(!root.childElementCount)root.append(node('p',tr('noCandidates'),'viz-empty'));
  }
  function selectedCandidates() { return (state.trip?.candidates||[]).filter(candidate=>state.selected.has(candidateKey(candidate))); }
  function selectedRow(source,rid) { return (state.trip?.selection?.[`selected_${source}_ids`]||[]).some(value=>value===rid || value===`${source}|${rid}`); }
  function tripViewport() { return viewport(state.trip?.bounds,$('viz-span').value,$('viz-pan').value); }
  function setWindowUI(day,view) {
    const prefix=day?'viz-day':'viz';$(`${prefix}-pan`).disabled=view.full;
    $(`${prefix}-window-label`).textContent=`${datedClock(view.start)} – ${datedClock(view.end)} · ${number(view.width/60,1)} ${tr('minutes')}`;
  }
  function renderTripCharts() { if(!state.trip)return;const view=tripViewport();setWindowUI(false,view);renderMap(view);renderTimeline(view);renderProgress(view); }
  function visible(t,view) { return finite(t)&&t>=view.start&&t<=view.end; }
  function overlaps(times,view) { return times && times[1]>=view.start&&times[0]<=view.end; }
  function observationColor(row) { return row.censored_tail ? '#b98529' : row.projection_status==='OFF_REFERENCE' ? '#ce5360' : row.exclude_from_matching ? '#b98529' : row.trajectory_included ? '#227cac' : '#8797a0'; }
  function renderMap(view) {
    const data=state.trip,root=$('viz-map'),width=1000,height=420;
    const allGeo=[...(data.references||[]).flatMap(reference=>reference.points||[]),...(data.avl||[]),...(data.apc||[]),...(data.candidates||[]).flatMap(candidate=>candidate.shape_geo||[])];
    const frame=geographicFrame(allGeo,width,height);if(!frame){blank('viz-map','noCoordinates');return;}
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':tr('map'),'data-coordinate-system':'local-equirectangular-metres','data-origin-lat':frame.latitude,'data-origin-lon':frame.longitude});
    const group=svgNode('g'),transform=()=>`translate(${state.map.dx} ${state.map.dy}) translate(${width/2} ${height/2}) scale(${state.map.scale}) translate(${-width/2} ${-height/2})`;group.setAttribute('transform',transform());
    function geographicPath(points,attributes){let open=false;const commands=points.map(point=>{if(!validGeo(point)){open=false;return '';}const p=frame.pixel(point),command=open?'L':'M';open=true;return `${command}${p.x.toFixed(2)},${p.y.toFixed(2)}`;}).join(' ');return svgNode('path',{d:commands,'vector-effect':'non-scaling-stroke',...attributes});}
    for(const reference of data.references||[])group.append(title(geographicPath(reference.points||[],{class:'map-shape','data-reference-id':reference.reference_id}),`${tr('reference')}: ${reference.reference_id}`));
    for(const candidate of selectedCandidates())group.append(title(geographicPath(candidate.shape_geo||[],{class:'map-candidate',stroke:color(candidate),'data-candidate-id':candidateKey(candidate),'data-assigned':candidate.assigned===true,'data-geometry-basis':candidate.geometry_basis}),`${role(candidate)} · ${candidateTrip(candidate)}\n${tr('geometryBasis')}\n${candidate.geometry_basis||'—'}`));
    const byRid=new Map((data.avl||[]).map(point=>[point.rid,point]));
    for(const fragment of data.fragments||[]){const points=(fragment.points||[]).map(point=>byRid.get(point.rid)).filter(point=>point&&visible(point.t,view));if(points.length>1)group.append(geographicPath(points,{class:'map-avl','data-fragment-id':fragment.fragment_id}));}
    for(const row of data.avl||[]){if(!validGeo(row)||!visible(row.t,view))continue;const p=frame.pixel(row),selected=row.selected_for_mode??selectedRow('avl',row.rid);const mark=svgNode('circle',{cx:p.x,cy:p.y,r:row.projection_status==='OFF_REFERENCE'?3.5:2.3,fill:observationColor(row),opacity:row.duplicate_of ? .5 : 1,'data-source':'avl','data-rid':row.rid,'data-projection-status':row.projection_status,'data-selected-for-decision':selected});title(mark,`${clock(row.t,true)} · ${row.rid}\n${row.projection_status||''} · ${number(row.projection_distance_m,1)} m\n${row.censored_tail?tr('censored'):selected?tr('selectedForDecision'):tr('notSelected')}`);group.append(mark);}
    for(const row of data.apc||[]){if(!validGeo(row)||!overlaps(interval(row),view))continue;const p=frame.pixel(row),mark=svgNode('path',{d:`M${p.x},${p.y-5} L${p.x+5},${p.y} L${p.x},${p.y+5} L${p.x-5},${p.y} Z`,class:'map-apc','data-source':'apc','data-coordinate-role':row.coordinate_role,'data-rid':row.rid});title(mark,`${tr('apcPosition')} · ${row.stop_name||row.stop_id||'—'}\n${clock(row.arrival,true)}–${clock(row.departure,true)}\n${tr('boarding')}: ${row.boarding??'—'} · ${tr('alighting')}: ${row.alighting??'—'}`);group.append(mark);}
    svg.append(group);svg.append(svgNode('path',{d:'M965 48 L965 20 M959 30 L965 20 L971 30',stroke:'#536b75',fill:'none','stroke-width':1.5}),svgNode('text',{x:965,y:15,'text-anchor':'middle'},'N'));
    const scaleGroup=svgNode('g'),updateScale=()=>{const pixelsPerMetre=frame.scale*state.map.scale,target=100/pixelsPerMetre,power=10**Math.floor(Math.log10(target)),metres=([1,2,5,10].map(value=>value*power).filter(value=>value<=target).at(-1)||power),length=metres*pixelsPerMetre;scaleGroup.replaceChildren(svgNode('path',{d:`M25 384 v7 h${length.toFixed(2)} v-7`,fill:'none',stroke:'#425c67','stroke-width':2}),svgNode('text',{x:25,y:409},`${number(metres)} m · ${tr('mapScale')}`));};updateScale();svg.append(scaleGroup);
    svg.addEventListener('wheel',event=>{event.preventDefault();state.map.scale=Math.max(.5,Math.min(15,state.map.scale*(event.deltaY<0?1.15:1/1.15)));group.setAttribute('transform',transform());updateScale();},{passive:false});
    let pointer=null;svg.addEventListener('pointerdown',event=>{pointer={x:event.clientX,y:event.clientY};svg.setPointerCapture(event.pointerId);});svg.addEventListener('pointermove',event=>{if(!pointer)return;const rect=svg.getBoundingClientRect();state.map.dx+=(event.clientX-pointer.x)*width/rect.width;state.map.dy+=(event.clientY-pointer.y)*height/rect.height;pointer={x:event.clientX,y:event.clientY};group.setAttribute('transform',transform());});svg.addEventListener('pointerup',()=>pointer=null);svg.addEventListener('pointercancel',()=>pointer=null);
    root.replaceChildren(svg);const legend=$('viz-map-legend');legend.replaceChildren(...[[tr('reference'),'#b5c5cc'],[tr('validAvl'),'#227cac'],[tr('offReference'),'#ce5360'],[tr('censored'),'#b98529'],[tr('apcStop'),'#a96126']].map(([name,value])=>{const label=node('span'),swatch=node('i',undefined,'viz-swatch');swatch.style.setProperty('--color',value);label.append(swatch,document.createTextNode(name));return label;}));
    legend.append(node('span',`${tr('mapMissing')}: AVL ${(data.avl||[]).filter(row=>!validGeo(row)).length} · APC ${(data.apc||[]).filter(row=>!validGeo(row)).length}`));
  }
  let clipSerial=0;
  function chart(view,height=260,maxS=null) {
    const width=1000,left=72,right=24,top=25,bottom=38,innerHeight=height-top-bottom;
    const x=value=>left+(value-view.start)/(view.end-view.start)*(width-left-right), y=value=>height-bottom-value/Math.max(1,maxS||1)*innerHeight;
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,'data-view-start':view.start,'data-view-end':view.end,role:'img','aria-label':maxS===null?tr('timeline'):tr('progress')});
    for(let i=0;i<=6;i++){const t=view.start+(view.end-view.start)*i/6,px=x(t);svg.append(svgNode('line',{x1:px,x2:px,y1:top,y2:height-bottom,class:'grid'}),svgNode('text',{x:px,y:height-13,'text-anchor':'middle'},clock(t)));}
    if(maxS!==null)for(let i=0;i<=4;i++){const s=maxS*i/4,py=y(s);svg.append(svgNode('line',{x1:left,x2:width-right,y1:py,y2:py,class:'grid'}),svgNode('text',{x:left-9,y:py+4,'text-anchor':'end'},number(s/1000,1)));}
    svg.append(svgNode('line',{x1:left,x2:width-right,y1:height-bottom,y2:height-bottom,class:'axis'}));
    const clipId='viz-clip-'+(++clipSerial),defs=svgNode('defs'),clip=svgNode('clipPath',{id:clipId});clip.append(svgNode('rect',{x:left,y:top-10,width:width-left-right,height:innerHeight+20}));defs.append(clip);svg.append(defs);const plot=svgNode('g',{'clip-path':`url(#${clipId})`});svg.append(plot);
    return {svg,plot,x,y,left,right,width,height,top,bottom};
  }
  function evidenceBands(chartObject,view,detail=true) {
    const {plot,x,top,height,bottom}=chartObject;
    if(detail)for(const range of state.trip?.selection?.time_ranges||[]){if(!finite(range.start)||!finite(range.end)||range.end<view.start||range.start>view.end)continue;plot.append(svgNode('rect',{x:x(range.start),y:top-8,width:Math.max(1,x(range.end)-x(range.start)),height:height-top-bottom+16,class:'selected-band','data-start':range.start,'data-end':range.end}));}
    if(detail){const trip=state.trip.trip;for(const [key,label,style]of [['raw_start_t','rawBoundary','boundary-raw'],['raw_end_t','rawBoundary','boundary-raw'],['effective_start_t','effectiveBoundary','boundary-effective'],['effective_end_t','effectiveBoundary','boundary-effective']])if(finite(trip[key]))plot.append(title(svgNode('line',{x1:x(trip[key]),x2:x(trip[key]),y1:top-8,y2:height-bottom+6,class:style,'data-boundary':key}),`${tr(label)} · ${clock(trip[key],true)}`));}
  }
  function renderTimeline(view) {
    const candidates=selectedCandidates(),height=160+candidates.length*35,{svg,plot,x,left,width,right}=chart(view,height);
    evidenceBands({plot,x,top:25,height,bottom:38},view);
    const lanes=[['AVL',42],['APC',88],...candidates.map((candidate,index)=>[`${tr('rank')} ${candidate.candidate_rank}${candidate.assigned?' ✓':''}`,128+index*35])];
    for(const [name,y]of lanes)svg.append(svgNode('text',{x:left-12,y:y+4,'text-anchor':'end'},name),svgNode('line',{x1:left,x2:width-right,y1:y,y2:y,stroke:'#e2e9ea'}));
    for(const row of state.trip.avl||[]){if(!finite(row.t))continue;const mark=svgNode('circle',{cx:x(row.t),cy:42,r:2.7,fill:observationColor(row),'data-source':'avl','data-rid':row.rid,'data-selected-for-decision':selectedRow('avl',row.rid)});title(mark,`${clock(row.t,true)} · ${row.projection_status||''}\n${row.rid}`);plot.append(mark);}
    for(const [index,row]of (state.trip.apc||[]).entries()){const times=interval(row);if(!times)continue;const y=88+(index%3-1)*5,mark=svgNode('line',{x1:x(times[0]),x2:x(times[1])+Math.max(0,1-(x(times[1])-x(times[0]))),y1:y,y2:y,stroke:row.apc_evidence_included?'#a96126':'#9da7aa','stroke-width':4,'data-source':'apc','data-rid':row.rid,'data-start':times[0],'data-end':times[1],'data-selected-for-decision':selectedRow('apc',row.rid)});title(mark,`${row.stop_name||row.stop_id||'—'} · ${clock(times[0],true)}–${clock(times[1],true)}\n${tr('boarding')}: ${row.boarding??'—'} · ${tr('alighting')}: ${row.alighting??'—'}\n${row.apc_attachment_reason||''}`);plot.append(mark);}
    candidates.forEach((candidate,index)=>{const y=128+index*35,points=(candidate.trajectory||[]).filter(point=>finite(point.t));if(!points.length)return;const group=svgNode('g',{'data-candidate-id':candidateKey(candidate),'data-assigned':candidate.assigned===true,'data-clock':'original-scheduled'});group.append(title(svgNode('line',{x1:x(Math.min(...points.map(point=>point.t))),x2:x(Math.max(...points.map(point=>point.t))),y1:y,y2:y,stroke:color(candidate),'stroke-width':2,'stroke-dasharray':'5 3'}),`${role(candidate)} · ${candidateTrip(candidate)}`));for(const stop of candidate.schedule||[]){if(!finite(stop.arrival_t)&&!finite(stop.departure_t))continue;const a=finite(stop.arrival_t)?stop.arrival_t:stop.departure_t,b=finite(stop.departure_t)?stop.departure_t:a;group.append(title(svgNode('rect',{x:x(a),y:y-4,width:Math.max(2,x(b)-x(a)),height:8,fill:color(candidate),'data-stop-id':stop.stop_id}),`${stop.stop_id} · ${clock(a,true)}–${clock(b,true)}`));}plot.append(group);});
    $('viz-timeline').replaceChildren(svg);
  }
  function fragmentMarks(parent,fragment,axes,attributes={}) {
    const points=fragment.points||[],path=svgNode('path',{d:pathData(points,axes.x,axes.y),class:'actual-fragment','data-fragment-id':fragment.fragment_id,'data-reference-id':fragment.reference_id,...attributes});
    parent.append(path);
    if(points.length===1&&finite(points[0].t)&&finite(points[0].s_m))parent.append(svgNode('circle',{cx:axes.x(points[0].t),cy:axes.y(points[0].s_m),r:2.7,fill:'#227cac',...attributes}));
  }
  function scheduleMark(parent,candidate,axes,extra={}) {
    const path=svgNode('path',{d:pathData(candidate.trajectory||[],axes.x,axes.y),class:'scheduled-trajectory',stroke:color(candidate),'data-candidate-id':candidateKey(candidate),'data-rank':candidate.candidate_rank,'data-assigned':candidate.assigned===true,'data-clock':'original-scheduled',...extra});title(path,`${candidateTrip(candidate)} · ${tr('rank')} ${candidate.candidate_rank} · ${candidate.assigned?tr('assigned'):tr('diagnostic')}`);parent.append(path);
  }
  function renderProgress(view) {
    const data=state.trip,root=$('viz-progress');root.replaceChildren();
    const ids=new Set([...(data.references||[]).map(reference=>reference.reference_id),...(data.fragments||[]).map(fragment=>fragment.reference_id),...selectedCandidates().map(candidate=>candidate.reference_id)].filter(Boolean));
    for(const id of ids){const reference=(data.references||[]).find(reference=>reference.reference_id===id),fragments=(data.fragments||[]).filter(fragment=>fragment.reference_id===id),candidates=selectedCandidates().filter(candidate=>candidate.reference_id===id),intervals=(data.apc||[]).flatMap(row=>(row.canonical_stop_intervals||[]).filter(item=>item.reference_id===id).map(item=>({row,item})));
      const allS=[reference?.length_m,...fragments.flatMap(fragment=>(fragment.points||[]).map(point=>point.s_m)),...candidates.flatMap(candidate=>(candidate.trajectory||[]).map(point=>point.s_m)),...intervals.flatMap(({item})=>[item.s_min_m,item.s_max_m])].filter(finite),maximum=Math.max(1,...allS);
      const section=node('section',undefined,'viz-progress-facet'),heading=node('p',`${reference?.direction_label||reference?.direction_id||''} · ${id} · ${tr('distance')}`,'viz-note');section.append(heading);
      const axes=chart(view,270,maximum);axes.svg.dataset.referenceId=id;evidenceBands(axes,view);
      for(const fragment of fragments)fragmentMarks(axes.plot,fragment,axes,{'data-trip-id':data.trip.trip_id_performed});
      for(const {row,item}of intervals){const times=interval(row);if(!times||!finite(item.s_min_m)||!finite(item.s_max_m))continue;const x1=axes.x(times[0]),x2=axes.x(times[1]),y1=axes.y(item.s_max_m),y2=axes.y(item.s_min_m);const mark=svgNode('rect',{x:x1,y:y1-1,width:Math.max(2,x2-x1),height:Math.max(3,y2-y1+2),fill:row.apc_evidence_included?'#a96126':'#9da7aa',opacity:item.ambiguous ? .35 : .8,'data-source':'apc','data-rid':row.rid,'data-reference-id':id,'data-position-role':'STOP_INTERVAL_EVIDENCE','data-evidence-included':row.apc_evidence_included===true,'data-ambiguous':item.ambiguous===true});title(mark,`${row.stop_name||row.stop_id||'—'} · ${tr('apcRange')}: ${number(item.s_min_m)}–${number(item.s_max_m)} m\n${clock(times[0],true)}–${clock(times[1],true)}\n${tr(row.apc_evidence_included?'apcUsed':'apcUnused')} · ${row.apc_attachment_reason||''}`);axes.plot.append(mark);}
      for(const candidate of candidates)scheduleMark(axes.plot,candidate,axes);section.append(axes.svg);root.append(section);
    }
    if(!root.childElementCount)blank('viz-progress','noProjection');
  }
  function renderOverview() {
    if(!state.overview)return;const data=state.overview,view=viewport(data.bounds,$('viz-day-span').value,$('viz-day-pan').value);setWindowUI(true,view);
    const root=$('viz-day-charts');root.replaceChildren();let sourceCount=0,displayCount=0;const uniqueTrips=new Set();
    for(const facet of data.facets||[]){const section=node('section',undefined,'viz-day-facet viz-chart');section.dataset.referenceId=facet.reference_id;section.dataset.directionId=String(facet.direction_id);section.append(node('h3',`${facet.direction_label||facet.direction_id||tr('unknown')} · ${facet.reference_id}`));
      const values=[facet.length_m,...(facet.segments||[]).flatMap(segment=>(segment.fragments||[]).flatMap(fragment=>(fragment.points||[]).map(point=>point.s_m)))].filter(finite),axes=chart(view,330,Math.max(1,...values));axes.svg.dataset.referenceId=facet.reference_id;
      for(const segment of facet.segments||[]){uniqueTrips.add(segment.trip_id_performed);const group=svgNode('g',{class:'viz-overview-trip',tabindex:0,role:'button','data-trip-id':segment.trip_id_performed,'data-display-role':segment.display_role,'aria-label':`${tr('clickTrip')} ${segment.vehicle_id} ${segment.trip_id_performed}`});
        title(group,`${segment.vehicle_id} · ${segment.trip_id_performed}\n${segment.display_role==='ASSIGNED'?tr('assigned'):segment.display_role==='DIAGNOSTIC_FALLBACK'?tr('diagnostic'):tr('noCandidates')}`);
        for(const fragment of segment.fragments||[]){sourceCount+=fragment.source_point_count??fragment.points?.length??0;displayCount+=fragment.display_point_count??fragment.points?.length??0;fragmentMarks(group,fragment,axes,{'data-trip-id':segment.trip_id_performed});}
        if(segment.candidate && segment.candidate.reference_id===facet.reference_id)scheduleMark(group,{...segment.candidate,assigned:segment.display_role==='ASSIGNED'},axes,{'data-display-role':segment.display_role});
        group.addEventListener('click',()=>openTrip(segment.trip_id_performed));group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openTrip(segment.trip_id_performed);}});axes.plot.append(group);
      }
      const renderedPoints=(facet.segments||[]).flatMap(segment=>[...(segment.fragments||[]).flatMap(fragment=>fragment.points||[]),...(segment.candidate?.reference_id===facet.reference_id?segment.candidate.trajectory||[]:[])]).filter(point=>finite(point.t)&&finite(point.s_m));
      const inWindow=(facet.segments||[]).some(segment=>[...(segment.fragments||[]).map(fragment=>fragment.points||[]),...(segment.candidate?.reference_id===facet.reference_id?[segment.candidate.trajectory||[]]:[])].some(points=>{const times=points.filter(point=>finite(point.t)&&finite(point.s_m)).map(point=>point.t);return times.length&&Math.max(...times)>=view.start&&Math.min(...times)<=view.end;}));
      if(!renderedPoints.length||!inWindow)section.append(node('p',tr(renderedPoints.length?'emptyWindow':'emptyFacet'),'viz-empty'));
      section.append(axes.svg);root.append(section);
    }
    metadata('viz-day-metrics',[[tr('performedTrip'),uniqueTrips.size],[tr('sourceDisplay'),`${number(displayCount)} / ${number(sourceCount)}`],[tr('detailFull'),tr(state.mode)],[tr('unattachedApc'),state.catalog?.counts?.unattached_apc]]);
    $('viz-day-legend').replaceChildren(...[[tr('actualLegend'),'#227cac',false],[tr('assignedLegend'),'#23815c',true],[tr('fallbackLegend'),palette[0],true]].map(([label,color,dashed])=>{const item=node('span'),swatch=node('i',undefined,'viz-swatch'+(dashed?' dashed':''));swatch.style.setProperty('--color',color);item.append(swatch,document.createTextNode(label));return item;}));
    if(!root.childElementCount)blank('viz-day-charts','noOverview');
  }
  global.Experiment05Viz = {
    setRun(detail,language) {
      const changed=state.runId!==detail?.run_id,languageChanged=state.language!==(language==='en'?'en':'zh');state.language=language==='en'?'en':'zh';state.run=detail;
      const enabled=detail?.status==='complete'&&detail.summary?.validation_ok===true;$('viz-panel').hidden=!enabled;
      if(!enabled){stopRequests();++state.generation;state.runId=null;state.catalog=null;state.overview=null;clearTrip();return;}
      state.runId=detail.run_id;state.serviceDate=detail.summary.service_date;$('viz-run-id').textContent=detail.run_id;
      if(!detail.summary.match_modes.includes(state.mode))state.mode=MODES.find(mode=>detail.summary.match_modes.includes(mode))||'global';
      translate();updateModes();
      if(changed){state.tripId=null;$('viz-vehicle').replaceChildren();$('viz-direction').replaceChildren();$('viz-search').value='';state.map={scale:1,dx:0,dy:0};$('viz-pan').value='0';$('viz-day-pan').value='0';reload();}
      else if(languageChanged){if(state.catalog)renderFilters();if(state.trip)renderTrip();if(state.overview)renderOverview();}
    },
    setLanguage(language) {
      if(state.language===(language==='en'?'en':'zh'))return;state.language=language==='en'?'en':'zh';translate();updateModes();
      if(state.catalog)renderFilters();if(state.trip)renderTrip();if(state.overview)renderOverview();
    },
    snapshot:()=>({run_id:state.runId,mode:state.mode,trip_id:state.tripId,page:state.page,language:state.language,selected_candidates:[...state.selected],view:state.trip?tripViewport():null})
  };
  $('viz-mode').addEventListener('change',()=>{state.mode=$('viz-mode').value;reload();});
  for(const id of ['viz-vehicle','viz-direction'])$(id).addEventListener('change',()=>{renderTripOptions();loadTrip($('viz-trip').value);});
  let searchTimer;$('viz-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{renderTripOptions();loadTrip($('viz-trip').value);},180);});
  $('viz-trip').addEventListener('change',()=>loadTrip($('viz-trip').value));
  for(const [id,step]of [['viz-previous',-1],['viz-next',1]])$(id).addEventListener('click',()=>{const trips=filteredTrips(),index=trips.findIndex(trip=>trip.trip_id_performed===state.tripId),next=trips[index+step];if(next)loadTrip(next.trip_id_performed);});
  $('viz-trip-tab').addEventListener('click',()=>showPage('trip'));$('viz-day-tab').addEventListener('click',()=>showPage('day'));
  $('viz-span').addEventListener('change',()=>{$('viz-pan').value='0';renderTripCharts();});$('viz-pan').addEventListener('input',renderTripCharts);
  $('viz-day-span').addEventListener('change',()=>{$('viz-day-pan').value='0';renderOverview();});$('viz-day-pan').addEventListener('input',renderOverview);
  $('viz-map-fit').addEventListener('click',()=>{state.map={scale:1,dx:0,dy:0};if(state.trip)renderMap(tripViewport());});
  translate();
})(globalThis);
