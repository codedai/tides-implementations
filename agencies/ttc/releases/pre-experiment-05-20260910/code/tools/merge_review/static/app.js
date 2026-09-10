/* Local, read-only source inspection. No external resources until map opt-in. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const tr = (value) => window.TidesI18n?.translate(value) ?? value;
  const NS = "http://www.w3.org/2000/svg";
  const COLORS = { direct: "#258d70", partial: "#c7993f", ambiguous: "#ce7160", unmatched: "#93a3a8", ineligible: "#9986b1", not_evaluated: "#8296a4", avl: "#168b78", counter: "#cf625f", apc: "#ca983f" };
  const STATUS = { direct: "直接支持", partial: "部分支持", ambiguous: "歧义", unmatched: "未关联", ineligible: "不具备条件", not_evaluated: "未执行匹配" };
  const LEVEL = { bounded_interval: "到离站有界支持", instant: "时刻支持", arrival_only: "仅到站支持", departure_only: "仅离站支持", interior_only: "仅区间内部支持" };
  const VERDICT = { accepted: "可接受", problem: "有问题", uncertain: "待确认", unreviewed: "未审查" };
  const METHOD = { gtfs_trip_matching: "GTFS · Route 29 计划候选", direction_destination: "实验 02 · 方向 + Destination 候选行程", vehicle_only: "仅车辆号汇集 · vehicle_only", vehicle_only_dedup: "车辆号汇集 + AVL 完全重复去重 · vehicle_only_dedup", gap_only: "仅时间间隔 · gap_only", avl_context: "AVL 上下文 · avl_context", apc_assist: "APC 辅助 · apc_assist", apc_assisted: "APC 辅助 · apc_assisted" };
  const PARAMETER_LABEL = { gap_seconds: "AVL 长缺口切分（秒）", context_min_points: "上下文最少持续点数", context_min_seconds: "上下文最少持续时间（秒）", boundary_cooldown_seconds: "候选边界冷却时间（秒）", reversal_window_points: "反向检测窗口点数", reversal_min_displacement_m: "反向检测最少位移（米）", reversal_angle_degrees: "反向夹角门槛（度）", reversal_min_straightness: "反向窗口最小直线程度", apc_min_direction_points: "APC 方向持续站点数", apc_anchor_max_gap_seconds: "APC 边界锚定最大间隔（秒）", same_time_conflict_distance_m: "同刻 GPS 位置冲突（米）", time_tolerance_s: "时间匹配容差（秒）", radius_m: "局部空间半径（米）", interpolation_gap_s: "插值最大括区（秒）", episode_gap_s: "局部访问最大缺口（秒）", contradiction_radius_m: "时空反证距离门槛（米）", contemporaneous_s: "反证近同时范围（秒）", max_speed_kph: "允许运动速度上界（km/h）", max_apc_dwell_s: "APC 到离站区间上限（秒）", accept_score: "接受评分门槛（非概率）", null_score: "无关联候选评分", margin_score: "最佳候选最小分差", max_episodes: "局部候选访问数量上限", max_measurements_per_record: "单 APC 实测点检查上限", route_policy: "线路标签冲突策略" };
  const RELATION = { accepted: "已采纳关联", candidate: "候选证据", context: "邻近上下文", unassigned: "未分配" };
  const MATCHING_MODES = { global: "全局 · 整段", early: "前期 · 前 1/3", middle: "中期 · 中 1/3", late: "后期 · 后 1/3" };
  const state = { meta: null, cases: [], filtered: [], reviews: {}, detail: null, selected: null, method: "", vehicle: "", caseId: "", contextSeconds: 180, sequence: 0, limit: 100, dirty: false, saving: false, revision: 0, cursor: null, view: null, basemap: false, map: { center: null, scale: 1 }, tableRows: [], chartFrame: 0, debug: null, run: "", base: "", job: null, pendingRun: null, comparison: null, compareSequence: 0, runSequence: 0, selectedObservation: null, observationPage: 0, observationRows: [] };
  Object.assign(state, { matchingMode: "global", page: "detail", modeSequence: 0, overviewSequence: 0, overview: null, overviewStart: null, overviewSize: 86400 });
  const fmt = (v) => v == null || v === "" ? "—" : typeof v === "number" ? v.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : String(v);
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  const timestamp = (v) => { if (finite(v)) return v; if (typeof v === "string") { const n = Number(v); if (Number.isFinite(n) && v.trim()) return n; const t = Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(v) ? v : v.replace(" ", "T") + "Z"); return Number.isFinite(t) ? t / 1000 : null; } return null; };
  const time = (v, seconds = false) => { const t = timestamp(v); return t == null ? "—" : new Date(t * 1000).toISOString().slice(11, seconds ? 19 : 16); };
  const date = (v) => { const t = timestamp(v); return t == null ? "—" : new Date(t * 1000).toISOString().slice(0, 10); };
  const range = (a, b) => `${time(a)} – ${time(b)}`;
  const arr = (v) => Array.isArray(v) ? v : [];
  const rid = (v) => String(v == null ? "" : v);
  const txt = (id, value) => { $(id).textContent = value; };
  const json = (v) => JSON.stringify(v, null, 2);
  const node = (tag, className, text) => { const e = document.createElement(tag); if (className) e.className = className; if (text != null) e.textContent = text; return e; };
  const svg = (tag, attrs = {}, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v); if (text != null) e.textContent = text; return e; };
  const setBadge = (e, status) => { e.className = `status-badge status-${Object.hasOwn(STATUS, status) ? status : "unknown"}`; e.textContent = STATUS[status] || status || "未选择"; };
  const selectedAPC = () => state.detail && arr(state.detail.apc).find((r) => rid(r.rid) === state.selected);
  const decision = (r) => r.decision || {};
  // These methods inspect source observations without running the APC–AVL matcher.
  const streamMethod = (method) => ["vehicle_only", "vehicle_only_dedup", "direction_destination", "gtfs_trip_matching"].includes(method);
  const isGTFS = () => state.method === "gtfs_trip_matching" || state.meta?.representation === "gtfs_trip_candidates";
  const isDirection = () => isGTFS() || state.method === "direction_destination" || ["direction_candidate_segments", "direction_candidate_trips"].includes(state.meta?.representation);
  const isStream = () => streamMethod(state.method) || state.meta?.representation === "vehicle_observation_stream" || state.detail?.case?.kind === "vehicle_stream";
  const isDedup = () => ["vehicle_only_dedup", "direction_destination", "gtfs_trip_matching"].includes(state.method) || state.meta?.avl_deduplication === true;
  const directionLabel = (row) => row?.direction_label ?? row?.direction ?? "UNKNOWN";
  const conflictCount = (row) => row?.direction_conflict_count ?? row?.conflict_count ?? arr(row?.direction_conflict_ids || row?.conflict_ids).length;
  const shortCandidate = (row) => isDirection() && row?.kind === "direction_segment" && timestamp(row.end) - timestamp(row.start) >= 0 && timestamp(row.end) - timestamp(row.start) < 60;
  const candidateRange = (row) => row?.gap_split && row.source_start != null && row.source_end != null ? `${time(row.source_start, true)}–${time(row.source_end, true)}` : shortCandidate(row) ? `${time(row.start, true)}–${time(row.end, true)}` : range(row.start, row.end);
  const shortCandidateNote = (row) => `短候选 · ${fmt(timestamp(row.end) - timestamp(row.start))} s（非确认 trip）`;
  const directionConflicts = () => arr(state.detail?.direction_conflicts || state.detail?.conflicts);
  const candidateIds = (row) => arr(row.review_candidate_ids ?? row.candidate_segment_ids ?? row.candidate_ids).map(rid);
  const sourceConflictIds = (source, row) => arr(row.direction_conflict_ids || state.directionSources?.get(`${source}|${rid(row.rid || row.source_rid)}`)?.direction_conflict_ids);
  const sourceColor = (source, row) => isDirection() && sourceConflictIds(source, row).length ? COLORS.counter : source === "avl" ? COLORS.avl : COLORS[rowStatus(row)] || COLORS.apc;
  const rowStatus = (r) => isStream() ? "not_evaluated" : r.status || decision(r).status || "unmatched";
  const rowsAVL = () => arr(state.detail?.avl).filter((r) => r.in_window !== false && (!state.view || timestamp(r.t) >= state.view[0] && timestamp(r.t) <= state.view[1]));
  const visibleAVL = () => rowsAVL().filter((r) => $("layer-context").checked || r.in_focus !== false);
  const visibleAPC = () => arr(state.detail?.apc).filter((r) => (!state.view || Math.max(timestamp(r.arrival) ?? -Infinity, timestamp(r.departure) ?? -Infinity) >= state.view[0] && Math.min(timestamp(r.arrival) ?? Infinity, timestamp(r.departure) ?? Infinity) <= state.view[1]) && ($("layer-context").checked || r.in_focus !== false || rid(r.rid) === state.selected));
  const validCoords = (r) => finite(r.lat) && finite(r.lon) && Math.abs(r.lat) < 85.051129 && Math.abs(r.lon) <= 180 && !(r.lat === 0 && r.lon === 0);
  const validGPS = (r) => r.valid !== false && validCoords(r) && timestamp(r.t) != null;
  const continuous = (p, q) => p && !q.break_before && p.progress_run != null && p.progress_run === q.progress_run && timestamp(q.t) >= timestamp(p.t) && timestamp(q.t) - timestamp(p.t) <= 90;
  function toast(message) { txt("toast", message); $("toast").hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000); }
  function error(message) { txt("global-error", message); $("global-error").hidden = !message; }
  function runPath(path, run = state.run) { if (!run) return path; const url = new URL(path, location.origin); if (!url.searchParams.has("run")) url.searchParams.set("run", run); return url.pathname + url.search; }
  async function api(path, options = {}) { if (state.debug?.enabled && /^\/api\/(meta|catalog|case|reviews)(?:[/?]|$)/.test(path)) path = runPath(path); const response = await fetch(path, { credentials: "same-origin", ...options }); let body; try { body = await response.json(); } catch { const e = new Error(`服务器返回无效数据（${response.status}）`); e.status = response.status; throw e; } if (!response.ok) { const e = new Error(typeof body.error === "string" ? body.error : json(body.error) || `请求失败（${response.status}）`); e.status = response.status; throw e; } return body; }
  const matchingQuery = (mode = state.matchingMode) => isGTFS() ? `&matching_mode=${encodeURIComponent(mode)}` : "";
  const availableMatchingModes = () => arr(state.detail?.gtfs?.available_matching_modes || state.overview?.available_matching_modes || state.meta?.available_matching_modes).length ? arr(state.detail?.gtfs?.available_matching_modes || state.overview?.available_matching_modes || state.meta?.available_matching_modes) : ["global"];
  function renderMatchingControls() {
    const active = isGTFS(); $("matching-view-toolbar").hidden = !active;
    if (!active) state.page = "detail";
    const modes = availableMatchingModes();
    for (const option of $("matching-mode-select").options) { option.disabled = !modes.includes(option.value); option.textContent = MATCHING_MODES[option.value] + (!modes.includes(option.value) ? " · 本运行未计算" : ""); }
    $("matching-mode-select").value = state.matchingMode; $("matching-mode-select").disabled = !!state.modeLoading || !!state.runLoading;
    const selection = state.detail?.gtfs?.decision_selection, partial = state.matchingMode !== "global";
    txt("matching-mode-note", state.modeLoading ? "正在读取该模式独立计算的候选与评分；不修改原始数据或审查笔记…" : `${MATCHING_MODES[state.matchingMode]}：${partial ? "按有效投影行驶距离选取对应三分之一；OFF_REFERENCE、缺口和参考轴跳变不计长度。" : "使用每个候选片段的全段证据，不是车辆全天联合优化。"}${selection && partial ? ` 本段选中 ${fmt(selection.effective_progress_m)} / ${fmt(selection.total_effective_progress_m)} m。` : ""}${modes.length === 1 ? " 当前历史运行只有全局结果；需新运行计算部分模式。" : " 四种模式独立评分；切换浏览窗不会重算匹配。"}${partial ? " 这是离线部分证据实验：shape projection 已使用完整片段，不是实时前缀预测。" : ""} 审查笔记仍绑定本运行／片段，四模式共享。`);
    $("trip-detail-page").hidden = state.page !== "detail"; $("day-overview-page").hidden = state.page !== "overview";
    $("trip-detail-tab").setAttribute("aria-selected", String(state.page === "detail")); $("day-overview-tab").setAttribute("aria-selected", String(state.page === "overview"));
    document.body.classList.toggle("day-overview-mode", state.page === "overview");
    $("matching-view-toolbar").dataset.matchingMode = state.matchingMode;
  }
  async function switchMatchingMode(mode) {
    if (!Object.hasOwn(MATCHING_MODES, mode) || !availableMatchingModes().includes(mode) || state.runLoading || state.modeLoading) { renderMatchingControls(); return; }
    if (mode === state.matchingMode) return;
    const seq = ++state.modeSequence, run = state.run, caseId = state.caseId, vehicle = state.vehicle;
    state.modeLoading = true; renderMatchingControls(); error("");
    try {
      // Replace only derived GTFS data: a read-only mode switch never reloads or
      // clears the shared run/case review draft, source rows, map or time window.
      const requests = [api(`/api/catalog?method=${encodeURIComponent(state.method)}&vehicle=${encodeURIComponent(vehicle)}${matchingQuery(mode)}`)];
      if (state.detail) requests.push(api(`/api/case?method=${encodeURIComponent(state.method)}&vehicle=${encodeURIComponent(vehicle)}&case_id=${encodeURIComponent(caseId)}&context_s=${state.contextSeconds}${matchingQuery(mode)}`));
      if (state.page === "overview") requests.push(api(runPath(`/api/day-overview?matching_mode=${encodeURIComponent(mode)}`)));
      const [catalog, ...rest] = await Promise.all(requests);
      if (seq !== state.modeSequence || run !== state.run || caseId !== state.caseId || vehicle !== state.vehicle) return;
      state.matchingMode = mode; state.cases = arr(catalog.cases);
      if (state.detail) { const detail = rest.shift(); state.detail.gtfs = detail.gtfs; state.detail.case = { ...state.detail.case, ...detail.case }; state.detail.provenance = detail.provenance; renderDirectionPanel(); initGTFS(); renderCharts(); renderMap(); renderInspector(); renderWarnings(); }
      state.filtered = state.cases.filter(matches); renderQueue(); updateNavigation();
      state.overview = state.page === "overview" ? rest.shift() : null;
      if (state.overview) renderDayOverview();
    } catch (e) { error(`决策模式未切换：${e.message}`); }
    finally { if (seq === state.modeSequence) { state.modeLoading = false; renderMatchingControls(); } }
  }
  async function showMatchingPage(page) {
    if (page === "overview" && !isGTFS()) return;
    state.page = page; renderMatchingControls();
    if (page === "overview") { if (state.overview?.run_id === state.run && state.overview?.matching_mode === state.matchingMode) renderDayOverview(); else await loadDayOverview(); }
    else if (state.detail) { renderCharts(); renderMap(); renderTimelinePan(); }
  }
  async function loadDayOverview() {
    const seq = ++state.overviewSequence, run = state.run, mode = state.matchingMode;
    $("day-overview-page").setAttribute("aria-busy", "true"); txt("day-overview-status", "正在载入全部车辆的有效投影、APC 站点区间及单一 GTFS 候选…"); $("day-overview-facets").replaceChildren(); $("overview-case-list").replaceChildren();
    try {
      const data = await api(runPath(`/api/day-overview?matching_mode=${encodeURIComponent(mode)}`, run));
      if (seq !== state.overviewSequence || run !== state.run || mode !== state.matchingMode) return;
      state.overview = data; renderDayOverview();
    } catch (e) { if (seq === state.overviewSequence) { state.overview = null; txt("day-overview-status", `全天总览未能载入：${e.message}。可点击“刷新总览”重试。`); } }
    finally { if (seq === state.overviewSequence) $("day-overview-page").setAttribute("aria-busy", "false"); }
  }
  function overviewBounds() {
    const day = state.overview?.day || state.meta?.debug_date || "2023-11-06", start = timestamp(`${day}T00:00:00`);
    return [start ?? 1699228800, (start ?? 1699228800) + 86400];
  }
  function overviewView() {
    const [start, end] = overviewBounds(), size = Math.min(86400, Math.max(60, state.overviewSize));
    state.overviewStart = Math.max(start, Math.min(state.overviewStart ?? start, end - size));
    return [state.overviewStart, state.overviewStart + size];
  }
  function updateOverviewWindow() {
    const custom = $("overview-window-size").value === "custom"; $("overview-custom-label").hidden = !custom;
    const size = custom ? Number($("overview-custom-minutes").value) * 60 : Number($("overview-window-size").value);
    if (!finite(size) || size < 60 || size > 86400) { toast("总览窗口应为 1–1440 分钟。原窗口未改变。"); return; }
    state.overviewSize = size; renderDayOverviewCharts();
  }
  function shiftOverview(direction) { const [a] = overviewView(); state.overviewStart = a + direction * state.overviewSize; renderDayOverviewCharts(); }
  async function openOverviewCase(c) {
    if (!confirmLeave()) return;
    state.page = "detail"; renderMatchingControls();
    for (const id of ["route-filter", "status-filter", "review-filter", "case-search", "direction-filter", "direction-conflict-filter"]) $(id).value = "";
    state.lastSearch = "";
    if (rid(c.vehicle) !== state.vehicle) { state.vehicle = rid(c.vehicle); $("vehicle-select").value = state.vehicle; await loadCatalog({ caseId: c.id, vehicle: state.vehicle, full: true }); }
    else { filterCases(false); await loadCase(c.id); }
    $("matching-view-toolbar").scrollIntoView({ block: "start", behavior: "instant" });
  }
  function renderDayOverview() {
    const data = state.overview; if (!data) return;
    const cases = arr(data.cases), accepted = cases.filter((c) => c.candidate?.source === "accepted").length, fallback = cases.filter((c) => c.candidate?.source === "fallback").length;
    txt("day-overview-status", `${data.day} · Route 29 · ${MATCHING_MODES[data.matching_mode] || data.matching_mode} · ${fmt(new Set(cases.map((c) => c.vehicle)).size)} 辆车 / ${fmt(cases.length)} 个片段 · ${fmt(accepted)} 个展示通过门槛候选 / ${fmt(fallback)} 个展示未过门槛候选 / ${fmt(cases.length - accepted - fallback)} 个无已评分候选。总览不受左侧单车筛选限制。`);
    $("day-overview-page").dataset.matchingMode = data.matching_mode; $("day-overview-page").dataset.runId = data.run_id; txt("overview-case-count", `${cases.length} 个`);
    const list = document.createDocumentFragment();
    for (const c of cases) {
      const candidate = c.candidate, button = node("button", "overview-case-card"); button.type = "button"; button.dataset.caseId = c.id; button.dataset.candidateSource = candidate?.source || "none";
      button.append(node("strong", "", `${c.vehicle_label || c.vehicle} · ${c.direction_label || "UNKNOWN"} · ${range(c.start, c.end)}`), node("span", "", `${c.status || "UNKNOWN"} · ${candidate ? `${candidate.source === "fallback" ? "未过门槛 · 诊断首选" : "通过门槛 · 候选首选"} ${candidate.trip_id}` : "无已评分候选"}`), node("small", "mono", c.id));
      if (!arr(c.fragments).some((f) => arr(f.points).length)) button.append(node("small", "", "没有可绘制 AVL 投影；APC 证据和原始记录仍保留"));
      button.addEventListener("click", () => openOverviewCase(c)); list.append(button);
    }
    $("overview-case-list").replaceChildren(list); renderMatchingControls(); renderDayOverviewCharts();
  }
  // Bounded display-only sampling. Never join fragments or delete the endpoints
  // of a decision/context change. Scoring always uses persisted backend evidence.
  function overviewSample(points, limit = 360) {
    const rows = arr(points).filter((p) => finite(timestamp(p[0])) && finite(p[1])); if (rows.length <= limit) return rows;
    const indexes = new Set([0, rows.length - 1]);
    for (let i = 0; i < limit; i++) indexes.add(Math.round(i * (rows.length - 1) / (limit - 1)));
    for (let i = 1; i < rows.length; i++) if (rows[i][3] !== rows[i - 1][3]) { indexes.add(i - 1); indexes.add(i); }
    return [...indexes].sort((a, b) => a - b).map((i) => rows[i]);
  }
  function renderDayOverviewCharts() {
    if (!state.overview || state.page !== "overview") return;
    const data = state.overview, cases = arr(data.cases), [a, b] = overviewView(), [dayStart, dayEnd] = overviewBounds(), facets = arr(data.reference_facets).slice().sort((p, q) => `${p.direction_label}|${p.reference_id}`.localeCompare(`${q.direction_label}|${q.reference_id}`));
    const endLabel = b === dayEnd ? "24:00" : time(b); txt("overview-window-label", `${time(a)} – ${endLabel}`);
    $("overview-pan").disabled = b - a >= 86400; $("overview-pan").value = 86400 > b - a ? Math.round((a - dayStart) / (86400 - (b - a)) * 1000) : 0;
    $("overview-previous").disabled = a <= dayStart; $("overview-next").disabled = b >= dayEnd;
    const container = $("day-overview-facets"), sections = document.createDocumentFragment();
    for (const [facetIndex, facet] of facets.entries()) {
      const referenceId = rid(facet.reference_id), relevant = cases.filter((c) => arr(c.fragments).some((f) => rid(f.reference_id) === referenceId) || arr(c.apc_intervals).some((p) => rid(p.reference_id) === referenceId) || rid(c.candidate?.reference_id) === referenceId);
      const section = node("section", "panel overview-facet"); section.dataset.direction = facet.direction_label || "UNKNOWN"; section.dataset.referenceId = referenceId;
      const header = node("div", "panel-header"), heading = node("div"); heading.append(node("h3", "", `${facet.direction_label || "UNKNOWN"} · Shape ${facet.shape_id || "—"}`), node("span", "panel-subtitle mono", `独立 canonical s 轴 · ${referenceId} · ${fmt(relevant.length)} 个片段`)); header.append(heading); section.append(header);
      const wrap = node("div", "chart-wrap"), root = svg("svg", { role: "img", "aria-label": `${facet.direction_label} 方向 ${referenceId} 全天时间距离图`, class: "overview-chart", "data-reference-id": referenceId, "data-direction": facet.direction_label, "data-view-start": a, "data-view-end": b }); wrap.append(root); section.append(wrap); sections.append(section);
      // The section is still detached: derive width from the visible page, not a
      // hidden/detached SVG parent (which would report zero).
      const width = Math.max(280, $("day-overview-page").clientWidth - 2), d = { width, height: 355, left: width < 500 ? 44 : 64, right: 23, top: 22, bottom: 30 };
      root.setAttribute("viewBox", `0 0 ${width} ${d.height}`); root.style.height = `${d.height}px`;
      const g = document.createDocumentFragment(), x = axes(g, d, a, b), observedMax = relevant.reduce((max, c) => Math.max(max, ...arr(c.fragments).filter((f) => rid(f.reference_id) === referenceId).flatMap((f) => arr(f.points).map((p) => finite(p[1]) ? p[1] : 0))), 0), maxS = Math.max(1, finite(facet.length_m) ? facet.length_m : observedMax), y = (s) => d.height - d.bottom - s / maxS * (d.height - d.top - d.bottom);
      for (let i = 0; i <= 4; i++) { const s = maxS * i / 4; g.append(svg("line", { x1: d.left, x2: width - d.right, y1: y(s), y2: y(s), class: "chart-grid" }), svg("text", { x: d.left - 8, y: y(s) + 3, "text-anchor": "end", class: "chart-axis" }, fmt(s / 1000))); }
      g.append(svg("text", { x: d.left - 8, y: 12, class: "chart-label", "text-anchor": "end" }, "s · km"));
      if (b === dayEnd) { const ticks = width < 500 ? 4 : 7; const labels = [...g.querySelectorAll?.("text.chart-axis") || []]; if (labels[ticks - 1]) labels[ticks - 1].textContent = "24:00"; }
      const marks = gtfsClip(g, d, `overview-clip-${facetIndex}`);
      for (const c of relevant) {
        const group = svg("g", { class: "overview-case", "data-case-id": c.id, "data-vehicle": c.vehicle, "data-reference-id": referenceId, tabindex: "0", role: "button", "aria-label": `${c.vehicle_label || c.vehicle} ${c.direction_label} ${range(c.start, c.end)} ${c.status}，打开逐趟检查` });
        const candidate = c.candidate?.reference_id === referenceId ? c.candidate : null, label = `${c.vehicle_label || c.vehicle} · ${c.direction_label} · ${range(c.start, c.end)}\n${c.id}\n${c.status} · ${candidate ? `${candidate.source === "fallback" ? "未过门槛（仅诊断）" : "通过门槛候选（非确认）"} ${candidate.trip_id}` : "此参考轴无展示候选"}`;
        group.append(svg("title", {}, label));
        if (candidate) {
          let path = "", previous = null;
          for (const p of arr(candidate.trajectory)) { const t = timestamp(p.t), s = p.s ?? p.s_m; if (!finite(t) || !finite(s)) { previous = null; continue; } path += `${previous && t >= previous ? "L" : "M"}${x(t).toFixed(2)},${y(s).toFixed(2)} `; previous = t; }
          const mark = svg("path", { d: path, class: "overview-scheduled", fill: "none", stroke: candidate.source === "fallback" ? "#b47732" : "#5276b6", "stroke-width": 1.35, "stroke-dasharray": candidate.source === "fallback" ? "2 4" : "6 4", opacity: .64, "data-trip-id": candidate.trip_id, "data-candidate-source": candidate.source, "data-reference-id": referenceId });
          mark.append(svg("title", {}, `${label}\n${arr(candidate.failed_gate_checks).map((gate) => `${gate.label || gate.metric}: ${fmt(gate.actual)} ${gate.operator || ""} ${fmt(gate.threshold)}`).join("\n")}\nGTFS 原计划时刻，不移动延误`)); group.append(mark);
        }
        for (const f of arr(c.fragments).filter((fragment) => rid(fragment.reference_id) === referenceId)) {
          const points = overviewSample(f.points); let path = "", selectedPath = "", previous = null;
          for (const p of points) { const t = timestamp(p[0]), s = p[1]; path += `${previous && t >= timestamp(previous[0]) ? "L" : "M"}${x(t).toFixed(2)},${y(s).toFixed(2)} `; if (previous && p[3] !== false && previous[3] !== false && t >= timestamp(previous[0])) selectedPath += `M${x(timestamp(previous[0])).toFixed(2)},${y(previous[1]).toFixed(2)} L${x(t).toFixed(2)},${y(s).toFixed(2)} `; previous = p; }
          const attrs = { "data-fragment-id": f.fragment_id, "data-reference-id": referenceId, "data-point-count": arr(f.points).length, "data-rendered-point-count": points.length, "data-context-only": String(f.context_only === true) };
          group.append(svg("path", { d: path, class: "overview-actual", fill: "none", stroke: COLORS.avl, "stroke-width": 1.6, opacity: state.matchingMode === "global" && !f.context_only ? .78 : .16, ...attrs }));
          if (state.matchingMode !== "global" && selectedPath && !f.context_only) group.append(svg("path", { d: selectedPath, class: "overview-selected", fill: "none", stroke: COLORS.avl, "stroke-width": 2, opacity: .9, "data-selected-for-decision": "true", ...attrs }));
          if (points.length === 1) group.append(svg("circle", { cx: x(timestamp(points[0][0])), cy: y(points[0][1]), r: 2.5, fill: COLORS.avl, opacity: points[0][3] === false || f.context_only ? .2 : .8, ...attrs }));
        }
        for (const selected of [false, true]) {
          if (!selected && state.matchingMode === "global") continue;
          const intervals = arr(c.apc_intervals).filter((p) => rid(p.reference_id) === referenceId); let path = "", uncertainPath = "", count = 0;
          for (const p of intervals) {
            // Draw the complete original interval faintly, then only the backend
            // intersection with this decision third in the evidence color.
            const spans = selected ? arr(p.selected_intervals) : [{ start: p.start, end: p.end }];
            for (const span of spans) {
              const start = timestamp(span.start), end = timestamp(span.end); if (!finite(start) || !finite(end) || end < start) continue;
              const left = x(start).toFixed(2), right = Math.max(x(start) + .6, x(end)).toFixed(2);
              if (finite(p.s)) path += `M${left},${y(p.s).toFixed(2)} L${right},${y(p.s).toFixed(2)} `;
              else if (finite(p.s_min_m) && finite(p.s_max_m)) { const top = y(p.s_max_m), bottom = Math.max(top + 1, y(p.s_min_m)); uncertainPath += `M${left},${top.toFixed(2)} L${right},${top.toFixed(2)} L${right},${bottom.toFixed(2)} L${left},${bottom.toFixed(2)} Z `; }
              count++;
            }
          }
          const attrs = { "data-selected-for-decision": String(selected), "data-reference-id": referenceId, "data-interval-count": count, "data-selection-clipped": String(selected && state.matchingMode !== "global") };
          if (path) group.append(svg("path", { d: path, fill: "none", stroke: COLORS.apc, "stroke-width": 2.5, opacity: selected ? .7 : .16, "data-kind": "apc-stop-interval", ...attrs }));
          if (uncertainPath) { const mark = svg("path", { d: uncertainPath, fill: COLORS.apc, stroke: COLORS.apc, "stroke-width": .5, "stroke-dasharray": "2 2", opacity: selected ? .22 : .07, "data-kind": "apc-stop-uncertainty-band", ...attrs }); mark.append(svg("title", {}, "APC canonical 站点位置存在歧义；色带保留 s 范围，不指定唯一站点进度。")); group.append(mark); }
        }
        const highlight = (enabled) => { root.classList.toggle("is-highlighting", enabled); group.classList.toggle("is-highlighted", enabled); };
        group.addEventListener("pointerenter", () => highlight(true)); group.addEventListener("pointerleave", () => highlight(false)); group.addEventListener("focus", () => highlight(true)); group.addEventListener("blur", () => highlight(false));
        group.addEventListener("click", () => openOverviewCase(c)); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); openOverviewCase(c); } }); marks.append(group);
      }
      if (!relevant.length) g.append(svg("text", { x: width / 2, y: 170, class: "chart-empty" }, "此方向／参考轴没有可绘制证据"));
      root.replaceChildren(g);
    }
    if (!facets.length) sections.append(node("p", "empty-panel", "本运行没有可用 canonical reference；下方仍列出全部片段，不补造时空轨迹。"));
    container.replaceChildren(sections);
  }
  function confirmLeave() { if (state.saving) { toast("审查正在保存，请保存完成后再切换。"); return false; } return !state.dirty || window.confirm(tr("当前审查笔记尚未保存。确定放弃未保存修改并切换吗？")); }
  function options(select, values, empty) { const previous = select.value; select.replaceChildren(); if (empty) select.append(new Option(empty, "")); for (const [value, label] of values) select.append(new Option(label, value)); if ([...select.options].some((o) => o.value === previous)) select.value = previous; }
  function modeText(element, streamText, stream) { if (!element) return; if (element.dataset.matchingText == null) element.dataset.matchingText = window.TidesI18n?.sourceText(element) ?? element.textContent; element.textContent = stream ? streamText : element.dataset.matchingText; }
  function renderRepresentation() {
    const stream = isStream(), dedup = isDedup(); document.body.classList.toggle("vehicle-stream-mode", stream);
    document.title = stream ? "TIDES · 按车辆查看 APC–AVL 原始观测" : "TIDES · APC–AVL 逐趟审查";
    document.querySelector(".sidebar").setAttribute("aria-label", stream ? "车辆日观测筛选" : "筛选与候选行程");
    modeText(document.querySelector(".sidebar-heading h1"), "逐车查看", stream);
    modeText($("method-select-label"), "观测组织方式", stream); modeText($("debug-mode-title"), "单日原始观测汇集", stream);
    modeText(document.querySelector(".sidebar-heading .eyebrow"), "VEHICLE-DAY STREAM", stream);
    modeText(document.querySelector(".sidebar-footer>span"), "J / K 切换浏览对象", stream);
    modeText($("evidence-notice-copy"), `这里只把同一 VehicleID 的 APC 与 AVL 观测放在一起，并按来源时间稳定排序。${dedup ? "仅将 AVL 原 CSV 13 个字段完全相同的记录合为一条，保留最早来源行及完整 source rid 追溯；APC 全部保留。" : "源记录不去重。"}未做 trip 分割、时空匹配或客流关联；同车不等于同一 trip 或同一停站。时间未校正，日期边缘不是行程边界。`, stream);
    modeText(document.querySelector(".review-panel h3"), "审查这辆车当天的原始观测", stream);
    modeText(document.querySelector(".review-panel .panel-subtitle"), "检查来源记录，不评价尚未执行的匹配算法", stream);
    modeText($("save-next"), "保存并下一对象 →", stream);
    modeText(document.querySelector(".progress-panel .chart-caption"), "仅 AVL 实测累计 · APC 不投影", stream);
    modeText(document.querySelector(".progress-panel .panel-subtitle"), "仅累计连续有效 AVL 观测的实测距离；绘图缺口不连接，不表示 trip 分割或行程完成率", stream);
    modeText(document.querySelector(".timeline-panel .panel-subtitle"), "同车 AVL 实测时刻与 APC 到离站区间 · 不建立证据连线", stream);
    modeText(document.querySelector(".inspector-panel h3"), "原始观测详情", stream);
    modeText(document.querySelector(".inspector-panel .eyebrow"), "SOURCE RECORD INSPECTOR", stream);
    modeText(document.querySelector(".window-toolbar>small"), `只切换图表浏览时间窗，不拆分来源记录。统一观测表默认展示全日${dedup ? "保留记录，合并副本可追溯" : "所有来源行"}，可自行筛选；没有 trip 起终点或匹配状态。`, stream);
    modeText(document.querySelector(".metric:nth-child(2)>small"), "当日全部 APC 来源行 · 不加总客流", stream);
    modeText(document.querySelector(".main-footer>span:first-child"), "TIDES · 同车原始观测查看", stream);
    modeText(document.querySelector(".top-meta .version"), dedup ? "VEHICLE-ID · EXACT AVL DEDUP" : "VEHICLE-ID UNION ALL", stream);
    modeText(document.querySelector("#observation-stream-panel .panel-subtitle"), dedup ? "AVL 原 CSV 13 字段完全重复合并；APC 全部保留。表内序号沿用服务端保留记录的稳定时间顺序，不执行匹配或 trip 分割。" : "UNION ALL · 不匹配、不去重、不拆 trip；表内序号沿用服务端完整记录顺序。", stream);
    $("observation-stream-panel").setAttribute("aria-label", dedup ? "同车 APC 与去重保留 AVL 统一时间排序记录" : "同车全部 APC 与 AVL 统一时间排序记录");
    $("dedup-summary").hidden = !dedup;
    $("case-search").placeholder = stream ? "搜索车辆、线路、日期…" : "搜索片段 ID、线路、日期…";
    $("case-list").setAttribute("aria-label", stream ? "车辆日观测对象列表" : "候选行程列表"); document.querySelector(".metrics").setAttribute("aria-label", stream ? "当前车辆日来源记录统计" : "当前候选片段统计"); $("method-select").setAttribute("aria-label", stream ? "观测组织方式" : "分割方法");
    $("map-svg").setAttribute("aria-label", stream ? "同车 AVL 实测位置与 APC 原始位置，无匹配连线" : "AVL 地理轨迹与 APC 关联记录"); $("progress-svg").setAttribute("aria-label", stream ? "仅 AVL 实测累计距离；APC 进度未知" : "实际 GPS 累计距离和 APC 支持点");
    modeText($("no-case"), "没有符合筛选条件的车辆日观测对象。请调整左侧筛选。", stream);
    $("status-filter").closest("label").hidden = stream; if (stream) $("status-filter").value = "";
    $("metric-support").closest(".metric").hidden = stream; $("metric-attention").closest(".metric").hidden = stream;
    $("observation-stream-panel").hidden = !stream; $("apc-detail-panel").hidden = stream;
    $("context-size").hidden = stream; $("layer-context").closest("label").hidden = stream; $("layer-counters").closest("label").hidden = stream;
    for (const legend of document.querySelectorAll(".map-legend>span")) legend.hidden = stream;
    let legend = $("stream-map-legend"); if (!legend) { legend = node("span", "stream-map-legend", "绿色 AVL · 灰蓝 APC 原始位置 · 不代表匹配"); legend.id = "stream-map-legend"; document.querySelector(".map-legend").prepend(legend); } legend.hidden = !stream;
    for (const button of [$("previous-case"), $("next-case")]) { const previous = button.id === "previous-case"; button.setAttribute("aria-label", `${previous ? "上" : "下"}一个${stream ? "车辆日对象" : "行程"}`); button.title = `${previous ? "上" : "下"}一个${stream ? "浏览对象" : "行程"} · ${previous ? "K" : "J"}`; }
    modeText($("window-size").querySelector('[value="full"]'), "当日全窗", stream);
    $("direction-panel").hidden = !isDirection(); $("direction-filters").hidden = !isDirection(); $("observation-direction-heading").hidden = !isDirection(); $("observation-conflicts-label").hidden = !isDirection(); document.body.classList.toggle("direction-candidate-mode", isDirection());
    if (!isDirection()) { $("direction-filter").value = ""; $("direction-conflict-filter").value = ""; }
    if (isDirection()) {
      document.title = "TIDES · 实验 02 方向候选行程";
      for (const [selector, text] of [[".sidebar-heading h1", "方向候选行程"], [".sidebar-heading .eyebrow", "DIRECTION CANDIDATES"], ["#debug-mode-title", "方向 + Destination 分割实验"], [".review-panel h3", "审查这段方向候选"], [".review-panel .panel-subtitle", "候选分割与方向冲突，不是已确认 trip 或匹配准确率"], [".top-meta .version", "EXPERIMENT 02 · CANDIDATES"], ["#window-size option[value=full]", "候选加载全窗"], ["#observation-stream-panel .panel-subtitle", "候选成员与冲突上下文按源时间并列展示；APC 跨边界可属于多个候选，不代表唯一匹配。"], [".window-toolbar>small", "只移动浏览时间窗，不改变方向分割。表格默认显示本候选加载的成员和上下文；跨候选出现的 APC 不可重复累加客流。"], [".metric:nth-child(2)>small", "候选 APC 成员 · 跨候选可能重复展示"], [".main-footer>span:first-child", "TIDES · 未确认的方向候选行程"]]) modeText(document.querySelector(selector), text, true);
      txt("evidence-notice-copy", "实验 02：沿实验 01b 的原字段精确 AVL 去重结果，排除 AVL Route 为 PI / PO 的记录，保留全部 APC。使用 APC RouteDirection 与 AVL Destination 方向时序构造候选行程；不是已确认 trip，不执行 APC–AVL 时空匹配或 APC 距离投影。方向冲突与跨边界 APC 保留，Heading 不当作运营方向。日期边缘仍是数据截断。");
      txt("stream-map-legend", "绿色 AVL · 灰蓝 APC · 橙红为方向冲突（不是匹配状态）");
      $("context-size").hidden = false; $("layer-context").closest("label").hidden = false;
      $("case-list").setAttribute("aria-label", "方向候选行程列表"); $("observation-stream-panel").setAttribute("aria-label", "方向候选成员与上下文统一观测表"); $("case-search").placeholder = "搜索候选 ID、方向、线路…";
      document.querySelector(".sidebar").setAttribute("aria-label", "方向候选与冲突筛选"); txt("no-case", "没有符合条件的方向候选或未定位审查窗口。请调整筛选。");
      for (const button of [$("previous-case"), $("next-case")]) { const previous = button.id === "previous-case"; button.setAttribute("aria-label", `${previous ? "上" : "下"}一个方向候选审查对象`); button.title = `${previous ? "上" : "下"}一个方向候选 · ${previous ? "K" : "J"}`; }
    } else if (stream) txt("stream-map-legend", "绿色 AVL · 灰蓝 APC 原始位置 · 不代表匹配");
    for (const id of ["gtfs-candidate-panel", "gtfs-schedule-panel", "gtfs-progress-panel"]) $(id).hidden = !isGTFS();
    document.body.classList.toggle("gtfs-mode", isGTFS());
    if (isGTFS()) {
      document.title = "TIDES · Route 29 GTFS 候选审查";
      $("case-search").placeholder = "搜索候选、方向、GTFS 状态（AMBIGUOUS…）";
      for (const [selector, text] of [[".sidebar-heading h1", "GTFS 候选审查"], [".sidebar-heading .eyebrow", "SCHEDULE EVIDENCE"], ["#debug-mode-title", "GTFS 计划候选实验"], [".top-meta .version", "GTFS · ROUTE 29 · NOV 06"], [".review-panel .panel-subtitle", "审查计划候选及证据；算法状态不是经独立验证的准确率"], [".main-footer>span:first-child", "TIDES · GTFS 候选与原始证据"]]) modeText(document.querySelector(selector), text, true);
      txt("evidence-notice-copy", `2023-11-06 · Route 29：${state.meta.gap_segmentation_used ? "先在实验 02 方向候选内按 APC + AVL 联合观测空档严格超过阈值处分段，再对每个子片段独立比较 GTFS 计划。" : "在实验 02 方向候选基础上比较 GTFS 计划，"}保留源观测、方向冲突和边界不确定性。原累计距离图不投影 APC；新增 canonical s 图只使用后端有效 AVL 投影和 APC 站点证据，不用最近 GPS 伪造关联。OFF_REFERENCE 仅诊断；候选分数未校准为概率，MATCHED 不是独立验证结果。`);
    }
    if (!stream) { state.selectedObservation = null; $("observation-selection-note").hidden = true; }
    renderMatchingControls();
  }
  function updateMethodParameters() {
    const method = $("debug-method").value, stream = streamMethod(method), dedup = method === "vehicle_only_dedup"; $("vehicle-only-config-note").hidden = !stream;
    txt("vehicle-only-config-note", dedup ? "仅将 AVL 原 CSV 13 个字段完全相同的记录合并，按最小 source_row 保留代表，并保留组内 source rid 追溯。APC 全部保留，再按 VehicleID 汇集与时间排序；不执行 trip 分割、时空匹配或插值。" : "仅按 VehicleID 汇集当天 APC 和 AVL，保留每条来源记录并统一时间排序。不执行 trip 分割、时空匹配、插值或阈值判定；同车不代表同一 trip 或同一停站。");
    modeText(document.querySelector(".debug-submit>small"), dedup ? "没有待调分割或匹配阈值。只去除可精确追溯的 AVL 完全重复副本；不改变 APC，原文件与旧实验不变。" : "本方法没有待调分割或匹配阈值。固定当天同车来源行全部保留，只做可复现时间排序。", stream);
    for (const group of ["segmentation", "merge"]) { const container = $(`debug-${group}-parameters`); container.closest("details").hidden = stream; for (const input of container.querySelectorAll("input,select")) input.disabled = stream || !!state.runLoading; }
    $("debug-gtfs-settings").hidden = method !== "gtfs_trip_matching";
    for (const input of $("debug-gtfs-parameters").querySelectorAll("input,select")) input.disabled = method !== "gtfs_trip_matching" || !!state.runLoading;
    let continuousNote = $("debug-continuous-parameter-note"); if (!continuousNote) { continuousNote = node("p", "stream-notice", "旧 DTW 与每小时时移参数仅用于旧评分器；连续时空评分请使用 continuous_* 参数。旧值保留，不随界面提示改变。"); continuousNote.id = "debug-continuous-parameter-note"; $("debug-gtfs-settings").append(continuousNote); }
    const continuousEditor = $("debug-gtfs-continuous_matching_enabled")?.checked === true; continuousNote.hidden = method !== "gtfs_trip_matching" || !continuousEditor;
    const legacyWeightField = $("debug-gtfs-score_delay_prior_weight")?.closest("label"); if (legacyWeightField) legacyWeightField.dataset.inactiveForContinuous = String(continuousEditor);
    if (method === "direction_destination") { txt("vehicle-only-config-note", "实验 02 v1：先对 AVL 原 CSV 13 字段精确去重，再排除 AVL Route=PI/PO；APC 全部保留。以 APC RouteDirection / AVL Destination 的方向时序生成候选，保留冲突和非唯一候选归属。Heading 不用于方向分割，不执行 APC–AVL matcher。规则为固定版本常数，运行摘要保留完整设置。"); modeText(document.querySelector(".debug-submit>small"), "本版配置 segmentation={}、merge={}；旧分割／匹配阈值不适用。候选数量和方向冲突数量都不是准确率。", true); }
    if (method === "gtfs_trip_matching") { txt("vehicle-only-config-note", "仅 2023-11-06、Route 29：沿实验 02 方向候选比较 GTFS 计划，保留最多三个最终候选及不确定性。旧 APC–AVL matcher 参数不适用；有效投影、站点证据、OFF_REFERENCE 和完整配置可在结果中追溯。"); modeText(document.querySelector(".debug-submit>small"), "计划候选结果不代表已确认 trip；没有真值时，准确率和校准概率保持未知。图表窗口长度是浏览设置，不改变实验算法。", true); }
  }
  const debugRunId = (run) => rid(run.id || run.run_id);
  const currentDebugRun = () => arr(state.debug?.runs).find((r) => debugRunId(r) === state.run);
  function captureAnchor() { const row = selectedAPC(); return { rid: state.selected, observationSource: state.selectedObservation?.source, observationRid: state.selectedObservation?.rid, at: timestamp(row?.arrival) ?? state.cursor ?? timestamp(state.detail?.case?.start), cursor: state.cursor, view: state.view ? [...state.view] : null, full: !state.view, vehicle: state.vehicle }; }
  function anchorCase(anchor) {
    if (!anchor || anchor.vehicle !== state.vehicle) return null;
    const exact = anchor.rid && state.cases.filter((c) => arr(c.apc_rids || c.focus_apc_rids).map(rid).includes(rid(anchor.rid)));
    if (exact?.length) return exact.find((c) => c.kind === "segment") || exact[0];
    if (!finite(anchor.at)) return null;
    return state.cases.filter((c) => timestamp(c.start) <= anchor.at && timestamp(c.end) >= anchor.at).sort((a, b) => (timestamp(a.end) - timestamp(a.start)) - (timestamp(b.end) - timestamp(b.start)))[0] || null;
  }
  function restoreAnchor(anchor) {
    const row = arr(state.detail?.apc).find((r) => rid(r.rid) === rid(anchor.rid));
    if (row) state.selected = rid(row.rid); else if (anchor.rid) toast(isStream() ? "当前车辆日未包含原先 APC，已保留时间位置；没有推断匹配。" : "此片段未包含原先 APC。已按原始时间定位；未按 segment ID 假定同一趟。");
    const [start, end] = sourceBounds();
    if (anchor.view && anchor.view[1] > start && anchor.view[0] < end) { state.view = [Math.max(start, anchor.view[0]), Math.min(end, anchor.view[1])]; if (state.view[1] <= state.view[0]) state.view = null; if (anchor.view[0] < start || anchor.view[1] > end) toast("已保留原 APC / 时间位置；原浏览窗口超出新片段加载范围的部分已裁剪，不表示实际 trip 起终点改变。"); }
    else if (anchor.full) state.view = null;
    if (finite(anchor.cursor)) state.cursor = Math.max(start, Math.min(end, anchor.cursor));
    else if (row) state.cursor = timestamp(row.arrival);
    syncWindowSize();
  }
  function renderDebug(resetParameters = true) {
    if (!state.debug?.enabled) return;
    $("debug-console").hidden = false; $("debug-diff").hidden = false; txt("debug-date", state.debug.date || "2023-11-06");
    const runs = arr(state.debug.runs), opts = runs.map((r) => [debugRunId(r), `${r.label || "未命名实验"} · ${debugRunId(r)}`]);
    options($("debug-run-select"), opts); options($("debug-base-select"), opts);
    if (!runs.some((r) => debugRunId(r) === state.base)) state.base = runs[0] ? debugRunId(runs[0]) : state.run;
    $("debug-run-select").value = state.run; $("debug-base-select").value = state.base;
    $("export-reviews").href = runPath("/api/reviews/export");
    const current = currentDebugRun(); txt("debug-provenance", json({ date: state.debug.date, fixture: state.debug.fixture, run: current }));
    if (resetParameters) {
      const config = current?.config || state.debug.defaults || {};
      options($("debug-method"), arr(state.debug.methods).length ? state.debug.methods.map((m) => [m, METHOD[m] || m]) : ["gap_only", "avl_context", "apc_assisted"].map((m) => [m, METHOD[m] || m]));
      $("debug-method").value = config.method || state.debug.defaults?.method || "avl_context";
      for (const group of ["segmentation", "merge", "gtfs"]) {
        const root = $(`debug-${group}-parameters`); root.replaceChildren();
        for (const parameter of arr(group === "gtfs" ? state.debug.gtfs_parameters || state.debug.parameters?.gtfs : state.debug.parameters?.[group])) {
          const label = node("label", "parameter-field"), id = `debug-${group}-${parameter.key}`, value = config[group]?.[parameter.key] ?? (group === "gtfs" ? state.debug.gtfs_defaults?.[parameter.key] : undefined) ?? parameter.default;
          label.append(node("span", "", PARAMETER_LABEL[parameter.key] || parameter.label || parameter.key), node("small", "mono", parameter.key));
          const input = node(parameter.type === "enum" ? "select" : "input"); input.id = id; input.dataset.group = group; input.dataset.key = parameter.key; input.dataset.type = parameter.type; input.setAttribute("aria-label", `${group} ${parameter.label || parameter.key}`);
          if (parameter.type === "enum") for (const choice of arr(parameter.choices)) input.append(new Option({ flag: "仅标记不一致", abstain_conflict: "标签冲突则保留歧义" }[choice] || String(choice), String(choice)));
          else if (["boolean", "bool"].includes(parameter.type)) { input.type = "checkbox"; input.checked = value === true; }
          else { input.type = "number"; input.required = true; input.step = parameter.type === "integer" ? "1" : "any"; if (parameter.min != null) input.min = parameter.min; if (parameter.max != null) input.max = parameter.max; }
          input.value = value ?? ""; if (group === "gtfs" && parameter.key === "continuous_matching_enabled") input.addEventListener("change", updateMethodParameters); label.append(input); if (parameter.description) label.append(node("small", "", parameter.description)); root.append(label);
        }
      }
    }
    updateMethodParameters();
  }
  async function refreshDebug(resetParameters = false) { state.debug = await api("/api/debug"); renderDebug(resetParameters); }
  function setRunLoading(loading) { state.runLoading = loading; $("debug-run-loading").hidden = !loading; $("debug-form").inert = loading; $("debug-form").setAttribute("aria-busy", String(loading)); for (const id of ["debug-method", "debug-label", "debug-reset"]) $(id).disabled = loading; $("debug-submit").disabled = loading || !!state.job; updateMethodParameters(); renderMatchingControls(); }
  async function switchRun(run, skipConfirm = false) {
    if (!run || (!skipConfirm && !confirmLeave())) { $("debug-run-select").value = state.run; return; }
    const anchor = captureAnchor(), seq = ++state.runSequence, draft = { notes: $("review-notes").value, verdict: document.querySelector("#review-verdict input:checked")?.value, dirty: state.dirty };
    setRunLoading(true);
    try {
      const meta = await api(runPath("/api/meta", run)); if (seq !== state.runSequence) return;
      if (state.saving || $("review-notes").value !== draft.notes || document.querySelector("#review-verdict input:checked")?.value !== draft.verdict || (!draft.dirty && state.dirty)) { state.pendingRun = run; $("debug-load-ready").hidden = false; $("debug-job").hidden = false; $("debug-run-select").value = state.run; txt("debug-job-message", "载入期间有新的笔记编辑，未切换运行。请先保存，再点击加载。"); return; }
      // Notes belong to the exact run, even when source IDs survive a rerun.
      state.run = run; state.meta = meta; state.comparison = null; state.pendingRun = null; state.overview = null; state.detail = null; ++state.overviewSequence; ++state.modeSequence; state.modeLoading = false; if (!arr(meta.available_matching_modes).includes(state.matchingMode)) state.matchingMode = "global"; $("debug-load-ready").hidden = true;
      options($("method-select"), arr(meta.methods).map((m) => [m, METHOD[m] || m])); state.method = meta.default_method || $("method-select").value; $("method-select").value = state.method;
      options($("vehicle-select"), arr(meta.vehicles).map((v) => typeof v === "object" ? [rid(v.id), `车辆 ${v.label || v.id}`] : [rid(v), `车辆 ${v}`]));
      state.vehicle = [...$("vehicle-select").options].some((o) => o.value === anchor.vehicle) ? anchor.vehicle : rid(meta.default_vehicle || $("vehicle-select").value); $("vehicle-select").value = state.vehicle;
      txt("run-label", `${run} · 单日独立运行 · 源数据只读`); renderDebug(); await loadCatalog(anchor); if (state.page === "overview") await loadDayOverview();
    } catch (e) { error(e.message); $("debug-run-select").value = state.run; }
    finally { if (seq === state.runSequence) setRunLoading(false); }
  }
  function readDebugConfig() {
    const config = { method: $("debug-method").value, segmentation: {}, merge: {} };
    if (config.method === "gtfs_trip_matching") config.gtfs = {};
    else if (streamMethod(config.method)) return config;
    for (const input of $("debug-form").querySelectorAll("[data-group][data-key]")) {
      if (input.disabled) continue;
      if (["boolean", "bool"].includes(input.dataset.type)) { config[input.dataset.group][input.dataset.key] = input.checked; continue; }
      const value = input.dataset.type === "enum" ? input.value : Number(input.value);
      if (input.dataset.type !== "enum" && (!input.value.trim() || !Number.isFinite(value) || (input.dataset.type === "integer" && !Number.isInteger(value)))) throw new Error(`参数 ${input.dataset.key} 必须为有效${input.dataset.type === "integer" ? "整数" : "数值"}`);
      config[input.dataset.group][input.dataset.key] = value;
    }
    return config;
  }
  async function startDebugRun(event) {
    event.preventDefault(); if (state.runLoading) { toast("请等待所选运行载入完成，再编辑并提交实验。"); return; } if (!state.debug?.enabled || state.job || !$("debug-form").reportValidity()) return;
    try {
      const config = readDebugConfig(); $("debug-submit").disabled = true; state.job = { state: "queued", message: "正在提交单日实验参数…" }; renderJob(state.job); error("");
      const result = await api("/api/debug/runs", { method: "POST", headers: { "Content-Type": "application/json", "X-Review-Token": state.meta.csrf_token }, body: JSON.stringify({ config, label: $("debug-label").value.trim(), parent_run_id: state.run }) });
      state.job = result.job; if (!state.job?.id) throw new Error("服务器未返回实验任务 ID"); state.pendingRun = null; $("debug-load-ready").hidden = true; renderJob(state.job); pollDebugJob(state.job.id);
    } catch (e) { state.job = null; $("debug-submit").disabled = false; renderJob({ state: "failed", error: e.message }); error(e.message); }
  }
  function renderJob(job) {
    $("debug-job").hidden = false; txt("debug-job-title", { queued: "排队中", running: "单日重跑中", complete: "重跑完成", failed: "重跑失败" }[job.state] || job.state || "已提交");
    const details = typeof job.progress === "object" ? job.progress : job.details;
    const percent = typeof job.progress === "number" ? job.progress : details?.total ? 100 * details.completed / details.total : job.state === "complete" ? 100 : 0;
    $("debug-job-progress").value = Math.max(0, Math.min(100, percent));
    txt("debug-job-message", job.error ? (typeof job.error === "string" ? job.error : json(job.error)) : job.message || `${details?.stage || ""}${details?.total ? ` · ${fmt(details.completed)} / ${fmt(details.total)}` : ""}`);
  }
  async function pollDebugJob(id) {
    try {
      const { job } = await api(`/api/debug/jobs?id=${encodeURIComponent(id)}`); if (state.job?.id !== id) return; renderJob(job);
      if (["complete", "failed"].includes(job.state)) {
        state.job = null; $("debug-submit").disabled = false;
        if (job.state === "failed") return;
        await refreshDebug(false);
        if (job.run_id) {
          state.pendingRun = job.run_id;
          if (state.dirty || state.saving) { $("debug-load-ready").hidden = false; txt("debug-job-message", "结果已保存。当前笔记未保存或保存中，未自动切换；请先保存笔记，再加载新运行。"); }
          else await switchRun(job.run_id, true);
        }
      } else setTimeout(() => pollDebugJob(id), 1000);
    } catch (e) { if ([400, 404].includes(e.status)) { state.job = null; $("debug-submit").disabled = false; renderJob({ state: "failed", error: `任务状态不可恢复：${e.message}。请刷新运行列表查看已完成结果；不要据此认为原任务未执行。` }); return; } txt("debug-job-message", `进度读取暂时失败：${e.message}；将重试，未重复提交实验。`); if (state.job?.id === id) setTimeout(() => pollDebugJob(id), 3000); }
  }
  async function loadComparison() {
    if (!state.debug?.enabled || !state.base || !state.run || !state.vehicle) return;
    const seq = ++state.compareSequence; state.comparison = null; txt("debug-diff-status", "正在按原始记录比较…"); $("debug-diff-list").replaceChildren(); $("debug-diff-summary").replaceChildren(); $("debug-boundary-svg").replaceChildren();
    try {
      const comparison = await api(`/api/debug/compare?base=${encodeURIComponent(state.base)}&run=${encodeURIComponent(state.run)}&vehicle=${encodeURIComponent(state.vehicle)}`); if (seq !== state.compareSequence) return;
      state.comparison = comparison; renderComparison();
    } catch (e) { if (seq === state.compareSequence) txt("debug-diff-status", `对照不可用：${e.message}`); }
  }
  function clearComparison() { if (!state.debug?.enabled) return; ++state.compareSequence; state.comparison = null; txt("debug-diff-status", "正在切换车辆或运行…"); for (const id of ["debug-diff-list", "debug-diff-summary", "debug-boundary-svg", "debug-diff-json"]) $(id).replaceChildren(); }
  function hideBoundaryComparison(hidden) { const chart = $("debug-boundary-svg"); chart.hidden = hidden; if (hidden) chart.setAttribute("hidden", ""); else chart.removeAttribute("hidden"); $("debug-diff-summary").hidden = hidden; }
  function renderComparison() {
    const diff = state.comparison; if (!diff) return; const summary = diff.summary || {};
    if (diff.comparison_status === "scope_and_diagnostics_only") {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), "只比较 Route 29 来源身份、保留数量与 GTFS 候选诊断。实验 02 未执行 GTFS 匹配，不能据此声称准确率改善。", true);
      txt("debug-diff-status", "Route 29 来源与 GTFS 诊断 · 非准确率对照"); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true); $("debug-diff-summary").hidden = false;
      const counts = diff.source_counts || {}, metrics = [["base_avl", "基线 Route 29 AVL", counts.base?.avl], ["current_avl", "当前 Route 29 AVL", counts.current?.avl], ["base_apc", "基线 Route 29 APC", counts.base?.apc], ["current_apc", "当前 Route 29 APC", counts.current?.apc], ["identity_changes", "来源身份变化", summary.violations?.route_29_identity_changed]];
      $("debug-diff-summary").replaceChildren(...metrics.map(([key, label, value]) => { const item = node("div", "diff-stat"); item.dataset.metric = key; item.append(node("strong", "", fmt(value)), node("span", "", label)); return item; }));
      $("debug-diff-list").replaceChildren(node("p", "stream-notice", "两版处理层级不同：来源身份和保留数量可核对，GTFS 状态只能作为当前实验诊断，不能计算所谓匹配提升或分割边界改善。")); txt("debug-diff-json", json(diff)); return;
    }
    if (diff.comparison_status === "direction_segmentation_comparison") {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), "实验 02 对照只检查 PI/PO 排除、APC 保留、来源追溯和方向候选数量，不称匹配改善。", true);
      txt("debug-diff-status", "方向候选分割对照 · 无匹配准确率"); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true); $("debug-diff-summary").hidden = false;
      const labels = { avl_removed_by_pi_po_filter: "PI/PO 排除 AVL", unexpected_removed: "未解释移除", unexpected_added: "未解释新增", original_fields_changed: "原字段变化", base_segments: "基线候选", current_segments: "当前候选", current_conflicts: "当前冲突窗口", current_conflict_observations: "涉及冲突记录" };
      $("debug-diff-summary").replaceChildren(...Object.entries(labels).map(([key, label]) => { const item = node("div", "diff-stat"); item.dataset.metric = key; item.append(node("strong", "", fmt(summary[key])), node("span", "", label)); return item; }));
      $("debug-diff-list").replaceChildren(node("p", "stream-notice", `APC ${summary.apc_unchanged === true ? "原记录核对不变" : "保留情况需检查"}；来源追溯${summary.source_lineage_conserved === true ? "完整" : "需检查"}。候选数量不等于真实 trip 数，冲突窗口不是独立冲突事件数。PI/PO 排除与去重均保留审计，完整源 ID 差异见下方。`)); txt("debug-diff-json", json(diff)); return;
    }
    if (diff.not_comparable || diff.comparable === false || diff.comparison_status === "not_comparable") {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), "原始观测汇集与分割 / 匹配是不同处理层级；不生成伪匹配改善或边界变化指标。", true);
      txt("debug-diff-status", "不同处理层级 · 不可比较匹配性能"); $("debug-diff-summary").replaceChildren(); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true);
      $("debug-diff-list").replaceChildren(node("p", "stream-notice", "两个运行执行的分割 / 匹配层级不同。不存在可直接比较的匹配提升或边界删除数量。"), node("p", "stream-order-note", typeof diff.reason === "string" ? diff.reason : json(diff.reason)));
      txt("debug-diff-json", json(diff)); return;
    }
    if (diff.comparison_status === "source_stream_comparison" || (isStream() && diff.representation === "vehicle_observation_stream")) {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), diff.avl_deduplication_comparison ? "对照 AVL 完全重复副本合并与来源追溯；APC 保留情况单独核对，不评价匹配准确率。" : "只检查完整 source rid 与稳定显示顺序；两个运行都没有 trip 分割或匹配判定。", true);
      txt("debug-diff-status", "仅比较来源记录及排序 · 未执行匹配"); $("debug-diff-summary").replaceChildren(); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true);
      const list = $("debug-diff-list"); list.replaceChildren(node("p", "stream-notice", "两个运行均为同车观测流。这里只核对 source rid、记录保留和显示顺序，不计算匹配状态、性能或 trip 边界变化。"));
      if (diff.avl_deduplication_comparison) {
        txt("debug-diff-status", "AVL 完全重复去重对照 · 非匹配性能"); $("debug-diff-summary").hidden = false;
        const labels = { dedup_explained_removals: "精确副本合并", dedup_explained_additions: "反向恢复副本", unexplained_removals: "未解释移除", unexplained_additions: "未解释新增", display_ordinals_renumbered: "显示序号重编" };
        $("debug-diff-summary").append(...Object.entries(labels).map(([key, label]) => { const item = node("div", "diff-stat"); item.dataset.metric = key; item.append(node("strong", "", fmt(summary[key])), node("span", "", label)); return item; }));
        const retained = diff.retained_counts || {}; list.append(node("p", "stream-order-note", `保留 AVL：${fmt(retained.base?.avl)} → ${fmt(retained.current?.avl)}；保留 APC：${fmt(retained.base?.apc)} → ${fmt(retained.current?.apc)}。APC ${summary.apc_unchanged === true ? "来源记录核对不变" : summary.apc_unchanged === false ? "存在变化，需检查" : "守恒待核对"}；AVL 来源追溯${summary.source_lineage_conserved === true ? "完整" : summary.source_lineage_conserved === false ? "不完整，需检查" : "待核对"}。`), node("p", "stream-order-note", "精确副本合并使显示行数减少，不等于来源证据丢失；须同时检查未解释变化及 source rid 追溯。删除副本后的 display_order 可重新编号，不表示真实时间或相对先后改变。"));
        if (diff.deduplication_note) list.append(node("p", "stream-order-note", typeof diff.deduplication_note === "string" ? diff.deduplication_note : json(diff.deduplication_note)));
      }
      txt("debug-diff-json", json(diff)); return;
    }
    hideBoundaryComparison(false);
    modeText(document.querySelector("#debug-diff .panel-subtitle"), "", false);
    txt("debug-diff-status", diff.source_compatible === false ? "源证据不兼容，不能解释为算法效果" : `${fmt(summary.apc_common)} 条共同 APC · ${diff.truncated ? "返回结果有截断" : "当前车辆"}`);
    const labels = { changed_rows: "变化 APC", status_changed: "状态变化", support_changed: "支持层级变化", owner_membership_changed: "片段成员归属变化", evidence_changed: "GPS 证据变化", decision_changed: "判定细节变化", boundaries_added: "新增边界", boundaries_removed: "移除边界", boundary_reasons_changed: "边界理由变化" };
    $("debug-diff-summary").replaceChildren(...Object.entries(labels).map(([key, label]) => { const item = node("div", "diff-stat"); item.append(node("strong", "", fmt(summary[key] ?? 0)), node("span", "", label)); return item; }));
    const list = $("debug-diff-list"); list.replaceChildren();
    for (const row of arr(diff.changes).slice(0, 200)) {
      const b = node("button", "diff-row"); b.type = "button"; b.dataset.apcRid = row.apc_rid;
      b.append(node("span", "mono", `${time(row.arrival, true)} · ${row.apc_rid}`), node("span", "", `${STATUS[row.base?.status] || row.base?.status || "无记录"} → ${STATUS[row.current?.status] || row.current?.status || "无记录"}`), node("small", "", arr(row.change_types).join(" · ")));
      b.addEventListener("click", () => openDiffRow(row)); list.append(b);
    }
    if (!arr(diff.changes).length) list.append(node("div", "empty-small", "此车辆没有 source-rid 级判定变化。边界变化仍单独显示。"));
    if (arr(diff.changes).length > 200) list.append(node("p", "diff-limit", `列表显示前 200 / ${fmt(diff.changes.length)} 条；完整差异见下方可追溯 JSON。`));
    txt("debug-diff-json", json(diff)); renderBoundaryDiff();
  }
  async function openDiffRow(row) {
    const existing = arr(state.detail?.apc).find((r) => rid(r.rid) === rid(row.apc_rid));
    if (existing) { selectAPC(row.apc_rid); return; }
    const anchor = { rid: row.apc_rid, at: timestamp(row.arrival), cursor: timestamp(row.arrival), view: finite(timestamp(row.arrival)) ? [timestamp(row.arrival) - 300, timestamp(row.arrival) + 300] : null, vehicle: state.vehicle };
    const target = anchorCase(anchor); if (!target) { toast("当前运行没有包含这条 APC 的审查窗口；其基线记录仍保留在差异 JSON 中。"); return; }
    if (!confirmLeave()) return;
    if (!state.filtered.includes(target)) { for (const id of ["route-filter", "status-filter", "review-filter", "case-search"]) $(id).value = ""; state.lastSearch = ""; filterCases(false); }
    await loadCase(target.id, anchor);
  }
  function renderBoundaryDiff() {
    const diff = state.comparison; if (!diff || diff.not_comparable || diff.comparable === false || diff.comparison_status === "source_stream_comparison" || isStream()) return;
    const root = $("debug-boundary-svg"), d = dimensions(root, 150), g = document.createDocumentFragment();
    let [a, b] = state.detail ? bounds() : [timestamp(`${state.debug.date}T00:00:00`), timestamp(`${state.debug.date}T00:00:00`) + 86400];
    if (!finite(a) || !finite(b) || b <= a) return; const x = axes(g, d, a, b);
    const lanes = [["removed", "基线移除", 35, COLORS.counter], ["added", "当前新增", 65, COLORS.avl], ["changed", "理由变化", 95, COLORS.apc]];
    for (const [key, label, y, color] of lanes) {
      g.append(svg("text", { x: d.left - 8, y: y + 3, class: "chart-label", "text-anchor": "end" }, label));
      g.append(svg("line", { x1: d.left, x2: d.width - d.right, y1: y, y2: y, stroke: "#e0e9e1", "stroke-width": 2 }));
      for (const boundary of arr(diff.boundaries?.[key])) { const t = timestamp(boundary.at); if (!finite(t) || t < a || t > b) continue; const mark = svg("line", { x1: x(t), x2: x(t), y1: y - 10, y2: y + 10, stroke: color, "stroke-width": 3, class: "svg-clickable", tabindex: 0 }); mark.append(svg("title", {}, `${label} ${time(t, true)} · ${boundary.before_rid || ""} → ${boundary.after_rid || ""}\n${key === "changed" ? `${arr(boundary.base_reasons).join(", ")} → ${arr(boundary.current_reasons).join(", ")}` : arr(boundary.reasons).join(", ")}`)); const focus = () => { if (!state.detail) return; state.cursor = t; renderCharts(); renderMap(); }; mark.addEventListener("click", focus); mark.addEventListener("keydown", (e) => { if (e.key === "Enter") focus(); }); g.append(mark); }
    }
    g.append(svg("text", { x: d.width - d.right, y: 13, class: "chart-axis", "text-anchor": "end" }, state.detail ? "跟随下方浏览时间窗 · 未变化边界不重复绘制" : "固定单日范围 · 未变化边界省略")); root.replaceChildren(g);
  }
  async function init() {
    bind();
    try {
      state.meta = await api("/api/meta");
      // Older frozen-only servers do not implement /api/debug. Their meta has
      // no debug marker, so opening them must not create a spurious HTTP error.
      if (state.meta.debug_enabled || state.meta.debug_date) { try { state.debug = await api("/api/debug"); } catch (e) { if (e.status !== 404) toast(`单日实验控制台不可用：${e.message}`); } }
      if (state.debug?.enabled) { state.run = state.meta.run_id || state.debug.runs?.[0]?.id || state.debug.runs?.[0]?.run_id || ""; state.base = currentDebugRun()?.parent_run_id || debugRunId(arr(state.debug.runs).at(-1) || {}) || state.run; renderDebug(); }
      options($("method-select"), arr(state.meta.methods).map((m) => [m, METHOD[m] || m]));
      options($("vehicle-select"), arr(state.meta.vehicles).map((v) => typeof v === "object" ? [rid(v.id), `车辆 ${v.label || v.id}`] : [rid(v), `车辆 ${v}`]));
      state.method = state.meta.default_method || $("method-select").value;
      state.vehicle = rid(state.meta.default_vehicle || $("vehicle-select").value);
      $("method-select").value = state.method; $("vehicle-select").value = state.vehicle;
      txt("run-label", `${state.meta.run_id || "LOCAL RUN"} · 源数据只读`);
      if (!state.vehicle || !state.method) throw new Error("没有可用的车辆或融合结果。请检查服务的数据目录。");
      await loadCatalog();
    } catch (e) { error(e.message); $("case-list").replaceChildren(node("div", "empty-small", "数据未能载入。请检查服务器与输入运行目录后刷新。")); }
  }
  async function loadCatalog(anchor = null) {
    const seq = ++state.sequence; clearTimeout(state.searchTimer); state.catalogLoading = true; state.cases = []; state.filtered = []; state.reviews = {}; error(""); clearComparison(); state.detail = null; state.caseId = ""; state.selected = null; state.dirty = false; renderQueue();
    renderRepresentation();
    $("case-workspace").hidden = true; $("case-loading").hidden = false; $("no-case").hidden = true;
    $("case-list").replaceChildren(node("div", "empty-small", isStream() ? "正在读取同车完整观测…" : "正在读取候选片段…"));
    updateNavigation();
    try {
      const data = await api(`/api/catalog?method=${encodeURIComponent(state.method)}&vehicle=${encodeURIComponent(state.vehicle)}${matchingQuery()}`);
      if (seq !== state.sequence) return;
      state.catalogLoading = false; state.cases = arr(data.cases); state.reviews = data.reviews || {}; state.limit = 100;
      options($("direction-filter"), [...new Set(state.cases.map(directionLabel))].sort().map((v) => [v, v]), "全部方向");
      options($("date-filter"), [...new Set(state.cases.map((c) => c.date || date(c.start)))].sort().map((v) => [v, v]), "全部日期");
      if (state.debug?.enabled) { $("date-filter").value = state.debug.date; $("date-filter").disabled = true; }
      options($("route-filter"), [...new Set(state.cases.flatMap((c) => arr(c.routes).map(String)))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).map((v) => [v, v]), "全部线路");
      filterCases(false);
      let initial = anchor?.caseId && state.cases.find((c) => c.id === anchor.caseId) || anchor && anchorCase(anchor);
      if (initial && !state.filtered.includes(initial)) { for (const id of ["route-filter", "status-filter", "review-filter", "case-search", "direction-filter", "direction-conflict-filter"]) $(id).value = ""; state.lastSearch = ""; filterCases(false); }
      initial ||= state.filtered.find((c) => c.kind === "segment" && c.avl_count >= 50 && c.avl_count <= 1000 && c.apc_count >= 5 && c.apc_count <= 80) || state.filtered.find((c) => c.kind === "segment" && c.apc_count > 0) || state.filtered[0];
      if (initial) await loadCase(initial.id, anchor); else { $("no-case").hidden = false; $("case-loading").hidden = true; }
      if (state.debug?.enabled) loadComparison();
    } catch (e) { if (seq === state.sequence) { state.catalogLoading = false; error(e.message); $("case-loading").hidden = true; } }
  }
  function matches(c) {
    const df = $("date-filter").value, rf = $("route-filter").value, sf = $("status-filter").value, vf = $("review-filter").value;
    const query = $("case-search").value.toLowerCase().trim();
    const dir = $("direction-filter").value, conflict = $("direction-conflict-filter").value;
    return (!isDirection() || (!dir || directionLabel(c) === dir) && (!conflict || (conflict === "with" ? conflictCount(c) > 0 : conflictCount(c) === 0))) && (!df || (c.date || date(c.start)) === df) && (!rf || arr(c.routes).map(String).includes(rf)) && (!sf || (c.status_counts || {})[sf] > 0) && (!vf || (state.reviews[c.id]?.verdict || "unreviewed") === vf) && (!query || `${c.id} ${c.title || ""} ${c.date} ${arr(c.routes).join(" ")} ${c.vehicle_label || c.vehicle} ${directionLabel(c)} ${c.gtfs_status || ""}`.toLowerCase().includes(query));
  }
  function filterCases(allowSwitch = true) {
    state.filtered = state.cases.filter(matches); state.limit = 100; renderQueue(); updateNavigation();
    if (state.catalogLoading) return;
    if (allowSwitch && !state.filtered.some((c) => c.id === state.caseId)) {
      if (state.filtered.length) loadCase(state.filtered[0].id);
      else { ++state.sequence; state.caseId = ""; state.detail = null; state.selected = null; state.dirty = false; $("case-workspace").hidden = true; $("case-loading").hidden = true; $("no-case").hidden = false; txt("case-title", isStream() && !isDirection() ? "没有符合条件的车辆日观测" : "没有符合条件的候选片段"); txt("case-subtitle", "调整筛选条件以继续审查。"); for (const id of ["metric-avl", "metric-apc", "metric-support", "metric-attention"]) txt(id, "—"); }
    }
  }
  function renderQueue() {
    txt("case-count", fmt(state.filtered.length));
    const reviewed = state.filtered.filter((c) => state.reviews[c.id]?.verdict && state.reviews[c.id].verdict !== "unreviewed").length;
    const percent = state.filtered.length ? Math.round(reviewed / state.filtered.length * 100) : 0;
    txt("review-progress-text", `${fmt(reviewed)} / ${fmt(state.filtered.length)} 已审查`); txt("review-progress-percent", `${percent}%`); $("review-progress").value = percent;
    const frag = document.createDocumentFragment();
    if (!state.filtered.length) frag.append(node("div", "empty-small", isStream() && !isDirection() ? "没有符合条件的车辆日观测。" : "没有符合条件的候选片段。"));
    for (const c of state.filtered.slice(0, state.limit)) {
      const b = node("button", `case-item${c.id === state.caseId ? " active" : ""}`); b.type = "button"; b.dataset.caseId = c.id; if (c.gtfs_status) b.dataset.gtfsStatus = c.gtfs_status; b.setAttribute("role", "listitem"); b.setAttribute("aria-current", c.id === state.caseId ? "true" : "false");
      const title = node("div", "case-item-title"); title.append(node("span", "", c.kind === "vehicle_stream" ? `${c.date || date(c.start)} · 同车全日` : `${isDirection() ? directionLabel(c) + " · " : ""}${candidateRange(c)}`)); if (isDirection()) { b.dataset.direction = directionLabel(c); b.dataset.conflictCount = String(conflictCount(c)); }
      const review = state.reviews[c.id]?.verdict || "unreviewed"; title.append(node("span", `review-indicator ${review}`, { accepted: "✓", problem: "!", uncertain: "?", unreviewed: "" }[review])); b.append(title);
      const kind = c.kind === "direction_unresolved" ? "未定位方向审查桶（不是候选 trip）" : c.kind === "vehicle_stream" ? "同车全日观测" : c.kind === "apc_only" ? "未关联 APC 窗口" : c.kind === "unresolved" ? "未决归属窗口" : "候选片段";
      b.append(node("div", "case-item-meta", `${c.date || date(c.start)} · ${arr(c.routes).length ? "线路 " + c.routes.join(" / ") : kind}`));
      const gpsLabel = c.kind === "vehicle_stream" ? `${fmt(c.avl_count)} AVL 来源行` : c.kind === "segment" ? `${fmt(c.avl_member_count ?? c.avl_count)} 成员 GPS` : c.window_avl_count != null ? `${fmt(c.window_avl_count)} 同窗 GPS` : "GPS 见窗口详情";
      const counts = node("div", "case-item-bottom"); counts.append(node("span", "", `${gpsLabel} · ${fmt(c.apc_count)} APC`));
      const attention = ["ambiguous", "unmatched", "ineligible"].reduce((n, s) => n + (c.status_counts?.[s] || 0), 0);
      if (attention) { const s = node("span", "mini-status"); const dot = node("i", "red-bg"); s.append(dot, document.createTextNode(String(attention))); counts.append(s); } b.append(counts);
      if (isDirection()) b.append(node("div", conflictCount(c) ? "direction-conflict-badge" : "table-route-name", `方向冲突窗口 ${fmt(conflictCount(c))} · ${c.kind === "direction_unresolved" ? "未定位审查桶，非候选 trip" : isGTFS() ? `GTFS ${c.gtfs_status || "待查看"}（非真值）` : "候选，未匹配"}`));
      if (shortCandidate(c)) b.append(node("div", "direction-conflict-badge", shortCandidateNote(c)));
      if (c.gap_split) { b.dataset.parentCaseId = c.parent_case_id; b.append(node("div", "gap-fragment-badge", c.gap_unlocated ? "联合分段 · 无法定位时刻，原记录保留" : `联合间隔子片段 ${c.gap_fragment_index} / ${c.gap_fragment_count}`)); }
      b.title = `${c.title || c.id}\n${kind} · ${VERDICT[review]}`;
      b.addEventListener("click", () => { if (c.id !== state.caseId && confirmLeave()) loadCase(c.id); }); frag.append(b);
    }
    $("case-list").replaceChildren(frag); $("load-more").hidden = state.limit >= state.filtered.length;
  }
  function updateNavigation() { const i = state.filtered.findIndex((c) => c.id === state.caseId); txt("case-position", `${i < 0 ? "—" : i + 1} / ${state.filtered.length || "—"}`); $("previous-case").disabled = i <= 0 || !state.detail; $("next-case").disabled = i < 0 || i >= state.filtered.length - 1 || !state.detail; }
  async function loadCase(id, anchor = null) {
    const seq = ++state.sequence; state.caseId = id; state.selected = null; state.detail = null; state.cursor = null; state.view = null; state.dirty = false; state.selectedObservation = null; state.observationPage = 0;
    error(""); $("case-workspace").hidden = true; $("no-case").hidden = true; $("case-loading").hidden = false; renderQueue(); updateNavigation();
    try {
      const detail = await api(`/api/case?method=${encodeURIComponent(state.method)}&vehicle=${encodeURIComponent(state.vehicle)}&case_id=${encodeURIComponent(id)}&context_s=${state.contextSeconds}${matchingQuery()}`);
      if (seq !== state.sequence) return;
      state.detail = detail; state.caseId = detail.case?.id || id;
      state.directionSources = new Map(arr(detail.observations).map((row) => [`${row.source}|${observationRid(row)}`, row]));
      const c = detail.case || state.cases.find((x) => x.id === id) || {};
      renderRepresentation(); renderGapPanel();
      txt("case-kicker", `${c.kind === "vehicle_stream" ? isDedup() ? "VEHICLE-DAY · EXACT AVL DEDUP" : "VEHICLE-DAY · SOURCE UNION ALL" : c.kind === "segment" ? "CANDIDATE SEGMENT" : c.kind === "apc_only" ? "UNASSIGNED APC WINDOW" : "UNRESOLVED CONTEXT"} · ${state.method}`);
      txt("case-title", isStream() ? `${c.vehicle_label || c.vehicle || state.vehicle} · ${c.date || date(c.start)} 全日观测` : `${c.vehicle_label || c.vehicle || state.vehicle} · ${range(c.start, c.end)}`);
      txt("case-subtitle", `${c.date || date(c.start)} · ${arr(c.routes).length ? "源线路 " + c.routes.join(" / ") : "线路未知"} · ${isStream() ? `${isDedup() ? "AVL 完全重复副本已合并；" : ""}仅同车汇集与时间排序，未分割或匹配 · ` : state.debug?.enabled ? "单日观测截面，非 trip 始终点 · " : ""}${c.id || id}`);
      const windowGPS = arr(detail.avl).filter((p) => p.in_window !== false && validGPS(p)).length;
      txt("metric-avl", fmt(windowGPS)); txt("metric-avl-note", isStream() ? `当天 AVL ${isDedup() ? "去重保留" : "来源"} ${fmt(c.avl_count)} 行 · 不代表匹配` : c.kind === "segment" ? `片段成员 ${fmt(c.avl_member_count ?? c.avl_count)} 行 · 窗口含上下文` : "同窗证据 ≠ 已关联 · 此卡片不是 AVL 片段"); txt("metric-apc", fmt(c.apc_count)); renderDedupSummary();
      txt("metric-support", `${fmt(c.status_counts?.direct || 0)} / ${fmt(c.status_counts?.partial || 0)}`);
      txt("metric-attention", fmt(["ambiguous", "unmatched", "ineligible"].reduce((n, s) => n + (c.status_counts?.[s] || 0), 0)));
      if (isDirection()) { txt("case-kicker", `${c.kind === "direction_unresolved" ? "UNLOCATED DIRECTION REVIEW" : "DIRECTION CANDIDATE"} · ${state.method}`); txt("case-title", `${c.vehicle_label || c.vehicle || state.vehicle} · ${c.kind === "direction_unresolved" ? "未定位方向审查窗口" : directionLabel(c)} · ${candidateRange(c)}`); txt("case-subtitle", `${c.date || date(c.start)} · ${c.kind === "direction_unresolved" ? "审查桶，不是方向候选或真实 trip" : "方向候选，非已确认 trip"}${shortCandidate(c) ? " · " + shortCandidateNote(c) : ""} · 方向冲突窗口 ${fmt(conflictCount(c))} · ${c.id}`); txt("metric-avl-note", `候选 AVL 成员 ${fmt(c.avl_member_count ?? c.avl_count)} · 已加载有效 GPS ${fmt(windowGPS)}（可含上下文）`); $("observation-conflicts-only").checked = false; renderDirectionPanel(); }
      $("apc-search").value = "";
      const review = detail.review || state.reviews[id] || { verdict: "unreviewed", notes: "", revision: 0 };
      loadReview(review); renderWarnings();
      $("case-loading").hidden = true; $("case-workspace").hidden = false;
      state.cursor = timestamp(c.start) ?? timestamp(detail.window?.start);
      const first = arr(detail.apc).find((r) => r.in_focus !== false && ["ambiguous", "partial"].includes(rowStatus(r))) || arr(detail.apc).find((r) => r.in_focus !== false) || arr(detail.apc)[0];
      state.selected = first ? rid(first.rid) : null;
      const [fullStart, fullEnd] = sourceBounds(); if (fullEnd - fullStart > 3 * 3600) { const firstObservation = isStream() ? arr(detail.observations).find((row) => finite(timestamp(row.event_time)) && timestamp(row.event_time) >= fullStart && timestamp(row.event_time) < fullEnd) : null; const visibleStart = firstObservation ? Math.max(fullStart, Math.min(timestamp(firstObservation.event_time), fullEnd - 3600)) : fullStart; state.view = [visibleStart, Math.min(fullEnd, visibleStart + 3600)]; $("window-size").value = "3600"; } else $("window-size").value = "full";
      if (state.view && !visibleAPC().some((r) => rid(r.rid) === state.selected)) state.selected = visibleAPC().find((r) => r.in_focus !== false)?.rid ?? visibleAPC()[0]?.rid ?? null;
      if (state.selected != null) state.selected = rid(state.selected);
      if (anchor) restoreAnchor(anchor);
      if (isStream()) { state.selectedObservation = state.selected ? { source: "apc", rid: state.selected } : null; if (anchor?.observationSource === "avl") { const retained = findSourceObservation("avl", anchor.observationRid); if (retained) { state.selectedObservation = { source: "avl", rid: observationRid(retained) }; state.selected = null; if (observationRid(retained) !== anchor.observationRid) toast("原选 AVL 是完全重复副本，已定位到该组保留记录；完整原始 ID 见去重追溯，未推断 APC 匹配。"); } } $("observation-search").value = ""; $("observation-source-filter").value = ""; $("observation-window-only").checked = false; renderObservations(); }
      if (isGTFS()) initGTFS();
      renderWindowControls(); fitMap(); renderInspector(); renderTable(); renderCharts(); updateNavigation(); renderQueue();
      $("case-list").querySelector(".active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    } catch (e) { if (seq === state.sequence) { error(e.message); $("case-loading").hidden = true; } }
  }
  function navigate(delta, skipConfirm = false) { const i = state.filtered.findIndex((c) => c.id === state.caseId); const next = state.filtered[i + delta]; if (state.detail && next && (skipConfirm || confirmLeave())) loadCase(next.id); }
  function selectAPC(id, cursor = true) {
    if (!state.detail) return; state.selected = rid(id); const r = selectedAPC(); if (!r) return;
    if (isStream()) state.selectedObservation = { source: "apc", rid: rid(id) };
    if (cursor) state.cursor = timestamp(r.arrival) ?? timestamp(r.departure) ?? state.cursor;
    if (state.view && finite(state.cursor) && (state.cursor < state.view[0] || state.cursor > state.view[1])) focusSelected();
    if (isStream() && validCoords(r)) { state.map.center = mercator(r.lat, r.lon); state.map.scale = Math.max(state.map.scale, 256 * 2 ** 14); }
    renderInspector(); renderTable(); renderMap(); renderCharts();
    if (isStream()) renderObservations(true);
    for (const tr of $("apc-table-body").rows) { const selected = tr.dataset.rid === state.selected; tr.classList.toggle("selected", selected); tr.setAttribute("aria-selected", String(selected)); }
  }
  function dlPair(dl, label, value) { dl.append(node("dt", "", label), node("dd", "", fmt(value))); }
  const observationRid = (row) => rid(row.source_rid ?? row.rid);
  const observationTimeBasis = (row) => row.event_time_basis ?? row.time_basis ?? "missing";
  const observationAliases = (row) => arr(row.deduplication?.source_rids).map(rid);
  const findSourceObservation = (source, id) => arr(state.detail?.observations).find((row) => row.source === source && (observationRid(row) === rid(id) || (source === "avl" && observationAliases(row).includes(rid(id)))));
  const selectedObservationRow = () => arr(state.detail?.observations).find((row) => row.source === state.selectedObservation?.source && observationRid(row) === state.selectedObservation?.rid);
  function eventTimeText(value) { const t = timestamp(value); if (t == null) return "—（保留缺失时间）"; return new Date(t * 1000).toISOString().replace("T", " ").replace("Z", ""); }
  function sourceTimeText(row) { return row.source === "avl" ? `time ${eventTimeText(row.time)}` : `arrival ${eventTimeText(row.arrival)} → departure ${eventTimeText(row.departure)}`; }
  function observationSelected(row) { return state.selectedObservation?.source === row.source && state.selectedObservation?.rid === observationRid(row); }
  function renderDedupSummary() {
    const box = $("dedup-summary"); box.hidden = !isDedup(); box.replaceChildren(); if (!isDedup() || !state.detail) return;
    const counts = state.detail.counts || {}, c = state.detail.case || {}, original = counts.source_avl_total ?? c.source_counts?.avl, retained = counts.retained_avl_total ?? c.retained_counts?.avl, removed = counts.removed_avl_duplicates ?? c.removed_avl, apc = counts.retained_apc_total ?? c.apc_count;
    box.append(node("strong", "", "当前车辆 · 仅 AVL 完全重复去重："));
    for (const [id, label, value] of [["original-avl", "原 AVL", original], ["retained-avl", "保留 AVL", retained], ["removed-avl", "合并副本", removed], ["apc", "APC 全部保留", apc]]) { const item = node("span", "dedup-count"); item.append(document.createTextNode(` ${label} `)); const number = node("strong", "", fmt(value)); number.id = `dedup-${id}`; number.dataset.count = String(value ?? ""); item.append(number, document.createTextNode(" 行 ·")); box.append(item); }
    box.append(node("span", "", " 只有原 CSV 13 个字段全部相同才合并；同时间或同坐标不单独构成去重依据。搜索组内任一原始 source rid 可找到保留行与完整追溯。原文件和旧实验未改动。"));
    if (isDirection()) { box.replaceChildren(node("strong", "", "当前车辆全日处理：")); for (const [key, label] of [["source_avl_total", "原 AVL"], ["deduplicated_avl_total", "精确去重后 AVL"], ["filtered_avl_pi_po", "排除 PI/PO"], ["retained_avl_total", "保留 AVL"], ["source_apc_total", "APC 全部保留"]]) { const span = node("span", "dedup-count", ` ${label} ${fmt(counts[key])} 行 ·`); span.dataset.countKey = key; span.dataset.count = String(counts[key] ?? ""); box.append(span); } box.append(node("span", "", ` 当前候选含 ${fmt(c.avl_count)} 条 AVL / ${fmt(c.apc_count)} 条 APC；上下文不是成员，跨候选记录不能重复计数。原 CSV、被过滤记录与副本追溯仍保留。`)); }
    if (isGTFS()) { box.replaceChildren(node("strong", "", "Route 29 · 派生审查范围："), node("span", "", ` 本车日选入 ${fmt(counts.vehicle_observations)} 条来源记录，排除其它线路 ${fmt(counts.excluded_other_route_count)} 条（包含 929，不与 29 合并）。当前候选成员 ${fmt(c.avl_count)} AVL / ${fmt(c.apc_count)} APC；加载上下文单独保留。父实验全线路的 APC 保留与 AVL 去重审计仍在旧版本，本页不能声称展示全部线路 APC。`)); }
  }
  function renderDirectionPanel() {
    renderGapPanel();
    if (!isDirection() || !state.detail) return;
    const c = state.detail.case || {}, conflicts = directionConflicts(), list = $("direction-conflict-list"); list.replaceChildren();
    txt("direction-conflict-count", `${fmt(conflicts.length)} 个已加载冲突窗口`); txt("direction-conflict-summary", `${c.kind === "direction_unresolved" ? "未定位审查桶，不是方向候选" : directionLabel(c) + " 候选"} · 冲突记录仍保留，橙红色不表示匹配失败`);
    txt("direction-candidate-note", `${c.kind === "direction_unresolved" ? "当前是未定位记录审查桶，不是生成的候选 trip。" : `候选边界 ${candidateRange(c)}；成员原始覆盖 ${range(c.source_start, c.source_end)}；首次确定方向 ${time(c.first_definite_at, true)}，未知方向累计 ${fmt(c.unknown_duration_s)} 秒。${c.scope_censored_start || c.scope_censored_end || c.scope_boundary_start || c.scope_boundary_end ? "受日范围截断，不代表行程始终点。" : ""}`} APC 闭区间跨边界时可保留多个 candidate_segment_ids，不能强选唯一归属；上下文只帮助解释冲突，不自动成为本候选成员。Heading 仅为来源角度，不参与运营方向判定。`);
    if (c.gap_split && !c.gap_unlocated) txt("direction-candidate-note", `当前实际成员覆盖 ${candidateRange(c)}；持久化候选区间 ${range(c.start, c.end)}（外缘可能只是日期截断，不能当作实际 trip 始终点）。方向继承父候选；原始 parent candidate_segment_ids 与当前 review_candidate_ids 分开保存。这里只显示与本子片段有实际重叠的方向冲突，原父片段全部冲突另在上方审计中保留。上下文不自动成为子片段成员。`);
    txt("direction-rules", `${c.gap_split ? "以下是父实验方向规则；本运行另按 APC + AVL 联合观测空档细分，详见上方子片段证据。\n" : ""}固定 v1：AVL Destination 只提取明确方向词，方向证据向后保持至下一不同 AVL 时刻，最多 30 秒；未知方向和 PI/PO 也截断保持，最后 AVL 仅点证据。APC 使用到离站正序闭区间；缺端点或倒序采用明确标记的点证据，不修复原时刻。仅非冲突、确定方向反转形成候选边界；冲突保留在前一候选，未知间隔不凭空新增行程。无时空 matcher、无 APC 距离投影。\n\n` + json({ rules: state.meta.direction_segmentation || state.meta.direction_rules || state.meta.segmentation_rules || currentDebugRun()?.rules, candidate: c, boundaries: state.detail.boundaries, segments: state.detail.segments }));
    txt("direction-filtered-count", `（${fmt(arr(state.detail.filtered_avl).length)} 行）`); txt("direction-filtered-json", $("direction-filter-audit").open ? json(state.detail.filtered_avl || []) : "");
    for (const conflict of conflicts) {
      const card = node("div", "direction-conflict-card"); card.dataset.conflictId = rid(conflict.id); const instant = (value) => eventTimeText(value).slice(11), interval = conflict.kind === "point" ? `${instant(conflict.start)} · 点冲突` : `${conflict.start_inclusive === false ? "(" : "["}${instant(conflict.start)}–${instant(conflict.end)}${conflict.end_inclusive === false ? ")" : "]"} · 区间`; const focus = node("button", "direction-conflict-focus", `${interval} · ${arr(conflict.types).join(" / ") || "方向冲突"}`); focus.type = "button"; focus.dataset.focusConflict = rid(conflict.id); focus.addEventListener("click", () => focusDirectionConflict(conflict)); card.append(focus);
      const sources = node("div", "direction-conflict-sources"); for (const source of arr(conflict.source_rids)) { const row = findSourceObservation(source.source, source.rid), button = node("button", "mono", `${String(source.source).toUpperCase()} ${row ? directionLabel(row) : ""} · ${rid(source.rid)}`); button.type = "button"; button.dataset.source = source.source; button.dataset.rid = rid(source.rid); button.disabled = !row; button.title = row ? "查看该方原始记录；不建立 APC–AVL 匹配" : "该来源记录不在当前加载范围，完整 ID 仍保留"; button.addEventListener("click", () => { if (row) selectObservation(row); }); sources.append(button); } card.append(sources); list.append(card);
    }
    if (!conflicts.length) list.append(node("p", "stream-order-note", "当前候选未标记方向冲突；这不证明候选 trip 身份正确。"));
  }
  function renderGapPanel() {
    const c = state.detail?.case || {}, panel = $("gap-segmentation-panel"); panel.hidden = !c.gap_split; if (!c.gap_split) return;
    panel.dataset.parentCaseId = rid(c.parent_case_id); panel.dataset.caseId = rid(c.id);
    txt("gap-fragment-title", c.gap_unlocated ? "无法定位时刻的保留记录" : `联合观测间隔子片段 ${c.gap_fragment_index} / ${c.gap_fragment_count}`);
    txt("gap-parent-id", `父方向候选 ${c.parent_case_id}`);
    txt("gap-segmentation-note", `${c.gap_unlocated ? "这些成员缺少可定位的有效时刻，保留在独立审查桶，不生成时间轨迹。" : `当前子片段 ${candidateRange(c)}；实际双源覆盖 ${range(c.source_start, c.source_end)}。`} 保留父方向判断，仅在 APC + AVL 联合观测覆盖之间严格大于阈值的空档处分开；APC 到离站区间可覆盖空档，不将 APC 变成 AVL 点。焦点只含持久化子片段成员，上下文只限本片段邻近窗口；这不是已确认 trip。`);
    const list = $("gap-boundary-list"); list.replaceChildren();
    for (const [side, gap] of [["before", c.gap_boundary_before], ["after", c.gap_boundary_after]]) {
      if (!gap) continue; const item = node("div", "gap-boundary"); item.dataset.side = side; item.dataset.durationSeconds = String(gap.duration_s); item.dataset.start = String(gap.start); item.dataset.end = String(gap.end);
      item.append(node("strong", "", `${side === "before" ? "片段前" : "片段后"}双源空档 · ${fmt(gap.duration_s / 60)} 分钟（${fmt(gap.duration_s)} 秒）`), node("span", "", `${eventTimeText(gap.start)} → ${eventTimeText(gap.end)}`), node("small", "", `分段条件：空档 ${gap.operator || ">"} ${fmt(gap.threshold_s)} 秒；边界来源 ID 在下方审计保留。`)); list.append(item);
    }
    if (!list.children.length) list.append(node("p", "stream-order-note", c.gap_unlocated ? "未定位记录不参与空档持续时间计算。" : "此片段未增加联合空档边界；原父方向边界仍保留。"));
    txt("gap-segmentation-audit", json({ parent_case_id: c.parent_case_id, parent_segment_start: c.parent_segment_start, parent_segment_end: c.parent_segment_end, parent_boundary_reason: c.parent_boundary_reason, gap_boundary_before: c.gap_boundary_before, gap_boundary_after: c.gap_boundary_after, membership_basis: state.detail.gap_membership_basis, source_rids: c.source_rids, parent_direction_conflicts: state.detail.parent_direction_conflicts }));
  }
  function focusDirectionConflict(conflict) {
    if (!state.detail) return; const [start, end] = sourceBounds(), a = timestamp(conflict.start), b = timestamp(conflict.end); if (a == null || b == null) return;
    const size = Math.min(end - start, Math.max(600, b - a + 120)), at = Math.max(start, Math.min(end - size, (a + b - size) / 2)); state.view = [at, at + size]; if (state.view[1] <= state.view[0]) state.view = null; state.cursor = Math.max(start, Math.min(end, (a + b) / 2));
    const first = arr(conflict.source_rids).map((source) => findSourceObservation(source.source, source.rid)).find(Boolean); if (first) { state.selectedObservation = { source: first.source, rid: observationRid(first) }; state.selected = first.source === "apc" ? observationRid(first) : null; }
    state.selectedDirectionConflict = rid(conflict.id); $("direction-panel").dataset.selectedConflict = rid(conflict.id); $("window-size").value = "600"; refreshWindow(); if (isStream()) renderObservations(true);
  }
  function drawDirectionConflicts(g, dim, x, a, b) {
    if (!isDirection() || !$("direction-conflicts-visible").checked) return;
    for (const conflict of directionConflicts()) { const start = timestamp(conflict.start), end = timestamp(conflict.end); if (start == null || end == null || end < a || start > b) continue; const mark = svg("rect", { x: x(Math.max(a, start)) - (start === end ? 1.5 : 0), y: dim.top, width: Math.max(3, x(Math.min(b, end)) - x(Math.max(a, start))), height: dim.height - dim.top - dim.bottom, fill: COLORS.counter, "fill-opacity": .13, stroke: COLORS.counter, "stroke-opacity": .4, "stroke-width": .8, "data-conflict-id": conflict.id, class: "svg-clickable" }); mark.append(svg("title", {}, `方向冲突 ${conflict.id} · ${arr(conflict.source_rids).map((r) => `${r.source}:${r.rid}`).join(" / ")}`)); mark.addEventListener("click", (event) => { event.stopPropagation(); focusDirectionConflict(conflict); }); g.append(mark); }
  }
  function renderObservations(reveal = false) {
    if (!isStream() || !state.detail) return;
    const observations = arr(state.detail.observations), source = $("observation-source-filter").value, query = $("observation-search").value.trim().toLowerCase(), windowOnly = $("observation-window-only").checked, conflictOnly = isDirection() && $("observation-conflicts-only").checked, [start, end] = bounds();
    // Keep persisted display_order. Filtering only selects a subsequence;
    // exact AVL deduplication, if requested, already happened in the runner.
    const rows = observations.filter((row) => (!source || row.source === source) && (!conflictOnly || sourceConflictIds(row.source, row).length > 0) && (!windowOnly || (timestamp(row.event_time) != null && timestamp(row.event_time) >= start && timestamp(row.event_time) <= end)) && (!query || `${row.source} ${observationRid(row)} ${observationAliases(row).join(" ")} ${row.route} ${row.route_name || ""} ${row.branch || ""} ${directionLabel(row)} ${candidateIds(row).join(" ")} ${sourceTimeText(row)} ${eventTimeText(row.event_time)} ${observationTimeBasis(row)} ${row.lat} ${row.lon}`.toLowerCase().includes(query)));
    state.observationRows = rows; const size = Number($("observation-page-size").value), pages = Math.max(1, Math.ceil(rows.length / size));
    if (reveal) { const index = rows.findIndex(observationSelected); if (index >= 0) state.observationPage = Math.floor(index / size); }
    state.observationPage = Math.max(0, Math.min(pages - 1, state.observationPage)); const first = state.observationPage * size, page = rows.slice(first, first + size), fragment = document.createDocumentFragment();
    for (const row of page) {
      const tr = node("tr", `${observationSelected(row) ? "selected " : ""}${isDirection() && sourceConflictIds(row.source, row).length ? "direction-conflict-row" : ""}`); tr.tabIndex = 0; tr.dataset.rid = observationRid(row); tr.dataset.source = row.source; tr.dataset.displayOrder = String(row.display_order); tr.setAttribute("aria-selected", String(observationSelected(row)));
      const type = node("td"); type.append(node("div", "table-mono", `#${fmt(row.display_order)}`), node("span", `source-badge source-${row.source === "apc" ? "apc" : "avl"}`, String(row.source || "未知").toUpperCase())); tr.append(type);
      const instant = node("td"); instant.append(node("div", "table-mono", eventTimeText(row.event_time)), node("div", "table-route-name mono", observationTimeBasis(row))); tr.append(instant);
      const identity = node("td", "observation-rid mono", observationRid(row)); identity.title = observationRid(row); if (row.source === "avl" && row.deduplication?.duplicate_group_size > 1) { tr.dataset.duplicateGroupSize = String(row.deduplication.duplicate_group_size); identity.append(node("small", "dedup-source-badge", `完全重复组 ${fmt(row.deduplication.duplicate_group_size)} 行 · 保留代表`)); } tr.append(identity);
      const route = node("td"); route.append(node("div", "", fmt(row.route)), node("div", "table-route-name", fmt(row.route_name)), node("div", "table-mono", `Branch ${fmt(row.branch)}`)); tr.append(route);
      if (isDirection()) { const ids = candidateIds(row), conflicts = sourceConflictIds(row.source, row), direction = node("td", "direction-source-cell"); tr.dataset.direction = directionLabel(row); tr.dataset.conflictCount = String(conflicts.length); tr.dataset.candidateIds = JSON.stringify(ids); direction.append(node("div", "", directionLabel(row)), node("small", "table-route-name", row.direction_basis || "方向依据未给出"), node("div", "table-route-name", row.is_candidate_member ? "本候选成员（不是匹配）" : "上下文 / 审查记录"), node("small", ids.length > 1 ? "direction-conflict-badge" : "table-route-name", ids.length ? `${ids.length} 个候选${ids.length > 1 ? " · 归属非唯一" : ""}` : "未定位到候选，记录保留")); if (conflicts.length) direction.append(node("small", "direction-conflict-badge", `${conflicts.length} 个方向冲突窗口`)); direction.title = ids.join("\n"); tr.append(direction); }
      tr.append(node("td", "table-mono", `${row.lat ?? "—"}, ${row.lon ?? "—"}`), node("td", "observation-source-time mono", sourceTimeText(row)));
      tr.addEventListener("click", () => selectObservation(row)); tr.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectObservation(row); } }); fragment.append(tr);
    }
    if (!page.length) { const tr = node("tr"), td = node("td", "empty-small", "当前搜索 / 来源 / 时间窗筛选下没有记录。筛选未删除来源数据。"); td.colSpan = isDirection() ? 7 : 6; tr.append(td); fragment.append(tr); }
    $("observation-table-body").replaceChildren(fragment); txt("observation-count", `${isDirection() ? "候选加载" : "全日"} ${fmt(observations.length)} ${isDedup() ? "保留记录" : "来源行"} · 全量可翻页`);
    txt("observation-page-info", `${fmt(rows.length)} / ${fmt(observations.length)} 行符合筛选 · 当前 ${rows.length ? fmt(first + 1) : "0"}–${fmt(first + page.length)} · 第 ${state.observationPage + 1} / ${pages} 页`);
    $("observation-page").value = state.observationPage + 1; $("observation-page").max = pages;
    $("observation-first").disabled = $("observation-previous").disabled = state.observationPage === 0; $("observation-last").disabled = $("observation-next").disabled = state.observationPage >= pages - 1;
    const selectedHidden = state.selectedObservation && !rows.some(observationSelected); $("observation-selection-note").hidden = !selectedHidden; txt("observation-selection-note", "当前选中记录不符合表格筛选；原始详情与地图仍保留。清空来源 / 搜索 / 时间窗筛选可定位该行。");
  }
  function selectObservation(row) {
    if (!isStream()) return;
    if (row.source === "apc") { selectAPC(observationRid(row)); return; }
    state.selected = null; state.selectedObservation = { source: row.source, rid: observationRid(row) }; const t = timestamp(row.event_time);
    if (finite(t)) { state.cursor = t; if (state.view && (t < state.view[0] || t > state.view[1])) { const [a, b] = sourceBounds(); state.view = [Math.max(a, t - 300), Math.min(b, t + 300)]; $("window-size").value = "600"; } }
    renderWindowControls(); renderInspector(); renderObservations(true); fitMap(); if (validCoords(row)) { state.map.center = mercator(row.lat, row.lon); state.map.scale = Math.max(state.map.scale, 256 * 2 ** 14); } renderMap(); renderCharts();
  }
  function renderSourceInspector() {
    const box = $("inspector"), row = selectedObservationRow(); box.replaceChildren();
    setBadge($("selected-status"), row ? "not_evaluated" : "未选择");
    if (!row) { const empty = node("div", "inspector-empty"); empty.append(node("strong", "", "选择一条 APC 或 AVL 原始观测"), node("p", "", "从统一观测表点击来源记录。这里没有 trip、匹配评分或采纳证据。")); box.append(empty); return; }
    box.append(node("h4", "record-heading", `${String(row.source).toUpperCase()} 原始观测`), node("div", "record-id", observationRid(row)));
    const dl = node("dl", "record-dl"); dlPair(dl, "源显示序号", row.display_order); dlPair(dl, "event_time", eventTimeText(row.event_time)); dlPair(dl, "时间依据", observationTimeBasis(row)); dlPair(dl, "VehicleID", row.vehicle); dlPair(dl, "Route", row.route); dlPair(dl, "RouteName", row.route_name); dlPair(dl, "Branch", row.branch); dlPair(dl, "位置", `${row.lat ?? "—"}, ${row.lon ?? "—"}`);
    if (row.source === "apc") { dlPair(dl, "来源到站", eventTimeText(row.arrival)); dlPair(dl, "来源离站", eventTimeText(row.departure)); dlPair(dl, "站点 / 方向", `${fmt(row.stop)} / ${fmt(row.direction)}`); dlPair(dl, "上 / 下车", `${fmt(row.boarding)} / ${fmt(row.alighting)}`); dlPair(dl, "到 / 离载量", `${fmt(row.arrival_load)} / ${fmt(row.departure_load)}`); }
    else { dlPair(dl, "来源定位时刻", eventTimeText(row.time)); dlPair(dl, "速度 km/h", row.speed); dlPair(dl, "Run", row.run); dlPair(dl, "Destination", row.destination); }
    if (isDirection()) { dlPair(dl, "候选运营方向", directionLabel(row)); dlPair(dl, "方向证据依据", row.direction_basis); dlPair(dl, "方向证据区间", `${eventTimeText(row.direction_evidence_start)} → ${eventTimeText(row.direction_evidence_end)} · ${fmt(row.direction_interval_kind)}`); dlPair(dl, "原 Heading", row.heading ?? row.source_fields?.heading); dlPair(dl, "当前成员关系", row.is_candidate_member ? "本候选成员，非 APC–AVL 匹配" : "上下文 / 未定位审查记录"); dlPair(dl, "候选 IDs（可多个）", candidateIds(row).length ? candidateIds(row).join("\n") : "未定位到方向候选，来源记录保留"); dlPair(dl, "跨候选边界", row.candidate_boundary_overlap ? "是 · 不强选唯一归属" : "未标记"); dlPair(dl, "方向冲突 IDs", sourceConflictIds(row.source, row).join("\n") || "无已标记冲突"); }
    if (state.detail.case?.gap_split) { dlPair(dl, "原父方向候选 IDs", arr(row.candidate_segment_ids).join("\n") || "原记录无父候选标注"); dlPair(dl, "当前 review_candidate_ids", candidateIds(row).join("\n") || "不属于当前子片段目录"); dlPair(dl, "子片段归属依据", "持久化 source_rids；不以父 candidate_segment_ids 推定"); }
    box.append(dl, node("div", "reason-box", isDirection() ? "这是方向分割候选，不是已确认 trip。Direction / Destination 用于运营方向证据，Heading 不参与分割；未执行 APC–AVL 时空匹配或 APC 进度投影。方向冲突与多候选归属原样保留。" : "仅按车辆号汇集：未执行时空匹配、trip 分割或 APC 进度投影。同车、同时或同坐标都不自动说明同一停站。"));
    if (isGTFS()) {
      box.querySelector(".reason-box").textContent = `${state.detail.case?.gap_split ? "原始来源与父方向标注保留；当前子片段成员严格按 source_rids 确定，上下文不算成员。" : "原始来源与方向候选归属保持不变；"}GTFS 新图使用独立 reference 投影／APC 站点证据，不使用最近 AVL 给 APC 造进度。OFF_REFERENCE 不参与 s–t 曲线或有效支持。`;
      const evidence = (row.source === "avl" ? gtfsProjection() : gtfsAPCEvidence()).filter((p) => rid(p.rid || p.source_rid) === observationRid(row)), originalProjection = row.source === "avl" ? arr(gtfsData().projection?.points).filter((p) => rid(p.rid || p.source_rid) === observationRid(row)) : [], audit = node("details", "gtfs-source-evidence"); audit.open = true; audit.append(node("summary", "", `GTFS ${row.source === "avl" ? "参考投影" : "站点区间"}证据 · ${evidence.length} 项`), node("pre", "mono", json({ reference_evidence: evidence, source_projection_diagnostics: originalProjection }))); box.append(audit);
    }
    if (row.source === "avl" && row.deduplication) {
      const group = row.deduplication, trace = node("details", "dedup-trace"); trace.id = "dedup-source-trace"; trace.open = group.duplicate_group_size > 1; trace.dataset.representativeRid = rid(group.representative_rid); trace.dataset.groupSize = String(group.duplicate_group_size);
      trace.append(node("summary", "", `AVL 去重追溯 · 原 ${fmt(group.duplicate_group_size)} 行 → 保留 1 行`), node("p", "stream-order-note", "按原 CSV 13 个字段精确相等分组，保留 source_row 最小的一行。下列列表包含代表与被合并副本；来源行号和原始 ID 未改写。"));
      const ids = node("pre", "mono", observationAliases(row).map((id, index) => `${id === rid(group.representative_rid) ? "保留" : "合并副本"} · source_row ${arr(group.source_rows)[index] ?? "—"} · ${id}`).join("\n")); ids.id = "dedup-source-rids"; trace.append(ids); box.append(trace);
    }
    const raw = node("details"); raw.open = row.source === "avl"; raw.append(node("summary", "", "完整来源字段（展示编码）"), node("p", "stream-order-note", "大整数以十进制字符串显示以保留精度；时间按原始本地时轴编码，源文件未改动。"), node("pre", "mono", json(row.source_fields || row))); box.append(raw);
  }
  function renderInspector() {
    if (isStream()) { renderSourceInspector(); return; }
    const box = $("inspector"), r = selectedAPC(); box.replaceChildren();
    if (!r) { setBadge($("selected-status"), "未选择"); const e = node("div", "inspector-empty"); e.append(node("span", "empty-crosshair", "⌖"), node("strong", "", "没有选中的 APC 观测"), node("p", "", "该片段可能只有 AVL。也可开启上下文层查看邻近记录。")); box.append(e); return; }
    const d = decision(r); setBadge($("selected-status"), rowStatus(r));
    box.append(node("h4", "record-heading", `站点 ${fmt(r.stop)}`), node("div", "record-id", `APC ${rid(r.rid)}`));
    const dl = node("dl", "record-dl");
    dlPair(dl, "原始到站", `${date(r.arrival)} ${time(r.arrival, true)}`); dlPair(dl, "原始离站", `${date(r.departure)} ${time(r.departure, true)}`);
    dlPair(dl, "Route / 方向", `${fmt(r.route)} / ${fmt(r.direction)}`); dlPair(dl, "RouteName", r.route_name ?? r.RouteName ?? r.source?.RouteName); dlPair(dl, "Branch", r.branch ?? r.Branch ?? r.source?.Branch); dlPair(dl, "源线路一致性", { concordant: "标签一致（非线路身份确认）", mixed: "GPS 支持包含多种线路标签", conflict: "APC / GPS 线路标签不一致", unknown: "未知" }[d.route_state] || d.route_state); dlPair(dl, "支持范围", LEVEL[r.support_level || d.support_level] || r.support_level || d.support_level);
    dlPair(dl, "证据类型", { point: "实际 GPS 点", interpolation: "相邻 GPS 插值（推导）", mixed: "实测点 + 推导插值" }[d.support_kind] || d.support_kind);
    dlPair(dl, "片段上下文", { single_segment: "单一片段", multiple_segments: "跨多个片段 · 归属未确认", unmapped: "未映射", no_assignment: "无分配" }[d.context_status] || d.context_status);
    dlPair(dl, "当前关系", RELATION[r.relation] || r.relation); dlPair(dl, "评分 / 差距", `${fmt(d.score)} / ${fmt(d.margin)}（非概率）`);
    dlPair(dl, "候选访问数", d.candidate_count ?? d.episode_count); dlPair(dl, "上 / 下车", `${fmt(r.boarding)} / ${fmt(r.alighting)}`);
    dlPair(dl, "到 / 离车载", `${fmt(r.arrival_load)} / ${fmt(r.departure_load)}`);
    dlPair(dl, "APC 坐标", validCoords(r) ? `${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}` : "无效或缺失（地图不定位）"); box.append(dl);
    box.append(node("div", "reason-box", `判定原因：${r.reason || d.reason || "未提供"}${d.grain_uncertain ? " · APC 观测粒度未确认" : ""}${d.interval_excursion ? " · 区间内观测到远离站点" : ""}${d.coverage_start_missing || d.coverage_end_missing ? " · 源覆盖边界不完整" : ""}`));
    witnessList(box, arr(d.witnesses), false); witnessList(box, arr(d.counter_witnesses), true);
    const candidates = arr(d.candidates);
    if (candidates.length) { const det = node("details"); det.append(node("summary", "", `全部保留候选（${candidates.length}）`), node("pre", "mono", json(candidates))); box.append(det); }
    const raw = node("details"); raw.append(node("summary", "", "完整判定与来源 ID"), node("pre", "mono", json(d))); box.append(raw);
  }
  function witnessList(box, witnesses, counter) {
    box.append(node("div", "evidence-label", `${counter ? "时空反证" : "实际支持证据"} · ${witnesses.length}`));
    if (!witnesses.length) { box.append(node("div", "muted", counter ? "无保留反证（不表示不存在）" : "无采纳的正支持证据")); return; }
    for (const w of witnesses) {
      const item = node("div", `witness-item${counter ? " counter" : ""}`);
      item.append(node("strong", "", `${w.kind === "interpolation" ? "◇ 推导插值" : "● 实测点"} · ${{ arrival: "到站", departure: "离站", interior: "内部" }[w.target] || w.target || "观测"}`));
      item.append(node("div", "", `${time(w.support_time, true)} · 距离 ${fmt(w.distance_m)} m · 时间差 ${fmt(w.gap_s)} s${w.kind === "interpolation" ? ` · GPS 括区 ${fmt(w.bracket_gap_s)} s` : ""}`));
      item.append(node("div", "mono", `GPS ${arr(w.ping_ids).map(rid).join(" · ") || "—"}`));
      const all = new Map(arr(state.detail?.avl).map((p) => [rid(p.rid), p]));
      if (arr(w.ping_ids).some((id) => all.get(rid(id))?.in_window === false)) item.append(node("div", "", "包含显示时间窗外的真实源 GPS；查看完整判定 ID。"));
      if (counter) item.append(node("div", "", "反证依赖未校正时钟与当前运动约束，非源数据错误证明。")); box.append(item);
    }
  }
  function renderTable() {
    if (!state.detail) return; const q = $("apc-search").value.trim().toLowerCase();
    const rows = arr(state.detail.apc).filter((r) => !q || `${r.rid} ${r.stop} ${r.route} ${r.route_name || r.source?.RouteName || ""} ${r.branch || ""} ${r.direction} ${r.reason || decision(r).reason} ${STATUS[rowStatus(r)]}`.toLowerCase().includes(q)); state.tableRows = rows;
    txt("table-count", `${rows.length} 条${arr(state.detail.apc).some((r) => r.in_focus === false) ? " · 含上下文" : ""}`);
    const frag = document.createDocumentFragment(), displayed = rows.slice(0, 1000);
    const selectedRow = rows.find((r) => rid(r.rid) === state.selected);
    if (selectedRow && !displayed.includes(selectedRow)) displayed[displayed.length - 1] = selectedRow;
    const scrollTop = $("apc-table-body").closest(".table-scroll").scrollTop;
    for (const r of displayed) {
      const tr = node("tr", `${rid(r.rid) === state.selected ? "selected " : ""}${r.in_focus === false ? "table-context" : ""}`); tr.dataset.rid = rid(r.rid); tr.tabIndex = 0; tr.setAttribute("aria-selected", String(rid(r.rid) === state.selected));
      const stop = node("td"); stop.append(node("div", "table-stop", `站点 ${fmt(r.stop)}`), node("div", "table-rid mono", rid(r.rid))); tr.append(stop);
      tr.append(node("td", "table-mono", `${time(r.arrival, true)} → ${time(r.departure, true)}`));
      const routeCell = node("td"); routeCell.append(node("div", "", `${fmt(r.route)} · ${fmt(r.direction)}`), node("div", "table-route-name", fmt(r.route_name ?? r.source?.RouteName)), node("div", "table-mono", `Branch ${fmt(r.branch ?? r.source?.Branch)}`)); tr.append(routeCell);
      const statusCell = node("td"); const badge = node("span"); setBadge(badge, rowStatus(r)); statusCell.append(badge); tr.append(statusCell);
      tr.append(node("td", "table-mono", `${fmt(r.boarding)} / ${fmt(r.alighting)}`), node("td", "table-mono", `${fmt(r.arrival_load)} / ${fmt(r.departure_load)}`), node("td", "", RELATION[r.relation] || r.relation || "—"));
      tr.addEventListener("click", () => selectAPC(r.rid)); tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectAPC(r.rid); } }); frag.append(tr);
    }
    if (!rows.length) { const tr = node("tr"), td = node("td", "empty-small", "没有符合条件的 APC 记录。"); td.colSpan = 7; tr.append(td); frag.append(tr); }
    $("apc-table-body").replaceChildren(frag); $("apc-table-body").closest(".table-scroll").scrollTop = scrollTop;
    const selectedHidden = state.selected != null && !selectedRow;
    $("table-limit").hidden = rows.length <= 1000 && !selectedHidden;
    txt("table-limit", selectedHidden ? "当前选中 APC 不符合表格搜索条件；清空搜索可查看该行。地图、曲线与证据面板仍保持选中记录。" : "为保证交互速度最多显示 1,000 条（始终保留符合搜索的选中行）；请搜索缩小范围。图表不受此行数限制。");
  }
  function renderWarnings() {
    const warnings = arr(state.detail?.warnings).map((w) => typeof w === "string" ? w : json(w));
    if (isStream()) { warnings.unshift(isDirection() ? "这里只建立方向候选行程，未确认真实 trip。冲突按时间窗口计数，不是独立冲突事件数量；APC 多候选归属与未定位记录保留。" : "仅按源 VehicleID 汇集全日 APC / AVL：没有 trip、停站匹配、评分或采纳证据。", isDedup() ? `仅合并原 CSV 13 字段完全相同的 AVL 副本${isDirection() ? "并排除 AVL Route=PI/PO" : ""}，APC 全部保留；代表和被合并 source rid 均可追溯。保留记录按 display_order 展示，相同 event_time 不证明真实先后。` : "全部来源记录与重复记录按 display_order 保留；相同 event_time 的稳定显示顺序不证明真实先后。", "APC 进度始终未知，不按最近 AVL 投影。AVL 曲线仅为连续可视化观测段的实际累计距离，不是 trip 完成率。", "日期两端缺少相邻日上下文；日期截面与绘图间断都不是 trip 边界。"); $("case-warnings").replaceChildren(...warnings.map((w) => node("li", "", w))); txt("case-provenance", json({ run_id: state.run || state.meta?.run_id, representation: state.meta?.representation || "vehicle_observation_stream", method: state.method, case_id: state.caseId, observations: arr(state.detail?.observations).length, counts: state.detail?.counts, avl_deduplication: isDedup(), observation_matching_used: false, trip_segmentation_used: isDirection(), time_basis: state.meta?.time_basis, provenance: state.detail?.provenance })); return; }
    if (!warnings.length) warnings.push("未提供额外窗口警告；无警告不等于关联已验证。");
    warnings.push("APC 粒度、源时钟及实际 trip 身份仍需外部核实。人工结论单独保存，不回写源判定。", "距离仅在连续有效 GPS 观测段内累计，断点归零；显示时间窗只是浏览切片，不重分割源片段。", "上下文记录可能在相邻候选中重复出现。请勿跨行程加总此视图中的客流。");
    if (state.debug?.enabled) warnings.unshift(`${state.debug.date} 是固定单日原始证据截面。日期边缘截断不是 trip 起点或终点；缺失跨日上下文不能被自动补全。`);
    if (state.detail?.case?.date_clipped_start || state.detail?.case?.date_clipped_end || state.detail?.date_clipping?.start || state.detail?.date_clipping?.end) warnings.unshift("当前窗口受日期范围剪裁；请区分原始观测时间与显示边界。");
    $("case-warnings").replaceChildren(...warnings.map((w) => node("li", "", w)));
    txt("case-provenance", json({ run_id: state.meta?.run_id, method: state.method, case_id: state.caseId, window: state.detail?.window, counts: state.detail?.counts, time_basis: state.meta?.time_basis, provenance: state.detail?.provenance }));
  }
  function loadReview(review) { state.revision = review.revision || 0; state.dirty = false; $("review-notes").value = review.notes || ""; const v = Object.hasOwn(VERDICT, review.verdict) ? review.verdict : "unreviewed"; document.querySelector(`#review-verdict input[value="${v}"]`).checked = true; txt("review-save-state", review.updated_at ? `已保存 · ${VERDICT[v]}` : VERDICT[v]); $("review-save-state").classList.remove("dirty"); }
  function dirty() { if (!state.detail) return; state.dirty = true; txt("review-save-state", "有未保存修改"); $("review-save-state").classList.add("dirty"); }
  async function saveReview(next = false) {
    if (!state.detail || state.saving) return; const identity = { method: state.method, vehicle: state.vehicle, case_id: state.caseId, ...(state.debug?.enabled ? { run_id: state.run } : {}) };
    const verdict = document.querySelector("#review-verdict input:checked")?.value || "unreviewed", notes = $("review-notes").value;
    const nextId = state.filtered[state.filtered.findIndex((c) => c.id === state.caseId) + 1]?.id;
    state.saving = true; $("save-review").disabled = true; $("save-next").disabled = true; txt("review-save-state", "正在保存…");
    try {
      const saved = await api("/api/reviews", { method: "POST", headers: { "Content-Type": "application/json", "X-Review-Token": state.meta.csrf_token }, body: JSON.stringify({ ...identity, verdict, notes, revision: state.revision }) });
      if (identity.case_id === state.caseId && identity.method === state.method && identity.vehicle === state.vehicle && (!state.debug?.enabled || identity.run_id === state.run)) {
        state.reviews[state.caseId] = saved; state.revision = saved.revision || state.revision + 1;
        const changedDuringSave = $("review-notes").value !== notes || document.querySelector("#review-verdict input:checked")?.value !== verdict;
        if (!changedDuringSave) loadReview(saved); else dirty(); renderQueue();
        toast(changedDuringSave ? "已保存提交时的版本；新修改尚未保存。" : isStream() ? "审查笔记已保存；原始观测记录未修改，也未执行匹配。" : "审查记录已保存。源数据与融合结果未修改。");
        if (next && !changedDuringSave && nextId) { filterCases(false); await loadCase(nextId); }
        else if (next && !nextId) toast(isStream() && !isDirection() ? "已保存。这是当前筛选中的最后一个车辆日对象。" : "已保存。这是当前筛选中的最后一个候选审查对象。");
      }
    } catch (e) { txt("review-save-state", "保存失败 · 修改已保留"); error(e.status === 409 ? "该条审查已被其他窗口修改。你的笔记仍保留在此，请复制笔记后重新载入，合并后再保存。" : e.message); }
    finally { state.saving = false; $("save-review").disabled = false; $("save-next").disabled = false; }
  }
  // Mercator plane, geographic fit and drag/zoom. Every overlay uses the same transform.
  const mercator = (lat, lon) => { const a = lat * Math.PI / 180; return [(lon + 180) / 360, (1 - Math.log(Math.tan(a) + 1 / Math.cos(a)) / Math.PI) / 2]; };
  const inverse = (x, y) => [Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI, x * 360 - 180];
  const mapSize = () => [$("map-stage").clientWidth || 700, $("map-stage").clientHeight || 350];
  function project(lat, lon) { const [x, y] = mercator(lat, lon), [w, h] = mapSize(); return [(x - state.map.center[0]) * state.map.scale + w / 2, (y - state.map.center[1]) * state.map.scale + h / 2]; }
  function fitMap() {
    const points = [...visibleAVL().filter(validGPS), ...visibleAPC().filter(validCoords)];
    if (!points.length) { state.map.center = mercator(43.66, -79.39); state.map.scale = 256 * 2 ** 12; renderMap(); return; }
    const coords = points.map((p) => mercator(p.lat, p.lon)); const xs = coords.map((p) => p[0]), ys = coords.map((p) => p[1]);
    let minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys); const [w, h] = mapSize();
    state.map.center = [(minX + maxX) / 2, (minY + maxY) / 2]; state.map.scale = Math.min((w - 65) / Math.max(maxX - minX, 0.000025), (h - 75) / Math.max(maxY - minY, 0.000025), 256 * 2 ** 19); renderMap();
  }
  function mapLine(a, b, attrs = {}) { return svg("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], ...attrs }); }
  function drawBasemap(group, width, height) {
    const scale = state.map.scale, center = state.map.center;
    if (!state.basemap) {
      const spacing = 75; for (let x = ((-center[0] * scale + width / 2) % spacing + spacing) % spacing; x < width; x += spacing) group.append(svg("line", { x1: x, x2: x, y1: 0, y2: height, stroke: "#dfe8dc", "stroke-width": .7 }));
      for (let y = ((-center[1] * scale + height / 2) % spacing + spacing) % spacing; y < height; y += spacing) group.append(svg("line", { x1: 0, x2: width, y1: y, y2: y, stroke: "#dfe8dc", "stroke-width": .7 }));
      for (const x of [width * .2, width * .5, width * .8]) { const [, lon] = inverse(center[0] + (x - width / 2) / scale, center[1]); group.append(svg("text", { x, y: height - 11, "text-anchor": "middle", fill: "#a5b5a2", "font-size": 8 }, `${lon.toFixed(4)}°`)); }
      const [lat] = inverse(center[0], center[1]); group.append(svg("text", { x: width - 8, y: 18, "text-anchor": "end", fill: "#a5b5a2", "font-size": 8 }, `${lat.toFixed(4)}° N`)); return;
    }
    const z = Math.max(1, Math.min(19, Math.floor(Math.log2(scale / 256)))), n = 2 ** z, tileSize = scale / n;
    const originX = width / 2 - center[0] * scale, originY = height / 2 - center[1] * scale;
    const x0 = Math.floor(-originX / tileSize), x1 = Math.floor((width - originX) / tileSize), y0 = Math.max(0, Math.floor(-originY / tileSize)), y1 = Math.min(n - 1, Math.floor((height - originY) / tileSize));
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) { const wrappedX = ((x % n) + n) % n; group.append(svg("image", { x: originX + x * tileSize, y: originY + y * tileSize, width: tileSize + .4, height: tileSize + .4, href: `https://tile.openstreetmap.org/${z}/${wrappedX}/${y}.png`, preserveAspectRatio: "none", opacity: .83 })); }
  }
  function renderMap() {
    if (!state.detail || !state.map.center) return; const root = $("map-svg"), [w, h] = mapSize(); root.setAttribute("viewBox", `0 0 ${w} ${h}`); const frag = document.createDocumentFragment();
    const bg = svg("g"); drawBasemap(bg, w, h); frag.append(bg);
    if (isGTFS()) drawGTFSMap(frag);
    const data = visibleAVL(); let previous = null;
    for (const p of data) { if (!validGPS(p)) { previous = null; continue; } const xy = project(p.lat, p.lon); if (continuous(previous, p)) { const context = p.in_focus === false || previous.in_focus === false; frag.append(mapLine(project(previous.lat, previous.lon), xy, { stroke: COLORS.avl, "stroke-width": context ? 1.6 : 2.6, opacity: context ? .28 : .8, "stroke-linecap": "round" })); } previous = p;
      if ($("layer-pings").checked || isDirection() && sourceConflictIds("avl", p).length) { const c = svg("circle", { cx: xy[0], cy: xy[1], r: isDirection() && sourceConflictIds("avl", p).length ? 3.5 : 2.1, fill: sourceColor("avl", p), opacity: p.in_focus === false ? .5 : .8 }); c.append(svg("title", {}, `GPS ${p.rid} · ${time(p.t, true)}${isDirection() && sourceConflictIds("avl", p).length ? " · 方向冲突" : ""}`)); if (isStream()) { c.setAttribute("class", "svg-clickable"); c.setAttribute("data-rid", p.rid); c.addEventListener("click", (event) => { event.stopPropagation(); if (state.mapDragged) return; const row = arr(state.detail.observations).find((r) => r.source === "avl" && observationRid(r) === rid(p.rid)); if (row) selectObservation(row); }); } frag.append(c); }
    }
    for (const r of visibleAPC()) { if (!validCoords(r)) continue; const xy = project(r.lat, r.lon), chosen = rid(r.rid) === state.selected;
      if (chosen) frag.append(svg("circle", { cx: xy[0], cy: xy[1], r: 13, fill: COLORS.apc, opacity: .12, stroke: COLORS.apc, "stroke-width": 1 }));
      const circle = svg("circle", { cx: xy[0], cy: xy[1], r: chosen ? 6 : 4.1, fill: sourceColor("apc", r), stroke: "#fff", "stroke-width": chosen ? 2 : 1.2, opacity: r.in_focus === false && !chosen ? .45 : .95, class: `svg-clickable${chosen ? " selected" : ""}`, "data-selected": String(chosen), "data-rid": rid(r.rid), tabindex: 0, "aria-label": `APC ${r.rid} 站点 ${fmt(r.stop)} ${STATUS[rowStatus(r)]}` });
      circle.append(svg("title", {}, `APC ${r.rid} · 站点 ${fmt(r.stop)} · ${range(r.arrival, r.departure)} · ${STATUS[rowStatus(r)]}`));
      circle.addEventListener("click", (e) => { if (!state.mapDragged) { e.stopPropagation(); selectAPC(r.rid); } }); circle.addEventListener("keydown", (e) => { if (e.key === "Enter") selectAPC(r.rid); }); frag.append(circle);
    }
    const selected = selectedAPC(); if (selected && !isStream()) { drawMapWitnesses(frag, selected, arr(decision(selected).witnesses), false); if ($("layer-counters").checked) drawMapWitnesses(frag, selected, arr(decision(selected).counter_witnesses), true); }
    const rawSelected = isStream() ? selectedObservationRow() : null;
    if (rawSelected?.source === "avl" && validCoords(rawSelected) && (!state.view || (timestamp(rawSelected.event_time) >= state.view[0] && timestamp(rawSelected.event_time) <= state.view[1]))) { const xy = project(rawSelected.lat, rawSelected.lon), ring = svg("circle", { cx: xy[0], cy: xy[1], r: 10, fill: "none", stroke: "#506d8a", "stroke-width": 3, "data-source-rid": observationRid(rawSelected), "pointer-events": "none" }); ring.append(svg("title", {}, "选中 AVL 原始位置；未关联 APC")); frag.append(ring); }
    const nearest = nearestGPS(state.cursor); if (nearest) { const p = project(nearest.lat, nearest.lon); frag.append(svg("circle", { cx: p[0], cy: p[1], r: 7, fill: "#fff", stroke: "#254c3c", "stroke-width": 2, "pointer-events": "none" }), svg("circle", { cx: p[0], cy: p[1], r: 2.5, fill: "#254c3c", "pointer-events": "none" })); }
    if (!data.some(validGPS) && !visibleAPC().some(validCoords)) frag.append(svg("text", { x: w / 2, y: h / 2, class: "chart-empty" }, "此窗口无可用地理坐标"));
    root.replaceChildren(frag); const [lat] = inverse(...state.map.center), mpp = Math.cos(lat * Math.PI / 180) * 40075016.686 / state.map.scale; const raw = mpp * 65, order = 10 ** Math.floor(Math.log10(Math.max(raw, .001))), scaleM = [1, 2, 5, 10].map((n) => n * order).reduce((a, b) => Math.abs(b - raw) < Math.abs(a - raw) ? b : a);
    txt("map-scale", scaleM >= 1000 ? `${fmt(scaleM / 1000)} km` : `${fmt(scaleM)} m`); $("map-scale").style.width = `${Math.min(120, scaleM / mpp)}px`;
  }
  function drawMapWitnesses(group, r, witnesses, counter) {
    const points = new Map(arr(state.detail.avl).map((p) => [rid(p.rid), p])); const color = counter ? COLORS.counter : COLORS.avl;
    for (const witness of witnesses) {
      const anchors = arr(witness.ping_ids).map((id) => points.get(rid(id))).filter((p) => p && validGPS(p));
      if (witness.kind === "interpolation" && anchors.length >= 2) group.append(mapLine(project(anchors[0].lat, anchors[0].lon), project(anchors[anchors.length - 1].lat, anchors[anchors.length - 1].lon), { stroke: color, "stroke-width": 3, "stroke-dasharray": "5 4", opacity: .85, "pointer-events": "none" }));
      for (const p of anchors) { const xy = project(p.lat, p.lon); const mark = svg("circle", { cx: xy[0], cy: xy[1], r: 4.2, fill: "#fff", stroke: color, "stroke-width": 2, "pointer-events": "none" }); group.append(mark); }
      if (validCoords(witness)) {
        const xy = project(witness.lat, witness.lon); if (validCoords(r)) group.append(mapLine(project(r.lat, r.lon), xy, { stroke: color, "stroke-width": 1.2, "stroke-dasharray": "2 4", opacity: .65, "pointer-events": "none" }));
        if (witness.kind === "interpolation") group.append(svg("path", { d: `M ${xy[0]} ${xy[1] - 5} l 5 5 -5 5 -5 -5 Z`, fill: "#fff", stroke: color, "stroke-width": 1.8, "pointer-events": "none" }));
      }
    }
  }
  function zoom(factor, x = null, y = null) { if (!state.map.center) return; const [w, h] = mapSize(); x ??= w / 2; y ??= h / 2; const old = state.map.scale, scale = Math.max(512, Math.min(256 * 2 ** 22, old * factor)); state.map.center[0] += (x - w / 2) * (1 / old - 1 / scale); state.map.center[1] += (y - h / 2) * (1 / old - 1 / scale); state.map.scale = scale; renderMap(); }
  // Linked time-based views: gaps are explicit; APC progress exists only at retained witnesses.
  function sourceBounds() { let a = timestamp(state.detail?.window?.start), b = timestamp(state.detail?.window?.end); if (a == null || b == null) { const ts = [...arr(state.detail?.avl).filter((r) => r.in_window !== false).map((r) => timestamp(r.t)), ...arr(state.detail?.apc).flatMap((r) => [timestamp(r.arrival), timestamp(r.departure)])].filter(finite); a = ts.length ? Math.min(...ts) : 0; b = ts.length ? Math.max(...ts) : 1; } return [a, Math.max(b, a + 1)]; }
  function bounds() { return state.view || sourceBounds(); }
  function syncWindowSize() {
    const size = state.view ? state.view[1] - state.view[0] : null, preset = size != null && [60, 120, 300, 600, 900, 1800, 3600, 7200].find((value) => Math.abs(value - size) < .001);
    $("window-size").value = size == null ? "full" : preset ? String(preset) : "custom"; $("window-custom-controls").hidden = $("window-size").value !== "custom";
    if (size != null && !preset) { $("window-custom-unit").value = "1"; $("window-custom-value").value = String(Math.round(size * 1000) / 1000); }
  }
  function renderWindowControls() { const [a, b] = bounds(), [start, end] = sourceBounds(); syncWindowSize(); txt("window-label", `${date(a)} ${time(a, true)}–${time(b, true)}${date(a) !== date(b) ? " · 跨日" : ""}${state.view ? " · 局部浏览" : isStream() && !isDirection() ? " · 当日全窗" : " · 加载全窗"}`); $("window-previous").disabled = !state.view || a <= start; $("window-next").disabled = !state.view || b >= end; $("window-focus").disabled = !selectedAPC(); renderTimelinePan(); }
  function renderTimelinePan() {
    if (!state.detail) return;
    const [start, end] = sourceBounds(), [a, b] = bounds(), size = b - a, movable = !!state.view && end - start > size + .001, control = $("timeline-pan"), title = isStream() && !isDirection() ? "横向浏览全天" : "横向浏览加载范围";
    txt("timeline-pan-title", title); control.setAttribute("aria-label", title); control.disabled = !movable;
    // Keep short windows reachable even across a full day: slider keyboard steps
    // must not leap over more than one tenth of the currently visible duration.
    const fractionStep = movable ? Math.min(1, 1000 * size / 10 / (end - start - size)) : 1;
    control.step = String(Math.max(.000001, 10 ** Math.floor(Math.log10(fractionStep))));
    control.value = movable ? (a - start) / (end - start - size) * 1000 : 0;
    for (const [key, value] of Object.entries({ viewStart: a, viewEnd: b, domainStart: start, domainEnd: end, windowSeconds: size })) control.dataset[key] = String(value);
    control.setAttribute("aria-valuetext", `${eventTimeText(a)} 至 ${eventTimeText(b)}${movable ? "，左右移动可见窗" : "，已显示完整范围"}`);
    txt("timeline-pan-label", `${date(start)} · 当前 ${time(a, true)}–${time(b, true)}${date(a) !== date(b) ? "（次日）" : ""} · ${movable ? `${fmt(size / 60)} 分钟窗口` : isStream() && !isDirection() ? "已显示全天总览" : "已显示完整加载范围"}`);
    $("timeline-pan-detail").hidden = movable || end - start <= 60; txt("timeline-pan-detail", `切换 ${end - start > 1800 ? "30" : end - start > 600 ? "10" : "1"} 分钟详细窗`);
    txt("timeline-pan-help", `${movable ? "左右拖动或使用 Home / End / 方向键移动可见时间窗，游标保持窗内相对位置；地图、曲线和“仅当前时间窗”表格同步。" : "当前已显示整个范围；选择详细窗后可左右拖动浏览。"} 绿色为 AVL 来源时刻，灰蓝为 APC 到离站区间，密集重叠不表示连续覆盖；下方滑条只控制时间游标。`);
    const root = $("timeline-pan-overview"), width = root.parentElement.clientWidth || 700, height = 83, left = 39, right = 14, x = (t) => left + (t - start) / (end - start) * (width - left - right), g = document.createDocumentFragment();
    root.setAttribute("viewBox", `0 0 ${width} ${height}`); root.dataset.viewStart = String(a); root.dataset.viewEnd = String(b);
    root.setAttribute("aria-label", `${title}：绿色 AVL 时刻、灰蓝 APC 区间；框选当前 ${range(a, b)}`);
    for (let i = 0; i <= 4; i++) { const t = start + (end - start) * i / 4, xx = x(t); g.append(svg("line", { x1: xx, x2: xx, y1: 8, y2: 57, class: "chart-grid" }), svg("text", { x: xx, y: 74, class: "chart-axis", "text-anchor": i === 4 ? "end" : i === 0 ? "start" : "middle" }, i === 4 && date(t) !== date(start) ? `${time(t)} +1日` : time(t))); }
    g.append(svg("text", { x: left - 7, y: 26, class: "chart-label", "text-anchor": "end" }, "AVL"), svg("text", { x: left - 7, y: 47, class: "chart-label", "text-anchor": "end" }, "APC"));
    let avlPath = "", apcPath = "", avlCount = 0, apcCount = 0;
    for (const row of arr(state.detail.avl)) { const t = timestamp(row.t); if (!finite(t) || t < start || t > end || row.in_window === false) continue; const xx = x(t).toFixed(2); avlPath += `M${xx},17v12`; avlCount++; }
    for (const row of arr(state.detail.apc)) { const arrival = timestamp(row.arrival), departure = timestamp(row.departure); if (arrival == null && departure == null) continue; const lo = Math.min(arrival ?? departure, departure ?? arrival), hi = Math.max(arrival ?? departure, departure ?? arrival); if (hi < start || lo > end) continue; const xx = x(Math.max(start, lo)), finish = x(Math.min(end, hi)); apcPath += `M${xx.toFixed(2)},44H${Math.max(xx + .8, finish).toFixed(2)}`; apcCount++; }
    g.append(svg("path", { d: avlPath, stroke: COLORS.avl, "stroke-width": 1, opacity: .5, "data-source": "avl", "data-observations": avlCount }), svg("path", { d: apcPath, stroke: COLORS.not_evaluated, "stroke-width": 5, opacity: .65, "data-source": "apc", "data-observations": apcCount }));
    g.append(svg("rect", { id: "timeline-pan-window", x: x(a), y: 8, width: Math.max(1, x(b) - x(a)), height: 48, rx: 3, fill: "#447e8d", "fill-opacity": .1, stroke: "#447e8d", "stroke-width": 1.5, "data-view-start": a, "data-view-end": b })); root.replaceChildren(g);
  }
  function panWindow(value) {
    if (!state.detail || !state.view) return;
    const [start, end] = sourceBounds(), [a, b] = bounds(), size = b - a, distance = Math.max(0, end - start - size), at = start + Math.max(0, Math.min(1000, value)) / 1000 * distance;
    if (!distance) return; const cursorOffset = Math.max(0, Math.min(size, (state.cursor ?? a) - a)); state.view = [at, at + size]; state.cursor = at + cursorOffset; refreshWindow();
  }
  function refreshWindow() { const visible = visibleAPC(); if (!isStream() && !visible.some((r) => rid(r.rid) === state.selected)) { const first = visible.find((r) => r.in_focus !== false) || visible[0]; state.selected = first ? rid(first.rid) : null; } renderWindowControls(); renderInspector(); renderTable(); if (isStream()) renderObservations(); fitMap(); renderCharts(); }
  function setWindowSize() { if (!state.detail) return; const value = $("window-size").value; $("window-custom-controls").hidden = value !== "custom"; if (value === "custom") { $("window-custom-value").focus(); return; } applyWindowSize(value === "full" ? null : Number(value)); }
  function applyWindowSize(seconds) { if (!state.detail) return; const [start, end] = sourceBounds(); if (seconds == null) state.view = null; else { if (!Number.isFinite(seconds) || seconds < .001) { toast("时间窗长度必须是正数，可用秒或分钟。"); return; } const size = Math.min(seconds, end - start), at = Math.max(start, Math.min(state.cursor ?? start, end - size)); state.view = size >= end - start ? null : [at, at + size]; } refreshWindow(); }
  function shiftWindow(direction) { if (!state.view) return; const [start, end] = sourceBounds(), size = state.view[1] - state.view[0], at = Math.max(start, Math.min(end - size, state.view[0] + direction * size)); state.view = [at, Math.min(end, at + size)]; state.cursor = at; refreshWindow(); }
  function focusSelected() { const r = selectedAPC(); if (!r) return; const t = timestamp(r.arrival) ?? timestamp(r.departure); if (!finite(t)) return; const [start, end] = sourceBounds(); state.view = [Math.max(start, t - 300), Math.min(end, t + 300)]; if (state.view[1] <= state.view[0]) state.view = null; $("window-size").value = "600"; state.cursor = t; refreshWindow(); }
  function dimensions(root, height) { const width = root.parentElement.clientWidth || 700; root.setAttribute("viewBox", `0 0 ${width} ${height}`); return { width, height, left: width < 500 ? 44 : 64, right: 23, top: 18, bottom: 30 }; }
  function axes(g, dim, a, b) { const x = (t) => dim.left + (t - a) / (b - a) * (dim.width - dim.left - dim.right); const ticks = dim.width < 500 ? 4 : 7; for (let i = 0; i < ticks; i++) { const t = a + (b - a) * i / (ticks - 1), xx = x(t); g.append(svg("line", { x1: xx, x2: xx, y1: dim.top, y2: dim.height - dim.bottom, class: "chart-grid" }), svg("text", { x: xx, y: dim.height - 11, class: "chart-axis", "text-anchor": "middle" }, time(t, b - a < 600))); } return x; }
  function nearestGPS(t) { if (!finite(t)) return null; let best = null, distance = Infinity; for (const p of rowsAVL()) if (validGPS(p)) { const d = Math.abs(timestamp(p.t) - t); if (d < distance) { best = p; distance = d; } } return best; }
  function updateCursor() { const [a, b] = bounds(); state.cursor = Math.min(b, Math.max(a, state.cursor ?? a)); $("time-scrubber").value = Math.round((state.cursor - a) / (b - a) * 1000); txt("scrubber-time", time(state.cursor, true)); const nearest = nearestGPS(state.cursor); txt("cursor-label", nearest ? `最近实测 GPS ${time(nearest.t, true)} · 相差 ${fmt(Math.abs(timestamp(nearest.t) - state.cursor))} s` : "此窗口无实际 GPS，不补出车辆位置"); }
  function drawCursor(g, dim, x) { if (!finite(state.cursor)) return; g.append(svg("line", { x1: x(state.cursor), x2: x(state.cursor), y1: 9, y2: dim.height - dim.bottom, class: "chart-cursor", "pointer-events": "none" })); }
  function chartClick(root, event) { if (!state.detail) return; const [a, b] = bounds(), r = root.getBoundingClientRect(), left = r.width < 500 ? 44 : 64; state.cursor = a + Math.max(0, Math.min(1, (event.clientX - r.left - left) / (r.width - left - 23))) * (b - a); renderCharts(); renderMap(); }
  function renderCharts() { if (!state.detail) return; const [a, b] = bounds(); for (const id of ["timeline-svg", "progress-svg", "map-svg", "gtfs-schedule-svg", "gtfs-progress-svg"]) { $(id).dataset.viewStart = String(a); $(id).dataset.viewEnd = String(b); } updateCursor(); renderTimeline(); renderProgress(); renderBoundaryDiff(); if (isGTFS()) { renderGTFSSchedule(); renderGTFSProgress(); for (const id of ["timeline-svg", "progress-svg", "gtfs-schedule-svg", "gtfs-progress-svg"]) decorateDecisionSelection($(id)); } }
  function renderTimeline() {
    const root = $("timeline-svg"), d = dimensions(root, 153), [a, b] = bounds(), g = document.createDocumentFragment(), x = axes(g, d, a, b);
    drawDirectionConflicts(g, d, x, a, b);
    g.append(svg("text", { x: d.left - 10, y: 37, class: "chart-label", "text-anchor": "end" }, "AVL"), svg("text", { x: d.left - 10, y: 86, class: "chart-label", "text-anchor": "end" }, "APC"));
    const avl = visibleAVL(); let prev = null;
    for (const p of avl) { const t = timestamp(p.t); if (!finite(t)) continue; if (!validGPS(p)) { g.append(svg("line", { x1: x(t), x2: x(t), y1: 25, y2: 43, stroke: "#dba89b", "stroke-width": 1.5 })); prev = null; continue; }
      if (continuous(prev, p)) g.append(svg("line", { x1: x(timestamp(prev.t)), x2: x(t), y1: 34, y2: 34, stroke: COLORS.avl, "stroke-width": 7, opacity: p.in_focus === false ? .22 : .65, "data-source": "avl", "data-from-rid": prev.rid, "data-to-rid": p.rid }));
      if (prev && p.break_before && t > timestamp(prev.t)) g.append(svg("rect", { x: x(timestamp(prev.t)), y: 22, width: Math.max(1, x(t) - x(timestamp(prev.t))), height: 24, class: "chart-gap" }));
      const tick = svg("line", { x1: x(t), x2: x(t), y1: 30, y2: 38, stroke: sourceColor("avl", p), "stroke-width": 1, opacity: .65, "data-source": "avl", "data-rid": p.rid }); tick.append(svg("title", {}, `AVL ${p.rid} · ${eventTimeText(p.t)} · 原始定位时刻`)); if (isGTFS()) gtfsMarkSource(tick, "avl", p); g.append(tick); prev = p;
    }
    let lanes = Array(5).fill(-Infinity); const events = visibleAPC().slice().sort((p, q) => (timestamp(p.arrival) ?? Infinity) - (timestamp(q.arrival) ?? Infinity));
    for (const r of events) { let start = timestamp(r.arrival), end = timestamp(r.departure); if (start == null && end == null) continue; start ??= end; end ??= start; const lo = Math.min(start, end), hi = Math.max(start, end); if (hi < a || lo > b) continue;
      let lane = lanes.findIndex((last) => last < lo); if (lane < 0) lane = lanes.indexOf(Math.min(...lanes)); lanes[lane] = hi; const selected = rid(r.rid) === state.selected, y = 61 + lane * 8;
      const bar = svg("rect", { x: x(Math.max(a, lo)) - (lo === hi ? 2 : 0), y, width: Math.max(4, x(Math.min(b, hi)) - x(Math.max(a, lo))), height: 6, rx: 2, fill: sourceColor("apc", r), opacity: selected ? 1 : r.in_focus === false ? .3 : .58, stroke: selected ? "#3d5d48" : "none", "stroke-width": selected ? 1.5 : 0, class: `svg-clickable${selected ? " selected" : ""}`, "data-selected": String(selected), "data-rid": rid(r.rid), "data-source": "apc" });
      bar.append(svg("title", {}, `APC ${r.rid} · 站点 ${fmt(r.stop)} · ${range(r.arrival, r.departure)}`)); bar.addEventListener("click", (e) => { e.stopPropagation(); selectAPC(r.rid); }); g.append(bar);
    }
    const chosen = selectedAPC(); if (chosen && !isStream()) {
      for (const [list, counter] of [[arr(decision(chosen).witnesses), false], [$("layer-counters").checked ? arr(decision(chosen).counter_witnesses) : [], true]]) for (const witness of list) {
        const t = timestamp(witness.support_time); if (!finite(t) || t < a || t > b) continue; const xx = x(t), color = counter ? COLORS.counter : COLORS.avl;
        g.append(svg("line", { x1: xx, x2: xx, y1: 42, y2: 107, stroke: color, "stroke-dasharray": "2 3", opacity: .6 }), svg("path", { d: `M ${xx} 107 l 4 4 -4 4 -4 -4 Z`, fill: witness.kind === "interpolation" ? "white" : color, stroke: color, "stroke-width": 1.3 }));
      }
    }
    drawCursor(g, d, x); root.replaceChildren(g);
  }
  function renderProgress() {
    const root = $("progress-svg"), d = dimensions(root, 211), [a, b] = bounds(), g = document.createDocumentFragment(), x = axes(g, d, a, b);
    drawDirectionConflicts(g, d, x, a, b);
    const points = visibleAVL(), distances = points.filter((p) => validGPS(p) && finite(p.progress_m)).map((p) => p.progress_m);
    const lo = distances.length ? Math.min(...distances) : 0, hi = distances.length ? Math.max(...distances) : 1, span = Math.max(hi - lo, 100);
    const y = (m) => d.height - d.bottom - (m - lo) / span * (d.height - d.top - d.bottom - 12);
    for (let i = 0; i < 4; i++) { const m = lo + span * i / 3, yy = y(m); g.append(svg("line", { x1: d.left, x2: d.width - d.right, y1: yy, y2: yy, class: "chart-grid" }), svg("text", { x: d.left - 9, y: yy + 3, class: "chart-axis", "text-anchor": "end" }, `${(m / 1000).toFixed(1)}`)); }
    g.append(svg("text", { x: d.left - 9, y: 11, class: "chart-label", "text-anchor": "end" }, "km"));
    let prev = null; for (const p of points) { if (!validGPS(p) || !finite(p.progress_m)) { prev = null; continue; } if (continuous(prev, p)) g.append(mapLine([x(timestamp(prev.t)), y(prev.progress_m)], [x(timestamp(p.t)), y(p.progress_m)], { stroke: COLORS.avl, "stroke-width": 2.2, opacity: p.in_focus === false ? .28 : .85, "data-source": "avl", "data-from-rid": prev.rid, "data-to-rid": p.rid })); if (prev && !continuous(prev, p)) g.append(svg("rect", { x: x(timestamp(prev.t)), y: d.top, width: Math.max(1, x(timestamp(p.t)) - x(timestamp(prev.t))), height: d.height - d.bottom - d.top, class: "chart-gap" })); prev = p; }
    const pointMap = new Map(arr(state.detail.avl).map((p) => [rid(p.rid), p]));
    for (const r of isStream() ? [] : visibleAPC()) for (const witness of arr(decision(r).witnesses)) {
      const anchors = arr(witness.ping_ids).map((id) => pointMap.get(rid(id))).filter((p) => p && validGPS(p) && finite(p.progress_m)).sort((p, q) => timestamp(p.t) - timestamp(q.t)); const t = timestamp(witness.support_time); if (!anchors.length || !finite(t) || t < a || t > b) continue;
      let distance = anchors[0].progress_m;
      if (witness.kind === "interpolation") { if (anchors.length < 2) continue; const first = anchors[0], last = anchors[anchors.length - 1], interval = timestamp(last.t) - timestamp(first.t); if (first.progress_run == null || first.progress_run !== last.progress_run || interval <= 0 || t < timestamp(first.t) || t > timestamp(last.t)) continue; distance += (last.progress_m - distance) * (t - timestamp(first.t)) / interval; }
      const xx = x(t), yy = y(distance), chosen = rid(r.rid) === state.selected, size = chosen ? 5 : 3, color = COLORS[rowStatus(r)] || COLORS.apc;
      const mark = witness.kind === "interpolation" ? svg("path", { d: `M ${xx} ${yy - size} l ${size} ${size} -${size} ${size} -${size} -${size} Z`, fill: "white", stroke: color, "stroke-width": chosen ? 2 : 1.2 }) : svg("circle", { cx: xx, cy: yy, r: size, fill: color, stroke: "white", "stroke-width": 1 });
      mark.setAttribute("class", `svg-clickable${chosen ? " selected" : ""}`); mark.setAttribute("data-selected", String(chosen)); mark.setAttribute("data-rid", rid(r.rid)); mark.setAttribute("opacity", chosen ? 1 : .55); mark.append(svg("title", {}, `APC ${r.rid} · ${witness.kind === "interpolation" ? "推导插值距离" : "真实 GPS 支持位置"} · ${fmt(distance)} m`)); mark.addEventListener("click", (e) => { e.stopPropagation(); selectAPC(r.rid); }); g.append(mark);
    }
    if (!distances.length) g.append(svg("text", { x: d.width / 2, y: 100, class: "chart-empty" }, "此窗口无可用实测累计距离 · 不外推 APC 进度"));
    drawCursor(g, d, x); root.replaceChildren(g);
  }
  // GTFS coordinates come only from the persisted reference / evidence adapter.
  // Never derive APC s from a nearby AVL ping or connect different fragments.
  const GTFS_COLORS = ["#426aab", "#aa6cbd", "#cc8744"];
  const GTFS_DIAGNOSTIC_COLORS = ["#b47435", "#a45569", "#83744a"];
  const GTFS_STATUS = { MATCHED: "MATCHED · 算法择定", AMBIGUOUS: "AMBIGUOUS · 多候选", PATTERN_ONLY: "PATTERN_ONLY · 仅线路模式", UNMATCHED: "UNMATCHED · 无可接受候选", BOUNDARY_UNCERTAIN: "BOUNDARY_UNCERTAIN · 边界待定" };
  const gtfsData = () => state.detail?.gtfs || {};
  function decorateDecisionSelection(root) {
    root.dataset.matchingMode = state.matchingMode;
    if (state.matchingMode === "global") return;
    const selection = gtfsData().decision_selection || {}, selectedAVL = state.gtfsDecisionAVL || new Set(), selectedAPC = state.gtfsDecisionAPC || new Set();
    const [a, b] = bounds(), box = root.viewBox.baseVal, left = box.width < 500 ? 44 : 64, right = box.width - 23, x = (t) => left + (t - a) / (b - a) * (right - left), bands = svg("g", { class: "decision-selection-bands", "pointer-events": "none", "data-matching-mode": state.matchingMode });
    const clipId = `${root.id}-decision-apc-clip`, defs = svg("defs"), clip = svg("clipPath", { id: clipId });
    for (const interval of arr(selection.time_ranges)) { const start = Math.max(a, timestamp(interval.start) ?? b), end = Math.min(b, timestamp(interval.end) ?? a); if (end >= start) clip.append(svg("rect", { x: x(start), y: 0, width: Math.max(.7, x(end) - x(start)), height: box.height })); }
    defs.append(clip); root.prepend(defs);
    for (const mark of root.querySelectorAll("[data-source]")) {
      const ids = mark.dataset.source === "avl" ? selectedAVL : selectedAPC, key = `${mark.dataset.source}|${mark.dataset.rid || mark.dataset.toRid || ""}`, included = ids.has(key) && (!mark.dataset.fromRid || ids.has(`${mark.dataset.source}|${mark.dataset.fromRid}`));
      const originalOpacity = Number(mark.getAttribute("opacity") || 1); mark.dataset.selectedForDecision = String(included);
      if (mark.dataset.source === "apc" && included) {
        const selected = mark.cloneNode(true); selected.setAttribute("clip-path", `url(#${clipId})`); selected.dataset.selectionClipped = "true";
        selected.addEventListener("click", (event) => { event.stopPropagation(); const row = findSourceObservation("apc", selected.dataset.rid); if (row) selectObservation(row); });
        mark.after(selected); mark.dataset.selectedForDecision = "false"; mark.setAttribute("opacity", String(originalOpacity * .18));
      } else if (!included) mark.setAttribute("opacity", String(originalOpacity * .18));
    }
    for (const interval of arr(selection.time_ranges)) {
      const start = Math.max(a, timestamp(interval.start) ?? b), end = Math.min(b, timestamp(interval.end) ?? a); if (end < start) continue;
      bands.append(svg("rect", { x: x(start), y: 18, width: Math.max(1, x(end) - x(start)), height: Math.max(1, box.height - 48), fill: "#138879", opacity: .065, "data-kind": "decision-selection", "data-start": interval.start, "data-end": interval.end }), svg("line", { x1: x(start), x2: x(end), y1: 20, y2: 20, stroke: "#138879", "stroke-width": 3 }));
    }
    const label = svg("text", { x: right, y: 12, "text-anchor": "end", fill: "#267e6d", "font-size": 9 }, `${MATCHING_MODES[state.matchingMode]} · 深色为决策证据，浅色仅上下文`); bands.append(label); root.append(bands);
  }
  const gtfsCandidates = () => arr(gtfsData().candidates);
  const gtfsDiagnosticCandidates = () => arr(gtfsData().diagnostic_candidates);
  const gtfsAllCandidates = () => [...gtfsCandidates(), ...gtfsDiagnosticCandidates()];
  const gtfsCandidateId = (c, i = 0) => rid(c.candidate_id ?? c.id ?? c.trip_id ?? i);
  const gtfsReferenceId = (r) => rid(r.reference_id ?? r.id);
  const gtfsReferences = () => Array.isArray(gtfsData().references) ? gtfsData().references : Object.entries(gtfsData().references || {}).map(([id, r]) => ({ id, ...r }));
  const gtfsProjection = () => { const projection = gtfsData().projection, reference = projection?.by_reference?.[state.gtfsReference]; return Array.isArray(reference) ? reference : Array.isArray(reference?.points) ? reference.points : Array.isArray(projection) ? projection : arr(projection?.points); };
  const gtfsAPCEvidence = () => Array.isArray(gtfsData().apc_evidence) ? gtfsData().apc_evidence : arr(gtfsData().apc_evidence?.intervals || gtfsData().apc_evidence?.points);
  const gtfsWindows = () => Array.isArray(gtfsData().windows) ? gtfsData().windows : arr(gtfsData().windows?.windows);
  const gtfsS = (row) => finite(row.s) ? row.s : finite(row.s_m) ? row.s_m : null;
  const gtfsTrajectory = (c) => arr(c.trajectory || c.trajectories || c.schedule_trajectory);
  const gtfsColor = (c) => state.gtfsColors?.get(gtfsCandidateId(c)) || GTFS_COLORS[0];
  const gtfsVisibleCandidates = () => gtfsAllCandidates().filter((c) => state.gtfsVisible?.has(gtfsCandidateId(c)));
  const gtfsCandidateLabel = (c) => c.diagnostic_only ? `诊断 ${c.diagnostic_rank || gtfsDiagnosticCandidates().indexOf(c) + 1}` : `候选 ${gtfsCandidates().indexOf(c) + 1}`;
  const gtfsPrecise = (value) => finite(value) ? value.toLocaleString("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 6 }) : "未知";
  const gtfsGateValue = (value, unit) => value == null ? "未知" : Array.isArray(value) ? `[${value.map((item) => gtfsGateValue(item, unit)).join(", ")}]` : unit === "fraction" ? finite(value) ? `${gtfsPrecise(value * 100)}%` : "未知" : typeof value === "number" ? gtfsPrecise(value) : String(value);
  const gtfsValid = (row) => ["VALID", "ON_REFERENCE", "VALID_PROJECTION", "STOP_INTERVAL_ONLY", "VALID_STOP", "STOP_MATCHED"].includes(String(row.status || row.projection_status || "").toUpperCase());
  const gtfsOnReference = (row) => rid(row.reference_id) === state.gtfsReference && row.ambiguous !== true && gtfsValid(row) && gtfsS(row) != null;
  function drawGTFSMap(group) {
    const reference = gtfsReferences().find((r) => gtfsReferenceId(r) === state.gtfsReference); let previous = null;
    for (const p of arr(reference?.geometry)) { if (!validCoords(p)) { previous = null; continue; } if (previous) group.append(mapLine(project(previous.lat, previous.lon), project(p.lat, p.lon), { stroke: "#627aa6", "stroke-width": 2, "stroke-dasharray": "6 4", opacity: .5, "data-reference-id": state.gtfsReference })); previous = p; }
    const [a, b] = bounds();
    for (const p of gtfsProjection()) { if (String(p.status).toUpperCase() !== "OFF_REFERENCE" || timestamp(p.t) < a || timestamp(p.t) > b || rid(p.reference_id) !== state.gtfsReference) continue; const raw = validCoords(p) ? p : findSourceObservation("avl", p.rid || p.source_rid); if (!raw || !validCoords(raw)) continue; const xy = project(raw.lat, raw.lon), mark = svg("circle", { cx: xy[0], cy: xy[1], r: 5, fill: "none", stroke: "#d18b4f", "stroke-width": 2, "data-status": "OFF_REFERENCE", "data-reference-id": p.reference_id }); mark.append(svg("title", {}, `OFF_REFERENCE · ${p.rid} · ${fmt(p.projection_distance_m)} m · 仅诊断，不进 s–t 曲线`)); gtfsMarkSource(mark, "avl", p); group.append(mark); }
  }
  function appendGTFSContinuousEvidence(card, candidate) {
    const components = [
      ["absolute_delay_level_cost", "绝对延误水平"],
      ["conditioned_fit_cost", "共享延误条件下的拟合"],
      ["delay_dynamics_cost", "延误动态变化"],
      ["APC_stop_time_cost", "APC 停站时间一致性"],
      ["projection_and_coverage_cost", "投影与覆盖"],
      ["pattern_progress_cost", "线路模式与进度"],
      ["vehicle_sequence_cost", "车辆全天序列"],
    ];
    if (candidate.occurrence_model_enabled) components.push(["occurrence_displacement_cost", "相邻班次位移"]);
    card.dataset.scorer = candidate.scorer;
    card.append(node("p", "gtfs-continuous-method", "连续计划通过时刻评分；不使用重采样 GTFS 的 DTW 距离"));
    const scroll = node("div", "gtfs-continuous-table-scroll"), table = node("table", "gtfs-score-components"), head = node("thead"), header = node("tr"), body = node("tbody");
    table.setAttribute("aria-label", candidate.occurrence_model_enabled ? "连续时空评分八项组成" : "连续时空评分七项组成");
    for (const label of ["评分组成", "原始成本", "权重", "加权贡献"]) header.append(node("th", "", label)); head.append(header); table.append(head);
    for (const [key, label] of components) {
      const row = node("tr"); row.dataset.component = key;
      row.append(node("th", "", label));
      for (const value of [candidate.score_components_unweighted?.[key], candidate.score_component_weights?.[key], candidate.score_components?.[key]]) { const cell = node("td", "mono", gtfsPrecise(value)); cell.title = finite(value) ? String(value) : "未知"; row.append(cell); }
      body.append(row);
    }
    table.append(body); scroll.append(table); card.append(scroll);
    const coverageScroll = node("div", "gtfs-continuous-table-scroll"), coverageTable = node("table", "gtfs-continuous-coverage"), coverageHead = node("thead"), coverageHeader = node("tr"), coverageBody = node("tbody");
    coverageTable.setAttribute("aria-label", "三层证据覆盖率");
    for (const label of ["证据覆盖层级", "观测点比例", "相邻区间时长比例"]) coverageHeader.append(node("th", "", label)); coverageHead.append(coverageHeader); coverageTable.append(coverageHead);
    for (const [key, label, definition] of [["projection", "投影覆盖率", "有效投影证据 / 全部空间证据"], ["schedule", "计划对齐覆盖率", "计划支持的有效证据 / 有效投影证据"], ["end_to_end", "端到端证据覆盖率", "计划支持的有效证据 / 全部空间证据"]]) {
      const row = node("tr"); row.dataset.coverage = key; row.title = definition; row.append(node("th", "", label));
      for (const metric of ["point_ratio", "duration_ratio"]) { const value = candidate.coverage_metrics?.[key]?.[metric]; const cell = node("td", "mono", finite(value) ? `${fmt(value * 100)}%` : "未知"); cell.dataset.metric = metric; cell.title = finite(value) ? String(value) : "未知"; row.append(cell); }
      coverageBody.append(row);
    }
    coverageTable.append(coverageBody); coverageScroll.append(coverageTable); card.append(coverageScroll);
    card.append(node("p", "gtfs-continuous-note", "覆盖率不是准确率；相邻区间按真实时间和片段边界计算，不跨持续 OFF_REFERENCE 或 GPS 缺口补线。"));
    const delay = node("dl", "gtfs-continuous-delay"), prior = ({ independently_validated_history: "独立验证的历史延误分布", local_scheduled_headway: "本地计划发车间隔（未校准先验）", uncalibrated_operational_defaults: "非对称运营延误混合先验（未校准）", independently_validated_operational_hierarchy: "独立验证的分层运营延误先验" })[candidate.delay_prior_source] || "未提供";
    const process = arr(candidate.whole_alignment?.delay_process), headways = process.map((point) => point.headway_s).filter((value) => finite(value) && value > 0), stored = candidate.headway_summary_s;
    const low = stored?.min ?? (headways.length ? headways.reduce((a, b) => Math.min(a, b)) : null), high = stored?.max ?? (headways.length ? headways.reduce((a, b) => Math.max(a, b)) : null);
    for (const [label, value] of [["共享延误中位数", finite(candidate.estimated_delay_s) ? `${fmt(candidate.estimated_delay_s)} s` : "未知"], ["延误不确定性尺度（非校准置信区间）", finite(candidate.delay_uncertainty_s) ? `${fmt(candidate.delay_uncertainty_s)} s` : "未知"], ["延误先验来源", prior], ["局部计划发车间隔范围", finite(low) && finite(high) ? `${fmt(low)}–${fmt(high)} s` : "未知"], ["APC 支持停站数", fmt(candidate.apc_support?.supported_stops)], ["连续支持窗口数", fmt(candidate.continuous_support_windows)]]) { delay.append(node("dt", "", label), node("dd", "", value)); }
    card.append(delay, node("p", "gtfs-continuous-note", candidate.occurrence_model_enabled ? "运营延误先验、相邻班次位移、候选检索分别计算；进入 rescue 集不代表同等先验概率。" : "下方保留原始计划时刻；延误先验按发车间隔或独立历史归一，不按每小时固定惩罚解释。"));
    if (candidate.occurrence_model_enabled) {
      const evidence = candidate.occurrence_displacement || {}, context = evidence.context || {}, details = node("dl", "gtfs-occurrence-evidence");
      for (const [label, value] of [["运营先验模型", candidate.delay_prior_source || "未知"], ["局部班次间隔来源", context.headway_source || "未知"], ["局部班次间隔可信度", gtfsPrecise(context.headway_confidence)], ["前一班通过间隔 / 秒", gtfsPrecise(context.previous_headway_s)], ["后一班通过间隔 / 秒", gtfsPrecise(context.next_headway_s)], ["跨越的竞争班次数", gtfsPrecise(evidence.crossed_occurrences)], ["超出班次时间单元 / 秒", gtfsPrecise(evidence.outside_cell_s)]]) details.append(node("dt", "", label), node("dd", "mono", value));
      const guard = candidate.occurrence_decision_guard;
      if (guard) details.append(node("dt", "", "班次身份保护诊断"), node("dd", "mono", JSON.stringify(guard)));
      card.append(details);
    }
  }
  function initGTFS() {
    state.gtfsDecisionAVL = new Set(arr(gtfsData().decision_selection?.selected_avl_ids)); state.gtfsDecisionAPC = new Set(arr(gtfsData().decision_selection?.selected_apc_ids));
    $("direction-candidate-note").append(document.createTextNode(state.detail.case?.gap_split ? " 当前仅展示 Route 29；在父方向候选上按 APC + AVL 联合观测空档进一步分段，当前子片段边界不跨该空档。方向标签仍继承父实验，可能受父实验其它线路（包括 929）方向证据影响，并非重新独立推断方向。原 candidate_segment_ids 是父标注；review_candidate_ids 才是当前子片段审查归属。" : " 当前仅展示 Route 29；方向边界沿用父实验，可能仍受父实验其它线路（包括 929）证据影响，不能把它称为重新按 29 独立分割。"));
    state.gtfsVisible = new Set((gtfsCandidates().length ? gtfsCandidates() : gtfsDiagnosticCandidates()).map(gtfsCandidateId));
    state.gtfsColors = new Map([...gtfsCandidates().map((c, i) => [gtfsCandidateId(c), GTFS_COLORS[i % GTFS_COLORS.length]]), ...gtfsDiagnosticCandidates().map((c, i) => [gtfsCandidateId(c), GTFS_DIAGNOSTIC_COLORS[i % GTFS_DIAGNOSTIC_COLORS.length]])]);
    // Choose from this case's stored primary projection, never the preceding case's selected axis.
    const references = gtfsReferences(), projection = gtfsData().projection, primaryPoints = Array.isArray(projection) ? projection : arr(projection?.points), preferred = rid(gtfsCandidates()[0]?.reference_id || gtfsDiagnosticCandidates()[0]?.reference_id || primaryPoints.find((p) => gtfsValid(p))?.reference_id); state.gtfsReference = references.some((r) => gtfsReferenceId(r) === preferred) ? preferred : gtfsReferenceId(references[0] || {});
    renderGTFSPanel(); renderMatchingControls();
  }
  function renderGTFSPanel() {
    if (!isGTFS()) return; const data = gtfsData(), result = data.result || {}, candidates = gtfsCandidates(), status = result.status || result.decision || "UNKNOWN", continuous = result.scorer === "continuous_passage_v1";
    txt("gtfs-result-status", GTFS_STATUS[status] || `${status} · 状态未声明`); $("gtfs-result-status").dataset.status = status; $("gtfs-result-status").className = `status-badge gtfs-status-${rid(status).toLowerCase()}`;
    txt("gtfs-candidate-count", `${candidates.length} 个最终候选`); $("gtfs-candidate-panel").dataset.candidateCount = String(candidates.length);
    txt("gtfs-result-summary", `${MATCHING_MODES[state.matchingMode]} · 参考日期 2023-11-06 · Route 29 · ${result.calibration_status || result.calibration?.status || result.calibration?.calibration_status || "UNCALIBRATED"}${candidates.length > 3 ? " · 警告：候选数量超出 0–3 输出合同" : ""}`);
    txt("gtfs-uncertainty-note", continuous ? "连续时空评分不是概率。只有独立校准的可信集和绝对质量门槛同时通过，才显示最终班次候选；未校准结果仅在诊断区展示。" : `校准概率：${result.probability == null ? "未知" : "见原始诊断，不能仅凭分数解释"}。候选评分不是概率；MATCHED 只是本实验算法判断，不是已验证 trip identity。${arr(data.warnings || result.warnings).join("；")}`);
    const summary = $("gtfs-evidence-summary"); summary.replaceChildren();
    for (const [key, label, value] of [["pattern_support", "Pattern 支持分（非概率）", result.pattern_confidence?.support_score], ["trip_support", continuous ? "班次校准概率" : "Trip 支持分（非概率）", continuous ? result.trip_confidence?.calibrated_probability : result.trip_confidence?.support_score], ["margin", continuous ? "第二名减第一名成本（非概率）" : "Top1–Top2 支持分差", result.top1_top2_margin], ["delay", "模型时移估计 / 秒", result.estimated_delay], ["windows", "支持窗口数（重叠不可相加）", result.supporting_window_count]]) {
      const item = node("div", "diff-stat"); item.dataset.metric = key;
      const display = key === "margin" ? finite(value) ? value !== 0 && Math.abs(value) < 0.000001 ? value.toExponential(4) : value.toLocaleString("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 6 }) : "未知" : fmt(value);
      const number = node("strong", "", display); if (key === "margin") number.title = finite(value) ? String(value) : "未知";
      item.append(number, node("span", "", label)); summary.append(item);
    }
    const list = $("gtfs-candidate-list"), diagnosticList = $("gtfs-diagnostic-list"), diagnostics = gtfsDiagnosticCandidates(); list.replaceChildren(); diagnosticList.replaceChildren();
    for (const [i, c] of gtfsAllCandidates().entries()) {
      const diagnostic = c.diagnostic_only === true, continuousCandidate = c.scorer === "continuous_passage_v1", id = gtfsCandidateId(c, i), labelText = gtfsCandidateLabel(c), card = node("div", diagnostic ? "gtfs-diagnostic-card" : "gtfs-candidate-card"); card.dataset.candidateId = id; card.dataset.tripId = rid(c.trip_id); card.dataset.diagnosticOnly = String(diagnostic); card.dataset.referenceId = rid(c.reference_id); card.style.setProperty("--candidate-color", gtfsColor(c));
      const label = node("label", diagnostic ? "gtfs-diagnostic-toggle" : "gtfs-candidate-toggle"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.gtfsVisible.has(id); checkbox.dataset.candidateId = id; checkbox.dataset.tripId = rid(c.trip_id); checkbox.setAttribute("aria-label", `显示${labelText} ${c.trip_id || id}`);
      checkbox.addEventListener("change", () => { if (checkbox.checked) state.gtfsVisible.add(id); else state.gtfsVisible.delete(id); renderCharts(); renderMap(); });
      label.append(checkbox, node("strong", "", `${labelText} · ${c.trip_id || id}`));
      const color = node("input", diagnostic ? "gtfs-diagnostic-color" : "gtfs-candidate-color"); color.type = "color"; color.value = gtfsColor(c); color.dataset.candidateId = id; color.setAttribute("aria-label", `${labelText}颜色`); color.addEventListener("input", () => { state.gtfsColors.set(id, color.value); card.style.setProperty("--candidate-color", color.value); renderCharts(); renderMap(); });
      const heading = node("div", "gtfs-candidate-heading"); heading.append(label, color); card.append(heading);
      if (diagnostic) card.append(node("p", "gtfs-diagnostic-warning", continuousCandidate ? "质量或校准可信集门槛未通过 · 仅供排查，不计入最终班次候选" : "未过门槛 · 仅供排查，不计入最终候选、状态或概率"));
      if (!diagnostic && typeof c.decision_gate?.absolute_cost_passed === "boolean") {
        const passed = c.decision_gate.absolute_cost_passed, gate = node("p", `gtfs-cost-gate${passed ? "" : " gtfs-cost-rejected"}`, passed ? "成本门通过（仍为候选，非已验证 trip）" : "近等价竞争者（超成本门，保留用于歧义检查）");
        card.dataset.absoluteCostPassed = String(passed); card.dataset.candidateRole = c.decision_gate.role || ""; gate.dataset.absoluteCostPassed = String(passed); gate.title = `${c.decision_gate.role || "最终决策成本门"} · 阈值 ${fmt(c.decision_gate.absolute_cost_threshold)}；是否通过成本门不等于已验证 trip identity。`; card.append(gate);
      }
      card.append(node("p", "", `${c.route_short_name || c.route_id || "29"} · ${c.trip_headsign || c.headsign || "终向未声明"} · reference ${c.reference_id || "未提供"}`));
      const score = c.score ?? c.total_score, scoreLine = node("p", "gtfs-candidate-score", continuousCandidate ? "" : `未校准评分 ${gtfsPrecise(score)} · 概率 ${c.probability == null ? "未知" : "未作为校准概率展示"}`); if (continuousCandidate) scoreLine.append(node("span", "", "连续时空成本（越低越好）"), node("span", "mono", ` ${gtfsPrecise(score)} · `), node("span", "", "校准概率"), node("span", "mono", c.calibrated === true && finite(c.probability) ? ` ${gtfsPrecise(c.probability)}` : " 未知")); scoreLine.title = finite(score) ? String(score) : "未知"; card.append(scoreLine);
      if (continuousCandidate) appendGTFSContinuousEvidence(card, c);
      if (!continuousCandidate && finite(c.coverage)) card.append(node("p", "", `有效 AVL 记录覆盖 ${fmt(c.coverage * 100)}%（不是时间覆盖率或准确率）· 连续支持窗口 ${fmt(c.continuous_support_windows)}`));
      if (finite(c.estimated_delay_s)) card.append(node("p", "", `模型时移 ${fmt(c.estimated_delay_s)} s · 下方仍画 GTFS 原计划时刻，不改两源原始时钟`));
      if (!continuousCandidate && finite(c.estimated_delay_s) && finite(data.config?.score_delay_prior_weight)) {
        const weight = data.config.score_delay_prior_weight, penalty = weight * Math.abs(c.estimated_delay_s) / 3600;
        const line = node("p", "gtfs-delay-penalty", `时间偏移惩罚 ${gtfsPrecise(penalty)} · 权重 ${gtfsPrecise(weight)} / 小时（提前与晚点对称）`);
        line.dataset.weight = String(weight); line.dataset.penalty = String(penalty); line.title = "此处仅为最终评分中的绝对时间偏移项；同一权重也参与延误拟合及边界模型。"; card.append(line);
      }
      if (diagnostic) {
        const gates = node("ul", "gtfs-failed-gates");
        for (const gate of arr(c.failed_gate_checks)) { const item = node("li", "", `${gate.label || gate.metric}：${gtfsGateValue(gate.actual, gate.unit)}；要求 ${gate.operator === "in" ? "∈" : gate.operator || ""} ${gtfsGateValue(gate.threshold, gate.unit)}`); item.dataset.metric = rid(gate.metric); item.dataset.actual = gate.actual == null ? "" : String(gate.actual); item.dataset.threshold = gate.threshold == null ? "" : String(gate.threshold); item.dataset.passed = String(gate.passed); item.title = json(gate); gates.append(item); }
        if (!gates.childElementCount) gates.append(node("li", "", "未返回具体拒判门槛；不得据此视为通过。")); card.append(gates);
      }
      const referenceButton = node("button", "subtle-button gtfs-candidate-reference", "查看此参考轴"); referenceButton.type = "button"; referenceButton.disabled = !gtfsReferences().some((r) => gtfsReferenceId(r) === rid(c.reference_id)); referenceButton.addEventListener("click", () => { state.gtfsReference = rid(c.reference_id); renderGTFSPanel(); renderCharts(); renderMap(); }); card.append(referenceButton);
      const detail = node("details"); detail.append(node("summary", "", "候选来源、时间窗与评分组成"), node("pre", "", json(c))); card.append(detail); (diagnostic ? diagnosticList : list).append(card);
    }
    if (!candidates.length) list.append(node("p", "stream-notice", "此方向候选没有最终 GTFS trip 候选。下方未过门槛候选只用于对比排查，不改变本次拒判结果。"));
    const diagnosticSummary = data.diagnostic_candidate_summary || {}; txt("gtfs-diagnostic-count", `${diagnostics.length} 个`); $("gtfs-diagnostic-candidates").dataset.candidateCount = String(diagnostics.length);
    txt("gtfs-diagnostic-note", `从已产生整趟评分、但未通过门槛的候选中按原评分由低到高展示，排除上方已展示的候选。总计 ${fmt(diagnosticSummary.total_failed)} 个未过门槛，${fmt(diagnosticSummary.already_displayed_failed)} 个已在最终列表保留；此处展示 ${diagnostics.length} 个。仅检索到但没有有效评分的 NO_EVIDENCE 项不补分数。${candidates.length ? "诊断曲线默认关闭，可勾选对比。" : "当前无最终候选，默认叠加诊断曲线；这不代表接受匹配。"}`);
    if (!diagnostics.length) diagnosticList.append(node("p", "stream-notice", data.diagnostic_candidates ? "没有额外可展示的已评分、未过门槛候选。" : "此版本没有提供额外诊断候选；未改变原结果。"));
    const refs = gtfsReferences(); options($("gtfs-reference-select"), refs.map((r) => [gtfsReferenceId(r), `${r.label || r.direction_label || r.direction || "Canonical"} · ${gtfsReferenceId(r)}`])); $("gtfs-reference-select").value = state.gtfsReference; $("gtfs-reference-select").disabled = refs.length < 2;
    txt("gtfs-reference-note", "只叠加同一 reference_id；更换候选不会把不同参考轴的 s 混为一体。");
    const projection = gtfsProjection(), valid = projection.filter(gtfsOnReference), off = projection.filter((p) => String(p.status).toUpperCase() === "OFF_REFERENCE");
    txt("gtfs-coverage", `所选参考轴：有效 AVL 投影 ${fmt(valid.length)} 点，OFF_REFERENCE ${fmt(off.length)} 点仅作诊断；APC 站点歧义 ${fmt(gtfsAPCEvidence().filter((p) => rid(p.reference_id) === state.gtfsReference && p.ambiguous === true).length)} 个位置候选不当成唯一 s 支持。有效 coverage、NO_EVIDENCE 窗口与阈值详见下方原始证据；缺证据不自动说明未运行。`);
    renderGTFSWindows(); txt("gtfs-diagnostics", json({ matching_mode: data.matching_mode, decision_selection: data.decision_selection, result, diagnostic_candidate_summary: data.diagnostic_candidate_summary, diagnostic_candidates: diagnostics, evidence_coverage: result.evidence_coverage, windows: data.windows, projection_summary: Array.isArray(data.projection) ? undefined : data.projection, apc_evidence_summary: Array.isArray(data.apc_evidence) ? undefined : data.apc_evidence, references: refs.map(({ geometry, ...r }) => r), off_reference_runs: data.off_reference_runs, config: data.config, warnings: data.warnings }));
  }
  const gtfsWindowQuality = (w) => w.quality_by_reference?.[state.gtfsReference] || w.quality || w;
  function renderGTFSWindows() {
    const windows = gtfsWindows(), filter = $("gtfs-window-filter").value, rows = windows.filter((w) => !filter || gtfsWindowQuality(w).status === filter), list = $("gtfs-window-list"); list.replaceChildren();
    txt("gtfs-window-count", `· ${rows.length} / ${windows.length} 个多尺度窗口，非独立事件`);
    for (const w of rows) { const q = gtfsWindowQuality(w), status = q.status || "未声明", button = node("button", "gtfs-window-row"); button.type = "button"; button.dataset.windowId = rid(w.id || w.window_id); button.dataset.status = status; button.append(node("strong", "", `${w.kind || "window"} · ${time(w.start, true)}–${time(w.end, true)} · ${status}`), node("span", "", `有效点 ${fmt(q.point_count)} · 实测持续 ${fmt(q.observed_duration_s)} s · 实测前进 ${fmt(q.observed_progress_m)} m`), node("small", "", arr(q.reasons).join(" · ") || "原始来源按跨尺度权重归一，重叠窗口不可独立重复计证据"));
      const coverage = w.quality_by_reference?.[state.gtfsReference] || w;
      if (finite(coverage.valid_projection_point_ratio) || finite(coverage.valid_projection_duration_ratio)) button.append(node("small", "", `有效点比例 ${finite(coverage.valid_projection_point_ratio) ? fmt(coverage.valid_projection_point_ratio * 100) + "%" : "未知"} · 有效持续比例 ${finite(coverage.valid_projection_duration_ratio) ? fmt(coverage.valid_projection_duration_ratio * 100) + "%" : "未知"} · 不是准确率`));
      button.addEventListener("click", () => { const [start, end] = sourceBounds(), a = Math.max(start, timestamp(w.start) ?? start), b = Math.min(end, timestamp(w.end) ?? end); if (b <= a) { toast("该证据窗口在当前已加载来源范围外，原始区间仍可追溯。"); return; } state.view = [a, b]; state.cursor = a; $("gtfs-windows-panel").dataset.selectedWindow = rid(w.id || w.window_id); refreshWindow(); }); list.append(button); }
    if (!rows.length) list.append(node("p", "stream-order-note", windows.length ? "没有符合筛选的证据窗口。" : "后端没有返回可定位窗口；不能推断连续覆盖。"));
  }
  function gtfsMarkSource(mark, source, row) {
    mark.setAttribute("data-source", source); mark.setAttribute("data-rid", rid(row.rid || row.source_rid)); mark.setAttribute("class", "svg-clickable");
    mark.addEventListener("click", (event) => { event.stopPropagation(); const original = findSourceObservation(source, row.rid || row.source_rid); if (original) selectObservation(original); else toast("当前加载窗口没有此原始记录；完整 source rid 仍保留在 GTFS 诊断中。"); });
  }
  function gtfsClip(group, d, id) { const defs = svg("defs"), clip = svg("clipPath", { id }); clip.append(svg("rect", { x: d.left, y: d.top, width: Math.max(1, d.width - d.left - d.right), height: d.height - d.top - d.bottom })); defs.append(clip); group.append(defs); const marks = svg("g", { "clip-path": `url(#${id})` }); group.append(marks); return marks; }
  function renderGTFSSchedule() {
    const root = $("gtfs-schedule-svg"), candidates = gtfsVisibleCandidates(), d = dimensions(root, 142 + candidates.length * 35), [a, b] = bounds(), g = document.createDocumentFragment(), x = axes(g, d, a, b), marks = gtfsClip(g, d, "gtfs-schedule-clip");
    root.style.height = `${d.height}px`;
    for (const [label, y] of [["AVL", 35], ["APC", 66], ...candidates.map((c) => [gtfsCandidateLabel(c), 106 + candidates.indexOf(c) * 35])]) g.append(svg("text", { x: d.left - 8, y: y + 3, class: "chart-label", "text-anchor": "end" }, label));
    for (const p of visibleAVL()) { const t = timestamp(p.t); if (t == null) continue; const mark = svg("line", { x1: x(t), x2: x(t), y1: 29, y2: 41, stroke: COLORS.avl, opacity: .55 }); gtfsMarkSource(mark, "avl", p); marks.append(mark); }
    for (const p of visibleAPC()) { const start = timestamp(p.arrival) ?? timestamp(p.departure), end = timestamp(p.departure) ?? start; if (start == null) continue; const mark = svg("line", { x1: x(Math.min(start, end)), x2: x(Math.max(start, end)) + .5, y1: 66, y2: 66, stroke: COLORS.not_evaluated, "stroke-width": 5, opacity: .55 }); mark.append(svg("title", {}, `APC ${p.rid} · ${eventTimeText(p.arrival)} → ${eventTimeText(p.departure)}`)); gtfsMarkSource(mark, "apc", p); marks.append(mark); }
    for (const [index, c] of candidates.entries()) {
      const y = 106 + index * 35, trajectory = gtfsTrajectory(c).filter((p) => timestamp(p.t) != null), color = gtfsColor(c), diagnostic = c.diagnostic_only === true; let previous = null;
      for (const p of trajectory) { const t = timestamp(p.t), mark = svg("line", { x1: x(t), x2: x(t), y1: y - 6, y2: y + 6, stroke: color, "stroke-width": p.kind === "departure" ? 2 : 1, "stroke-dasharray": diagnostic ? "2 2" : null, "data-trip-id": c.trip_id, "data-candidate-id": gtfsCandidateId(c), "data-stop-id": p.stop_id, "data-diagnostic-only": String(diagnostic), "data-reference-id": c.reference_id }); mark.append(svg("title", {}, `${gtfsCandidateLabel(c)} · ${c.trip_id} · ${p.stop_id} · ${p.kind} · ${eventTimeText(p.t)}${diagnostic ? " · 未过门槛，仅供排查" : ""}`)); marks.append(mark);
        if (previous && p.kind === "departure" && previous.stop_id === p.stop_id && t >= timestamp(previous.t)) marks.append(svg("line", { x1: x(timestamp(previous.t)), x2: x(t), y1: y, y2: y, stroke: color, "stroke-width": 6, "stroke-dasharray": diagnostic ? "2 3" : null, opacity: .5, "data-kind": "scheduled-dwell", "data-candidate-id": gtfsCandidateId(c), "data-diagnostic-only": String(diagnostic), "data-reference-id": c.reference_id })); previous = p;
      }
    }
    if (!candidates.length) g.append(svg("text", { x: d.width / 2, y: 108, class: "chart-empty" }, "无已显示的 GTFS 候选；不补计划时刻"));
    txt("gtfs-schedule-caption", `${candidates.filter((c) => !c.diagnostic_only).length} 个最终候选 / ${candidates.filter((c) => c.diagnostic_only).length} 个诊断候选可见 · 点划线为未过门槛计划，仅供排查`); drawCursor(g, d, x); root.replaceChildren(g);
  }
  function renderGTFSProgress() {
    const root = $("gtfs-progress-svg"), d = dimensions(root, 285), [a, b] = bounds(), g = document.createDocumentFragment(), x = axes(g, d, a, b);
    root.dataset.referenceId = state.gtfsReference || "";
    const points = gtfsProjection().filter(gtfsOnReference), evidence = gtfsAPCEvidence().filter(gtfsOnReference), candidates = gtfsVisibleCandidates().filter((c) => rid(c.reference_id) === state.gtfsReference), reference = gtfsReferences().find((r) => gtfsReferenceId(r) === state.gtfsReference);
    const values = [...points.map(gtfsS), ...evidence.map(gtfsS), ...candidates.flatMap((c) => gtfsTrajectory(c).map(gtfsS)), ...arr(reference?.geometry).map(gtfsS)].filter(finite), lo = values.length ? Math.min(...values) : 0, hi = values.length ? Math.max(...values) : 1, span = Math.max(hi - lo, 1), y = (s) => d.height - d.bottom - (s - lo) / span * (d.height - d.top - d.bottom);
    for (let i = 0; i < 4; i++) { const s = lo + span * i / 3; g.append(svg("line", { x1: d.left, x2: d.width - d.right, y1: y(s), y2: y(s), class: "chart-grid" }), svg("text", { x: d.left - 8, y: y(s) + 3, class: "chart-axis", "text-anchor": "end" }, fmt(s / 1000))); }
    g.append(svg("text", { x: d.left - 8, y: 11, class: "chart-label", "text-anchor": "end" }, "s · km"));
    const marks = gtfsClip(g, d, "gtfs-progress-clip");
    for (const c of candidates) { let previous = null; for (const p of gtfsTrajectory(c)) { const t = timestamp(p.t), s = gtfsS(p); if (t == null || s == null) { previous = null; continue; } if (previous && t >= timestamp(previous.t)) { const line = mapLine([x(timestamp(previous.t)), y(gtfsS(previous))], [x(t), y(s)], { stroke: gtfsColor(c), "stroke-width": p.stop_id === previous.stop_id ? 3.5 : 1.6, "stroke-dasharray": c.diagnostic_only ? "2 4" : p.stop_id === previous.stop_id ? null : "5 3", opacity: .75, "data-candidate-id": gtfsCandidateId(c), "data-trip-id": c.trip_id, "data-reference-id": c.reference_id, "data-diagnostic-only": String(c.diagnostic_only === true), "data-kind": p.stop_id === previous.stop_id ? "scheduled-dwell" : "scheduled-trajectory" }); line.append(svg("title", {}, `${gtfsCandidateLabel(c)} · ${c.trip_id} · GTFS 计划，非实测${c.diagnostic_only ? " · 未过门槛，仅供排查" : ""}`)); marks.append(line); } previous = p; } }
    let previous = null;
    // Iterate all projection records so invalid / OFF_REFERENCE points break paths.
    for (const p of gtfsProjection()) {
      const t = timestamp(p.t); if (!gtfsOnReference(p) || t == null) { previous = null; continue; }
      if (previous && p.fragment_id != null && rid(p.fragment_id) === rid(previous.fragment_id) && t >= timestamp(previous.t) && !p.break_before) marks.append(mapLine([x(timestamp(previous.t)), y(gtfsS(previous))], [x(t), y(gtfsS(p))], { stroke: COLORS.avl, "stroke-width": 1.8, "data-fragment-id": p.fragment_id, "data-source": "avl", "data-from-rid": previous.rid || previous.source_rid, "data-to-rid": p.rid || p.source_rid }));
      if (t >= a && t <= b) { const mark = svg("circle", { cx: x(t), cy: y(gtfsS(p)), r: 2.6, fill: COLORS.avl, "data-fragment-id": p.fragment_id, "data-status": p.status, "data-reference-id": p.reference_id }); mark.append(svg("title", {}, `AVL ${p.rid} · ${eventTimeText(p.t)} · s=${fmt(gtfsS(p))} m · fragment ${p.fragment_id} · 投影距离 ${fmt(p.projection_distance_m)} m`)); gtfsMarkSource(mark, "avl", p); marks.append(mark); } previous = p;
    }
    for (const p of evidence) { const start = timestamp(p.arrival ?? p.start), end = timestamp(p.departure ?? p.end) ?? start; if (start == null || end < start || end < a || start > b) continue; const mark = svg("line", { x1: x(start), x2: x(end) + .7, y1: y(gtfsS(p)), y2: y(gtfsS(p)), stroke: COLORS.apc, "stroke-width": 4, opacity: .8, "data-reference-id": p.reference_id, "data-status": p.status, "data-kind": "apc-stop-interval" }); mark.append(svg("title", {}, `APC ${p.rid} · stop ${p.stop_id} · ${eventTimeText(start)} → ${eventTimeText(end)} · 原站点参考 s=${fmt(gtfsS(p))} m`)); gtfsMarkSource(mark, "apc", p); marks.append(mark); }
    const validVisible = points.filter((p) => timestamp(p.t) >= a && timestamp(p.t) <= b).length;
    txt("gtfs-progress-note", `reference ${state.gtfsReference || "未提供"} · 当前窗有效 AVL ${fmt(validVisible)} 点；绿色为有效投影，黄色为 APC 站点到离站证据，彩色虚线为计划；短点划线为未过门槛诊断计划，仅供排查。${validVisible ? "按 fragment_id 断开，不跨无效／OFF_REFERENCE 记录补线。" : "当前窗口无有效 AVL 投影；APC 证据单独保留，不说明车辆未运行。"}${candidates.length < gtfsVisibleCandidates().length ? " 其它参考轴候选未叠加；可在上方切换 reference。" : ""}`);
    if (!state.gtfsReference || !values.length) g.append(svg("text", { x: d.width / 2, y: 130, class: "chart-empty" }, "没有可用 canonical s 证据；原始来源仍可审查"));
    drawCursor(g, d, x); root.replaceChildren(g);
  }
  function bind() {
    $("matching-mode-select").addEventListener("change", () => switchMatchingMode($("matching-mode-select").value));
    $("trip-detail-tab").addEventListener("click", () => showMatchingPage("detail")); $("day-overview-tab").addEventListener("click", () => showMatchingPage("overview"));
    $("day-overview-refresh").addEventListener("click", loadDayOverview);
    $("overview-window-size").addEventListener("change", updateOverviewWindow); $("overview-custom-minutes").addEventListener("change", updateOverviewWindow);
    $("overview-previous").addEventListener("click", () => shiftOverview(-1)); $("overview-next").addEventListener("click", () => shiftOverview(1));
    $("overview-pan").addEventListener("input", () => { const [a] = overviewBounds(); state.overviewStart = a + Number($("overview-pan").value) / 1000 * (86400 - state.overviewSize); renderDayOverviewCharts(); });
    $("gtfs-reference-select").addEventListener("change", () => { state.gtfsReference = $("gtfs-reference-select").value; renderGTFSPanel(); renderCharts(); renderMap(); });
    $("gtfs-window-filter").addEventListener("change", renderGTFSWindows);
    $("debug-method").addEventListener("change", updateMethodParameters);
    for (const id of ["observation-source-filter", "observation-window-only", "observation-page-size", "observation-conflicts-only"]) $(id).addEventListener("change", () => { state.observationPage = 0; renderObservations(); });
    $("direction-conflicts-visible").addEventListener("change", renderCharts);
    $("direction-filter-audit").addEventListener("toggle", () => { if (isDirection() && $("direction-filter-audit").open) txt("direction-filtered-json", json(state.detail?.filtered_avl || [])); });
    $("observation-search").addEventListener("input", () => { state.observationPage = 0; renderObservations(); });
    $("observation-first").addEventListener("click", () => { state.observationPage = 0; renderObservations(); });
    $("observation-previous").addEventListener("click", () => { state.observationPage -= 1; renderObservations(); });
    $("observation-next").addEventListener("click", () => { state.observationPage += 1; renderObservations(); });
    $("observation-last").addEventListener("click", () => { state.observationPage = Math.max(0, Math.ceil(state.observationRows.length / Number($("observation-page-size").value)) - 1); renderObservations(); });
    $("observation-page").addEventListener("change", () => { const page = Number($("observation-page").value); state.observationPage = Number.isFinite(page) ? Math.max(0, Math.floor(page) - 1) : 0; renderObservations(); });
    $("debug-form").addEventListener("submit", startDebugRun);
    $("debug-run-select").addEventListener("change", () => switchRun($("debug-run-select").value));
    $("debug-base-select").addEventListener("change", () => { state.base = $("debug-base-select").value; loadComparison(); });
    $("debug-reset").addEventListener("click", () => renderDebug(true));
    $("debug-refresh").addEventListener("click", async () => { try { await refreshDebug(false); await loadComparison(); } catch (e) { error(e.message); } });
    $("debug-load-ready").addEventListener("click", () => { if (state.pendingRun) switchRun(state.pendingRun); });
    for (const id of ["method-select", "vehicle-select"]) $(id).addEventListener("change", () => { if (!confirmLeave()) { $("method-select").value = state.method; $("vehicle-select").value = state.vehicle; return; } state.method = $("method-select").value; state.vehicle = $("vehicle-select").value; loadCatalog(); });
    for (const id of ["date-filter", "route-filter", "status-filter", "review-filter", "direction-filter", "direction-conflict-filter"]) { const e = $(id); e.dataset.previous = e.value; e.addEventListener("change", () => { if (!confirmLeave()) { e.value = e.dataset.previous; return; } e.dataset.previous = e.value; filterCases(); }); }
    $("case-search").addEventListener("input", () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => { if (state.dirty && !confirmLeave()) { $("case-search").value = state.lastSearch || ""; return; } state.lastSearch = $("case-search").value; filterCases(); }, 300); });
    $("load-more").addEventListener("click", () => { state.limit += 100; renderQueue(); });
    $("previous-case").addEventListener("click", () => navigate(-1)); $("next-case").addEventListener("click", () => navigate(1));
    $("apc-search").addEventListener("input", renderTable); $("review-notes").addEventListener("input", dirty); $("review-verdict").addEventListener("change", dirty);
    $("save-review").addEventListener("click", () => saveReview()); $("save-next").addEventListener("click", () => saveReview(true));
    $("map-fit").addEventListener("click", fitMap); $("map-zoom-in").addEventListener("click", () => zoom(1.5)); $("map-zoom-out").addEventListener("click", () => zoom(1 / 1.5));
    $("window-size").addEventListener("change", setWindowSize); $("window-previous").addEventListener("click", () => shiftWindow(-1)); $("window-next").addEventListener("click", () => shiftWindow(1)); $("window-focus").addEventListener("click", focusSelected);
    $("window-custom-apply").addEventListener("click", () => applyWindowSize(Number($("window-custom-value").value) * Number($("window-custom-unit").value)));
    $("window-custom-value").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); $("window-custom-apply").click(); } });
    $("timeline-pan").addEventListener("input", () => panWindow(Number($("timeline-pan").value)));
    $("timeline-pan-detail").addEventListener("click", () => { const [a, b] = sourceBounds(); $("window-size").value = b - a > 1800 ? "1800" : b - a > 600 ? "600" : "60"; setWindowSize(); });
    $("context-size").addEventListener("change", () => { if (!state.detail) return; if (!confirmLeave()) { $("context-size").value = String(state.contextSeconds); return; } const anchor = captureAnchor(); state.contextSeconds = Number($("context-size").value); loadCase(state.caseId, anchor); });
    $("map-basemap").addEventListener("click", () => { if (!state.basemap && !window.confirm(tr("启用街道底图将向 OpenStreetMap 的 tile.openstreetmap.org 请求当前地图区域的图块，第三方将获得你的 IP、浏览器信息和可推知的地图视野。不会发送 APC/AVL 原始记录或审查笔记。是否仅为本次页面启用？"))) return; state.basemap = !state.basemap; $("map-basemap").setAttribute("aria-pressed", String(state.basemap)); $("map-attribution").hidden = !state.basemap; txt("map-mode", state.basemap ? "OSM 街道底图 · 拖动平移 / 滚轮缩放" : "离线坐标地图 · 拖动平移 / 滚轮缩放"); renderMap(); });
    for (const id of ["layer-context", "layer-pings", "layer-counters"]) $(id).addEventListener("change", () => { renderMap(); renderCharts(); });
    $("time-scrubber").addEventListener("input", () => { if (!state.detail) return; const [a, b] = bounds(); state.cursor = a + Number($("time-scrubber").value) / 1000 * (b - a); cancelAnimationFrame(state.chartFrame); state.chartFrame = requestAnimationFrame(() => { renderCharts(); renderMap(); }); });
    for (const id of ["timeline-svg", "progress-svg", "gtfs-schedule-svg", "gtfs-progress-svg"]) $(id).addEventListener("click", (e) => chartClick($(id), e));
    const map = $("map-svg"); map.addEventListener("wheel", (e) => { if (!state.detail) return; e.preventDefault(); const r = map.getBoundingClientRect(); zoom(Math.exp(-Math.max(-150, Math.min(150, e.deltaY)) * .003), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
    map.addEventListener("pointerdown", (e) => { if (!state.map.center || e.button !== 0) return; state.drag = { x: e.clientX, y: e.clientY, center: [...state.map.center], pointer: e.pointerId }; state.mapDragged = false; if (!e.target.closest("[data-rid]")) map.setPointerCapture(e.pointerId); });
    map.addEventListener("pointermove", (e) => { if (!state.drag) return; const dx = e.clientX - state.drag.x, dy = e.clientY - state.drag.y; if (Math.abs(dx) + Math.abs(dy) > 4) state.mapDragged = true; if (state.mapDragged) { state.map.center = [state.drag.center[0] - dx / state.map.scale, state.drag.center[1] - dy / state.map.scale]; renderMap(); } });
    const stopDrag = () => { state.drag = null; setTimeout(() => { state.mapDragged = false; }, 0); }; map.addEventListener("pointerup", stopDrag); map.addEventListener("pointercancel", stopDrag);
    map.addEventListener("keydown", (e) => { if (e.key === "+" || e.key === "=") { e.preventDefault(); zoom(1.5); } else if (e.key === "-") { e.preventDefault(); zoom(1 / 1.5); } });
    document.addEventListener("keydown", (e) => { const typing = /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable; if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (state.page !== "overview") saveReview(); return; } if (typing || state.page === "overview" || e.ctrlKey || e.metaKey || e.altKey) return; if (e.key.toLowerCase() === "j") { e.preventDefault(); navigate(1); } if (e.key.toLowerCase() === "k") { e.preventDefault(); navigate(-1); } });
    window.addEventListener("beforeunload", (e) => { if (state.dirty || state.saving) { e.preventDefault(); e.returnValue = ""; } });
    let resizeTimer; const ro = new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.page === "overview") renderDayOverviewCharts(); else if (state.detail) { renderMap(); renderCharts(); renderTimelinePan(); } }, 100); }); ro.observe($("map-stage")); ro.observe($("main-content"));
  }
  init();
})();
