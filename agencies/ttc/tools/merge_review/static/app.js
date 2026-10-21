/* Local, read-only source inspection. No external resources until map opt-in. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const COLORS = { direct: "#258d70", partial: "#c7993f", ambiguous: "#ce7160", unmatched: "#93a3a8", ineligible: "#9986b1", not_evaluated: "#8296a4", avl: "#168b78", counter: "#cf625f", apc: "#ca983f" };
  const STATUS = { direct: "Direct support", partial: "Partial support", ambiguous: "Ambiguous", unmatched: "Unassociated", ineligible: "Ineligible", not_evaluated: "Matching not performed" };
  const LEVEL = { bounded_interval: "Bounded arrival/departure support", instant: "Instant support", arrival_only: "Arrival-only support", departure_only: "Departure-only support", interior_only: "Interval-interior support only" };
  const VERDICT = { accepted: "Acceptable", problem: "Problematic", uncertain: "Uncertain", unreviewed: "Not reviewed" };
  const METHOD = { gtfs_trip_matching: "GTFS · Route 29 schedule candidates", direction_destination: "Experiment 02 · direction + Destination candidate trips", vehicle_only: "Group by vehicle only · vehicle_only", vehicle_only_dedup: "Group by vehicle + exact AVL deduplication · vehicle_only_dedup", gap_only: "Time gaps only · gap_only", avl_context: "AVL context · avl_context", apc_assist: "APC-assisted · apc_assist", apc_assisted: "APC-assisted · apc_assisted" };
  const PARAMETER_LABEL = { gap_seconds: "AVL long-gap split (seconds)", context_min_points: "Minimum persistent context points", context_min_seconds: "Minimum context duration (seconds)", boundary_cooldown_seconds: "Candidate-boundary cooldown (seconds)", reversal_window_points: "Reversal detection window points", reversal_min_displacement_m: "Minimum reversal displacement (meters)", reversal_angle_degrees: "Reversal angle threshold (degrees)", reversal_min_straightness: "Minimum reversal-window straightness", apc_min_direction_points: "APC direction persistence in stops", apc_anchor_max_gap_seconds: "Maximum APC boundary-anchor gap (seconds)", same_time_conflict_distance_m: "Simultaneous GPS position conflict (meters)", time_tolerance_s: "Time matching tolerance (seconds)", radius_m: "Local spatial radius (meters)", interpolation_gap_s: "Maximum interpolation bracket (seconds)", episode_gap_s: "Maximum local-visit gap (seconds)", contradiction_radius_m: "Spatiotemporal counterevidence distance threshold (meters)", contemporaneous_s: "Counterevidence near-simultaneous range (seconds)", max_speed_kph: "Maximum feasible speed (km/h)", max_apc_dwell_s: "Maximum APC arrival/departure interval (seconds)", accept_score: "Acceptance score threshold (not a probability)", null_score: "Null-association candidate score", margin_score: "Minimum best-candidate score margin", max_episodes: "Maximum local candidate visits", max_measurements_per_record: "Maximum observed points inspected per APC record", route_policy: "Route-label conflict policy" };
  const RELATION = { accepted: "Accepted association", candidate: "Candidate evidence", context: "Nearby context", unassigned: "Unassigned" };
  const MATCHING_MODES = { global: "Global · full segment", early: "Early · first 1/3", middle: "Middle · middle 1/3", late: "Late · last 1/3" };
  const state = { meta: null, cases: [], filtered: [], reviews: {}, detail: null, selected: null, method: "", vehicle: "", caseId: "", contextSeconds: 180, sequence: 0, limit: 100, dirty: false, saving: false, revision: 0, cursor: null, view: null, basemap: false, map: { center: null, scale: 1 }, tableRows: [], chartFrame: 0, debug: null, run: "", base: "", job: null, pendingRun: null, comparison: null, compareSequence: 0, runSequence: 0, selectedObservation: null, observationPage: 0, observationRows: [] };
  Object.assign(state, { matchingMode: "global", page: "detail", modeSequence: 0, overviewSequence: 0, overview: null, overviewStart: null, overviewSize: 86400 });
  const fmt = (v) => v == null || v === "" ? "—" : typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(v);
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
  const setBadge = (e, status) => { e.className = `status-badge status-${Object.hasOwn(STATUS, status) ? status : "unknown"}`; e.textContent = STATUS[status] || status || "Not selected"; };
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
  const shortCandidateNote = (row) => `Short candidate · ${fmt(timestamp(row.end) - timestamp(row.start))} s (not a confirmed trip)`;
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
  async function api(path, options = {}) { if (state.debug?.enabled && /^\/api\/(meta|catalog|case|reviews)(?:[/?]|$)/.test(path)) path = runPath(path); const response = await fetch(path, { credentials: "same-origin", ...options }); let body; try { body = await response.json(); } catch { const e = new Error(`The server returned invalid data (${response.status})`); e.status = response.status; throw e; } if (!response.ok) { const e = new Error(typeof body.error === "string" ? body.error : json(body.error) || `Request failed (${response.status})`); e.status = response.status; throw e; } return body; }
  const matchingQuery = (mode = state.matchingMode) => isGTFS() ? `&matching_mode=${encodeURIComponent(mode)}` : "";
  const availableMatchingModes = () => arr(state.detail?.gtfs?.available_matching_modes || state.overview?.available_matching_modes || state.meta?.available_matching_modes).length ? arr(state.detail?.gtfs?.available_matching_modes || state.overview?.available_matching_modes || state.meta?.available_matching_modes) : ["global"];
  function renderMatchingControls() {
    const active = isGTFS(); $("matching-view-toolbar").hidden = !active;
    if (!active) state.page = "detail";
    const modes = availableMatchingModes();
    for (const option of $("matching-mode-select").options) { option.disabled = !modes.includes(option.value); option.textContent = MATCHING_MODES[option.value] + (!modes.includes(option.value) ? " · Not computed in this run" : ""); }
    $("matching-mode-select").value = state.matchingMode; $("matching-mode-select").disabled = !!state.modeLoading || !!state.runLoading;
    const selection = state.detail?.gtfs?.decision_selection, partial = state.matchingMode !== "global";
    txt("matching-mode-note", state.modeLoading ? "Loading this mode's independently computed candidates and scores; source data and review notes remain unchanged…" : `${MATCHING_MODES[state.matchingMode]}: ${partial ? "Select the corresponding third by valid projected travel distance; OFF_REFERENCE points, gaps and reference-axis jumps do not contribute length." : "Use the full evidence of each candidate segment, not joint vehicle-day optimization."}${selection && partial ? ` Selected progress: ${fmt(selection.effective_progress_m)} / ${fmt(selection.total_effective_progress_m)} m.` : ""}${modes.length === 1 ? " This historical run has only global results; partial modes require a new run." : " The four modes are scored independently; changing the viewing window does not recompute matching."}${partial ? " This is an offline partial-evidence experiment: shape projection used the full segment, not a real-time prefix prediction." : ""} Review notes remain bound to this run / segment and are shared across all four modes.`);
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
    } catch (e) { error(`Decision mode was not switched: ${e.message}`); }
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
    $("day-overview-page").setAttribute("aria-busy", "true"); txt("day-overview-status", "Loading valid projections, APC stop intervals and one GTFS candidate per segment for all vehicles…"); $("day-overview-facets").replaceChildren(); $("overview-case-list").replaceChildren();
    try {
      const data = await api(runPath(`/api/day-overview?matching_mode=${encodeURIComponent(mode)}`, run));
      if (seq !== state.overviewSequence || run !== state.run || mode !== state.matchingMode) return;
      state.overview = data; renderDayOverview();
    } catch (e) { if (seq === state.overviewSequence) { state.overview = null; txt("day-overview-status", `The day overview could not be loaded: ${e.message}. Use Refresh overview to retry.`); } }
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
    if (!finite(size) || size < 60 || size > 86400) { toast("The overview window must be 1–1440 minutes. The previous window is unchanged."); return; }
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
    txt("day-overview-status", `${data.day} · Route 29 · ${MATCHING_MODES[data.matching_mode] || data.matching_mode} · ${fmt(new Set(cases.map((c) => c.vehicle)).size)} vehicles / ${fmt(cases.length)} segments · ${fmt(accepted)} show a threshold-passing candidate / ${fmt(fallback)} show a below-threshold candidate / ${fmt(cases.length - accepted - fallback)} have no scored candidate. The overview is not limited by the left-side vehicle filter.`);
    $("day-overview-page").dataset.matchingMode = data.matching_mode; $("day-overview-page").dataset.runId = data.run_id; txt("overview-case-count", `${cases.length} items`);
    const list = document.createDocumentFragment();
    for (const c of cases) {
      const candidate = c.candidate, button = node("button", "overview-case-card"); button.type = "button"; button.dataset.caseId = c.id; button.dataset.candidateSource = candidate?.source || "none";
      button.append(node("strong", "", `${c.vehicle_label || c.vehicle} · ${c.direction_label || "UNKNOWN"} · ${range(c.start, c.end)}`), node("span", "", `${c.status || "UNKNOWN"} · ${candidate ? `${candidate.source === "fallback" ? "Below threshold · Top diagnostic" : "Passed threshold · Top candidate"} ${candidate.trip_id}` : "No scored candidate"}`), node("small", "mono", c.id));
      if (!arr(c.fragments).some((f) => arr(f.points).length)) button.append(node("small", "", "No drawable AVL projection; APC evidence and source records are retained"));
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
      const header = node("div", "panel-header"), heading = node("div"); heading.append(node("h3", "", `${facet.direction_label || "UNKNOWN"} · Shape ${facet.shape_id || "—"}`), node("span", "panel-subtitle mono", `Independent canonical s axis · ${referenceId} · ${fmt(relevant.length)} segments`)); header.append(heading); section.append(header);
      const wrap = node("div", "chart-wrap"), root = svg("svg", { role: "img", "aria-label": `${facet.direction_label} direction · ${referenceId} full-day time–distance diagram`, class: "overview-chart", "data-reference-id": referenceId, "data-direction": facet.direction_label, "data-view-start": a, "data-view-end": b }); wrap.append(root); section.append(wrap); sections.append(section);
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
        const group = svg("g", { class: "overview-case", "data-case-id": c.id, "data-vehicle": c.vehicle, "data-reference-id": referenceId, tabindex: "0", role: "button", "aria-label": `${c.vehicle_label || c.vehicle} ${c.direction_label} ${range(c.start, c.end)} ${c.status}; open trip-by-trip review` });
        const candidate = c.candidate?.reference_id === referenceId ? c.candidate : null, label = `${c.vehicle_label || c.vehicle} · ${c.direction_label} · ${range(c.start, c.end)}\n${c.id}\n${c.status} · ${candidate ? `${candidate.source === "fallback" ? "Below threshold (diagnostic only)" : "Threshold-passing candidate (not confirmed)"} ${candidate.trip_id}` : "No displayed candidate on this reference axis"}`;
        group.append(svg("title", {}, label));
        if (candidate) {
          let path = "", previous = null;
          for (const p of arr(candidate.trajectory)) { const t = timestamp(p.t), s = p.s ?? p.s_m; if (!finite(t) || !finite(s)) { previous = null; continue; } path += `${previous && t >= previous ? "L" : "M"}${x(t).toFixed(2)},${y(s).toFixed(2)} `; previous = t; }
          const mark = svg("path", { d: path, class: "overview-scheduled", fill: "none", stroke: candidate.source === "fallback" ? "#b47732" : "#5276b6", "stroke-width": 1.35, "stroke-dasharray": candidate.source === "fallback" ? "2 4" : "6 4", opacity: .64, "data-trip-id": candidate.trip_id, "data-candidate-source": candidate.source, "data-reference-id": referenceId });
          mark.append(svg("title", {}, `${label}\n${arr(candidate.failed_gate_checks).map((gate) => `${gate.label || gate.metric}: ${fmt(gate.actual)} ${gate.operator || ""} ${fmt(gate.threshold)}`).join("\n")}\nOriginal GTFS scheduled times; no delay shift applied`)); group.append(mark);
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
          if (uncertainPath) { const mark = svg("path", { d: uncertainPath, fill: COLORS.apc, stroke: COLORS.apc, "stroke-width": .5, "stroke-dasharray": "2 2", opacity: selected ? .22 : .07, "data-kind": "apc-stop-uncertainty-band", ...attrs }); mark.append(svg("title", {}, "The APC canonical stop position is ambiguous; the band preserves the s range without assigning a unique stop progress.")); group.append(mark); }
        }
        const highlight = (enabled) => { root.classList.toggle("is-highlighting", enabled); group.classList.toggle("is-highlighted", enabled); };
        group.addEventListener("pointerenter", () => highlight(true)); group.addEventListener("pointerleave", () => highlight(false)); group.addEventListener("focus", () => highlight(true)); group.addEventListener("blur", () => highlight(false));
        group.addEventListener("click", () => openOverviewCase(c)); group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); openOverviewCase(c); } }); marks.append(group);
      }
      if (!relevant.length) g.append(svg("text", { x: width / 2, y: 170, class: "chart-empty" }, "No drawable evidence for this direction / reference axis"));
      root.replaceChildren(g);
    }
    if (!facets.length) sections.append(node("p", "empty-panel", "No canonical reference is available in this run. All segments remain listed below; no trajectories are fabricated."));
    container.replaceChildren(sections);
  }
  function confirmLeave() { if (state.saving) { toast("The review is being saved. Wait for saving to finish before switching."); return false; } return !state.dirty || window.confirm("Your review notes have unsaved changes. Discard these changes and switch?"); }
  function options(select, values, empty) { const previous = select.value; select.replaceChildren(); if (empty) select.append(new Option(empty, "")); for (const [value, label] of values) select.append(new Option(label, value)); if ([...select.options].some((o) => o.value === previous)) select.value = previous; }
  function modeText(element, streamText, stream) { if (!element) return; if (element.dataset.matchingText == null) element.dataset.matchingText = element.textContent; element.textContent = stream ? streamText : element.dataset.matchingText; }
  function renderRepresentation() {
    const stream = isStream(), dedup = isDedup(); document.body.classList.toggle("vehicle-stream-mode", stream);
    document.title = stream ? "TIDES · APC–AVL source observations by vehicle" : "TIDES · APC–AVL trip-by-trip review";
    document.querySelector(".sidebar").setAttribute("aria-label", stream ? "Vehicle-day observation filters" : "Filters and candidate trips");
    modeText(document.querySelector(".sidebar-heading h1"), "Review by vehicle", stream);
    modeText($("method-select-label"), "Observation grouping", stream); modeText($("debug-mode-title"), "Single-day source observation collection", stream);
    modeText(document.querySelector(".sidebar-heading .eyebrow"), "VEHICLE-DAY STREAM", stream);
    modeText(document.querySelector(".sidebar-footer>span"), "J / K: next / previous review item", stream);
    modeText($("evidence-notice-copy"), `APC and AVL observations sharing a VehicleID are collected and stably sorted by source time. ${dedup ? "AVL rows identical across all 13 original CSV fields are merged, retaining the earliest source row and complete source rid lineage; all APC is retained." : "Source records are not deduplicated."} No trip segmentation, spatiotemporal matching or passenger association is performed. Sharing a vehicle does not imply the same trip or stop visit. Times are uncorrected and day edges are not trip boundaries.`, stream);
    modeText(document.querySelector(".review-panel h3"), "Review this vehicle's daily source observations", stream);
    modeText(document.querySelector(".review-panel .panel-subtitle"), "Inspect source records; do not evaluate a matching algorithm that has not run", stream);
    modeText($("save-next"), "Save and next item →", stream);
    modeText(document.querySelector(".progress-panel .chart-caption"), "Observed AVL distance only · APC is not projected", stream);
    modeText(document.querySelector(".progress-panel .panel-subtitle"), "Distance is accumulated only across continuous valid AVL observations. Plot gaps stay disconnected; this is neither trip segmentation nor trip completion.", stream);
    modeText(document.querySelector(".timeline-panel .panel-subtitle"), "Same-vehicle AVL observation times and APC arrival–departure intervals · No inferred evidence links", stream);
    modeText(document.querySelector(".inspector-panel h3"), "Source observation details", stream);
    modeText(document.querySelector(".inspector-panel .eyebrow"), "SOURCE RECORD INSPECTOR", stream);
    modeText(document.querySelector(".window-toolbar>small"), `Only the chart viewing window changes; source rows are not split. The combined table shows ${dedup ? "all retained daily records, with merged duplicates traceable" : "all source rows for the day"} and can be filtered. There are no trip endpoints or matching statuses.`, stream);
    modeText(document.querySelector(".metric:nth-child(2)>small"), "All daily APC source rows · Passenger counts are not summed", stream);
    modeText(document.querySelector(".main-footer>span:first-child"), "TIDES · Same-vehicle source observation review", stream);
    modeText(document.querySelector(".top-meta .version"), dedup ? "VEHICLE-ID · EXACT AVL DEDUP" : "VEHICLE-ID UNION ALL", stream);
    modeText(document.querySelector("#observation-stream-panel .panel-subtitle"), dedup ? "Exact duplicates across all 13 original AVL CSV fields are merged; all APC rows are retained. Table ordinals preserve the server's stable time order of retained records. No matching or trip segmentation is performed." : "UNION ALL · No matching, deduplication or trip segmentation; table ordinals preserve the server's full source-record order.", stream);
    $("observation-stream-panel").setAttribute("aria-label", dedup ? "Same-vehicle APC and deduplicated AVL records in shared time order" : "All same-vehicle APC and AVL records in shared time order");
    $("dedup-summary").hidden = !dedup;
    $("case-search").placeholder = stream ? "Search vehicles, routes, dates…" : "Search segment IDs, routes, dates…";
    $("case-list").setAttribute("aria-label", stream ? "Vehicle-day observation list" : "Candidate trip list"); document.querySelector(".metrics").setAttribute("aria-label", stream ? "Source-record counts for the current vehicle day" : "Current candidate segment statistics"); $("method-select").setAttribute("aria-label", stream ? "Observation grouping" : "Segmentation method");
    $("map-svg").setAttribute("aria-label", stream ? "Same-vehicle observed AVL and source APC locations; no match links" : "AVL geographic trajectory and associated APC records"); $("progress-svg").setAttribute("aria-label", stream ? "Observed AVL cumulative distance only; APC progress is unknown" : "Observed GPS cumulative distance and APC support points");
    modeText($("no-case"), "No vehicle-day observations match these filters. Adjust the filters on the left.", stream);
    $("status-filter").closest("label").hidden = stream; if (stream) $("status-filter").value = "";
    $("metric-support").closest(".metric").hidden = stream; $("metric-attention").closest(".metric").hidden = stream;
    $("observation-stream-panel").hidden = !stream; $("apc-detail-panel").hidden = stream;
    $("context-size").hidden = stream; $("layer-context").closest("label").hidden = stream; $("layer-counters").closest("label").hidden = stream;
    for (const legend of document.querySelectorAll(".map-legend>span")) legend.hidden = stream;
    let legend = $("stream-map-legend"); if (!legend) { legend = node("span", "stream-map-legend", "Green: AVL · Blue-gray: source APC locations · Does not imply a match"); legend.id = "stream-map-legend"; document.querySelector(".map-legend").prepend(legend); } legend.hidden = !stream;
    for (const button of [$("previous-case"), $("next-case")]) { const previous = button.id === "previous-case"; button.setAttribute("aria-label", `${previous ? "Previous" : "Next"} ${stream ? "vehicle-day item" : "trip"}`); button.title = `${previous ? "Previous" : "Next"} ${stream ? "review item" : "trip"} · ${previous ? "K" : "J"}`; }
    modeText($("window-size").querySelector('[value="full"]'), "Full-day window", stream);
    $("direction-panel").hidden = !isDirection(); $("direction-filters").hidden = !isDirection(); $("observation-direction-heading").hidden = !isDirection(); $("observation-conflicts-label").hidden = !isDirection(); document.body.classList.toggle("direction-candidate-mode", isDirection());
    if (!isDirection()) { $("direction-filter").value = ""; $("direction-conflict-filter").value = ""; }
    if (isDirection()) {
      document.title = "TIDES · Experiment 02 direction candidates";
      for (const [selector, text] of [[".sidebar-heading h1", "Direction candidate trips"], [".sidebar-heading .eyebrow", "DIRECTION CANDIDATES"], ["#debug-mode-title", "Direction + Destination segmentation experiment"], [".review-panel h3", "Review this direction candidate"], [".review-panel .panel-subtitle", "Candidate segmentation and direction conflicts; not confirmed trips or matching accuracy"], [".top-meta .version", "EXPERIMENT 02 · CANDIDATES"], ["#window-size option[value=full]", "Full loaded candidate window"], ["#observation-stream-panel .panel-subtitle", "Candidate members and conflict context are shown by source time. APC intervals crossing boundaries may belong to multiple candidates; this is not a unique match."], [".window-toolbar>small", "Moving the viewing window does not change direction segmentation. The table shows loaded members and context; do not double-count APC passenger totals across candidates."], [".metric:nth-child(2)>small", "Candidate APC members · May appear in multiple candidates"], [".main-footer>span:first-child", "TIDES · Unconfirmed direction candidate trips"]]) modeText(document.querySelector(selector), text, true);
      txt("evidence-notice-copy", "Experiment 02 uses the exact-field AVL deduplication from Experiment 01b, removes AVL Route PI / PO rows and retains all APC rows. APC RouteDirection and AVL Destination direction evidence define candidate trips, not confirmed trips. No APC–AVL spatiotemporal matching or APC distance projection is performed. Direction conflicts and boundary-crossing APC intervals are retained; Heading is not treated as operating direction. Day edges remain data truncation boundaries.");
      txt("stream-map-legend", "Green: AVL · Blue-gray: APC · Orange-red: direction conflict (not a match status)");
      $("context-size").hidden = false; $("layer-context").closest("label").hidden = false;
      $("case-list").setAttribute("aria-label", "Direction candidate trip list"); $("observation-stream-panel").setAttribute("aria-label", "Combined observations for direction-candidate members and context"); $("case-search").placeholder = "Search candidate IDs, directions, routes…";
      document.querySelector(".sidebar").setAttribute("aria-label", "Direction candidate and conflict filters"); txt("no-case", "No direction candidates or unlocated review windows match. Adjust the filters.");
      for (const button of [$("previous-case"), $("next-case")]) { const previous = button.id === "previous-case"; button.setAttribute("aria-label", `${previous ? "Previous" : "Next"} direction-candidate review item`); button.title = `${previous ? "Previous" : "Next"} direction candidate · ${previous ? "K" : "J"}`; }
    } else if (stream) txt("stream-map-legend", "Green: AVL · Blue-gray: source APC locations · Does not imply a match");
    for (const id of ["gtfs-candidate-panel", "gtfs-schedule-panel", "gtfs-progress-panel"]) $(id).hidden = !isGTFS();
    document.body.classList.toggle("gtfs-mode", isGTFS());
    if (isGTFS()) {
      document.title = "TIDES · Route 29 GTFS candidate review";
      $("case-search").placeholder = "Search candidates, directions, GTFS status (AMBIGUOUS…)";
      for (const [selector, text] of [[".sidebar-heading h1", "GTFS candidate review"], [".sidebar-heading .eyebrow", "SCHEDULE EVIDENCE"], ["#debug-mode-title", "GTFS scheduled-candidate experiment"], [".top-meta .version", "GTFS · ROUTE 29 · NOV 06"], [".review-panel .panel-subtitle", "Review scheduled candidates and evidence; algorithm status is not independently verified accuracy"], [".main-footer>span:first-child", "TIDES · GTFS candidates and source evidence"]]) modeText(document.querySelector(selector), text, true);
      txt("evidence-notice-copy", `2023-11-06 · Route 29: ${state.meta.gap_segmentation_used ? "First split Experiment 02 direction candidates where a joint APC + AVL observation gap strictly exceeds the threshold, then compare GTFS schedules independently for each subsegment." : "Compare GTFS schedules against Experiment 02 direction candidates."} Retain source observations, direction conflicts and boundary uncertainty. The original cumulative-distance plot does not project APC. Canonical s plots use only valid backend AVL projections and APC stop evidence; no associations are fabricated from nearest GPS points. OFF_REFERENCE is diagnostic only. Candidate scores are not calibrated probabilities, and MATCHED is not independently verified.`);
    }
    if (!stream) { state.selectedObservation = null; $("observation-selection-note").hidden = true; }
    renderMatchingControls();
  }
  function updateMethodParameters() {
    const method = $("debug-method").value, stream = streamMethod(method), dedup = method === "vehicle_only_dedup"; $("vehicle-only-config-note").hidden = !stream;
    txt("vehicle-only-config-note", dedup ? "Merge AVL rows only when all 13 original CSV fields are identical, retaining the smallest source_row as representative and all source rid lineage. Retain every APC row, then group by VehicleID and sort by time. No trip segmentation, spatiotemporal matching or interpolation is performed." : "Collect daily APC and AVL by VehicleID, retain every source row and sort by time. No trip segmentation, spatiotemporal matching, interpolation or threshold decision is performed. Sharing a vehicle does not imply the same trip or stop visit.");
    modeText(document.querySelector(".debug-submit>small"), dedup ? "There are no segmentation or matching thresholds to tune. Only traceable exact AVL duplicates are merged; APC, original files and earlier experiments remain unchanged." : "This method has no segmentation or matching thresholds. All same-vehicle source rows for the fixed day are retained with reproducible time ordering.", stream);
    for (const group of ["segmentation", "merge"]) { const container = $(`debug-${group}-parameters`); container.closest("details").hidden = stream; for (const input of container.querySelectorAll("input,select")) input.disabled = stream || !!state.runLoading; }
    $("debug-gtfs-settings").hidden = method !== "gtfs_trip_matching";
    for (const input of $("debug-gtfs-parameters").querySelectorAll("input,select")) input.disabled = method !== "gtfs_trip_matching" || !!state.runLoading;
    let continuousNote = $("debug-continuous-parameter-note"); if (!continuousNote) { continuousNote = node("p", "stream-notice", "Legacy DTW and hourly time-shift parameters apply only to the old scorer. Use continuous_* parameters for continuous time–space scoring. Legacy values remain unchanged."); continuousNote.id = "debug-continuous-parameter-note"; $("debug-gtfs-settings").append(continuousNote); }
    const continuousEditor = $("debug-gtfs-continuous_matching_enabled")?.checked === true; continuousNote.hidden = method !== "gtfs_trip_matching" || !continuousEditor;
    const legacyWeightField = $("debug-gtfs-score_delay_prior_weight")?.closest("label"); if (legacyWeightField) legacyWeightField.dataset.inactiveForContinuous = String(continuousEditor);
    if (method === "direction_destination") { txt("vehicle-only-config-note", "Experiment 02 v1: exact deduplication across all 13 AVL CSV fields, followed by exclusion of AVL Route=PI/PO; all APC is retained. APC RouteDirection / AVL Destination evidence generates candidates while retaining conflicts and non-unique membership. Heading is not used for direction segmentation and the APC–AVL matcher is not run. Rules are versioned constants; the run summary preserves all settings."); modeText(document.querySelector(".debug-submit>small"), "This version uses segmentation={} and merge={}; earlier segmentation / matching thresholds do not apply. Candidate counts and direction-conflict counts are not accuracy metrics.", true); }
    if (method === "gtfs_trip_matching") { txt("vehicle-only-config-note", "2023-11-06, Route 29 only: compare GTFS schedules against Experiment 02 direction candidates, retaining up to three final candidates and uncertainty. Earlier APC–AVL matcher parameters do not apply. Valid projections, stop evidence, OFF_REFERENCE records and full configuration remain traceable."); modeText(document.querySelector(".debug-submit>small"), "Scheduled candidates are not confirmed trips. Without ground truth, accuracy and calibrated probabilities remain unknown. Chart window length is a viewing setting and does not change the experiment algorithm.", true); }
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
    if (row) state.selected = rid(row.rid); else if (anchor.rid) toast(isStream() ? "The original APC row is not in this vehicle day. Its time position is retained; no match was inferred." : "This segment does not contain the original APC row. Its source time was used to restore position; segment ID was not used to assume trip identity.");
    const [start, end] = sourceBounds();
    if (anchor.view && anchor.view[1] > start && anchor.view[0] < end) { state.view = [Math.max(start, anchor.view[0]), Math.min(end, anchor.view[1])]; if (state.view[1] <= state.view[0]) state.view = null; if (anchor.view[0] < start || anchor.view[1] > end) toast("The APC / time position is retained. The viewing window was clipped to the newly loaded segment; actual trip endpoints were not changed."); }
    else if (anchor.full) state.view = null;
    if (finite(anchor.cursor)) state.cursor = Math.max(start, Math.min(end, anchor.cursor));
    else if (row) state.cursor = timestamp(row.arrival);
    syncWindowSize();
  }
  function renderDebug(resetParameters = true) {
    if (!state.debug?.enabled) return;
    $("debug-console").hidden = false; $("debug-diff").hidden = false; txt("debug-date", state.debug.date || "2023-11-06");
    const runs = arr(state.debug.runs), opts = runs.map((r) => [debugRunId(r), `${r.label || "Unnamed experiment"} · ${debugRunId(r)}`]);
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
          if (parameter.type === "enum") for (const choice of arr(parameter.choices)) input.append(new Option({ flag: "Flag inconsistencies only", abstain_conflict: "Retain ambiguity on label conflicts" }[choice] || String(choice), String(choice)));
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
      if (state.saving || $("review-notes").value !== draft.notes || document.querySelector("#review-verdict input:checked")?.value !== draft.verdict || (!draft.dirty && state.dirty)) { state.pendingRun = run; $("debug-load-ready").hidden = false; $("debug-job").hidden = false; $("debug-run-select").value = state.run; txt("debug-job-message", "Notes were edited during loading, so the run was not switched. Save them first, then load the run."); return; }
      // Notes belong to the exact run, even when source IDs survive a rerun.
      state.run = run; state.meta = meta; state.comparison = null; state.pendingRun = null; state.overview = null; state.detail = null; ++state.overviewSequence; ++state.modeSequence; state.modeLoading = false; if (!arr(meta.available_matching_modes).includes(state.matchingMode)) state.matchingMode = "global"; $("debug-load-ready").hidden = true;
      options($("method-select"), arr(meta.methods).map((m) => [m, METHOD[m] || m])); state.method = meta.default_method || $("method-select").value; $("method-select").value = state.method;
      options($("vehicle-select"), arr(meta.vehicles).map((v) => typeof v === "object" ? [rid(v.id), `Vehicle ${v.label || v.id}`] : [rid(v), `Vehicle ${v}`]));
      state.vehicle = [...$("vehicle-select").options].some((o) => o.value === anchor.vehicle) ? anchor.vehicle : rid(meta.default_vehicle || $("vehicle-select").value); $("vehicle-select").value = state.vehicle;
      txt("run-label", `${run} · Independent single-day run · Read-only source data`); renderDebug(); await loadCatalog(anchor); if (state.page === "overview") await loadDayOverview();
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
      if (input.dataset.type !== "enum" && (!input.value.trim() || !Number.isFinite(value) || (input.dataset.type === "integer" && !Number.isInteger(value)))) throw new Error(`Parameter ${input.dataset.key} must be a valid ${input.dataset.type === "integer" ? "integer" : "number"}`);
      config[input.dataset.group][input.dataset.key] = value;
    }
    return config;
  }
  async function startDebugRun(event) {
    event.preventDefault(); if (state.runLoading) { toast("Wait for the selected run to finish loading before editing and submitting an experiment."); return; } if (!state.debug?.enabled || state.job || !$("debug-form").reportValidity()) return;
    try {
      const config = readDebugConfig(); $("debug-submit").disabled = true; state.job = { state: "queued", message: "Submitting single-day experiment parameters…" }; renderJob(state.job); error("");
      const result = await api("/api/debug/runs", { method: "POST", headers: { "Content-Type": "application/json", "X-Review-Token": state.meta.csrf_token }, body: JSON.stringify({ config, label: $("debug-label").value.trim(), parent_run_id: state.run }) });
      state.job = result.job; if (!state.job?.id) throw new Error("The server did not return an experiment job ID"); state.pendingRun = null; $("debug-load-ready").hidden = true; renderJob(state.job); pollDebugJob(state.job.id);
    } catch (e) { state.job = null; $("debug-submit").disabled = false; renderJob({ state: "failed", error: e.message }); error(e.message); }
  }
  function renderJob(job) {
    $("debug-job").hidden = false; txt("debug-job-title", { queued: "Queued", running: "Reprocessing the single day", complete: "Reprocessing complete", failed: "Reprocessing failed" }[job.state] || job.state || "Submitted");
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
          if (state.dirty || state.saving) { $("debug-load-ready").hidden = false; txt("debug-job-message", "Results are saved. Notes are unsaved or being saved, so the run was not switched automatically. Save notes before loading the new run."); }
          else await switchRun(job.run_id, true);
        }
      } else setTimeout(() => pollDebugJob(id), 1000);
    } catch (e) { if ([400, 404].includes(e.status)) { state.job = null; $("debug-submit").disabled = false; renderJob({ state: "failed", error: `Job status cannot be recovered: ${e.message}. Refresh the run list to inspect completed results; do not assume the original job did not run.` }); return; } txt("debug-job-message", `Progress temporarily unavailable: ${e.message}. Retrying; the experiment has not been resubmitted.`); if (state.job?.id === id) setTimeout(() => pollDebugJob(id), 3000); }
  }
  async function loadComparison() {
    if (!state.debug?.enabled || !state.base || !state.run || !state.vehicle) return;
    const seq = ++state.compareSequence; state.comparison = null; txt("debug-diff-status", "Comparing by source records…"); $("debug-diff-list").replaceChildren(); $("debug-diff-summary").replaceChildren(); $("debug-boundary-svg").replaceChildren();
    try {
      const comparison = await api(`/api/debug/compare?base=${encodeURIComponent(state.base)}&run=${encodeURIComponent(state.run)}&vehicle=${encodeURIComponent(state.vehicle)}`); if (seq !== state.compareSequence) return;
      state.comparison = comparison; renderComparison();
    } catch (e) { if (seq === state.compareSequence) txt("debug-diff-status", `Comparison unavailable: ${e.message}`); }
  }
  function clearComparison() { if (!state.debug?.enabled) return; ++state.compareSequence; state.comparison = null; txt("debug-diff-status", "Switching vehicle or run…"); for (const id of ["debug-diff-list", "debug-diff-summary", "debug-boundary-svg", "debug-diff-json"]) $(id).replaceChildren(); }
  function hideBoundaryComparison(hidden) { const chart = $("debug-boundary-svg"); chart.hidden = hidden; if (hidden) chart.setAttribute("hidden", ""); else chart.removeAttribute("hidden"); $("debug-diff-summary").hidden = hidden; }
  function renderComparison() {
    const diff = state.comparison; if (!diff) return; const summary = diff.summary || {};
    if (diff.comparison_status === "scope_and_diagnostics_only") {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), "Compare only Route 29 source identities, retained counts and GTFS candidate diagnostics. Experiment 02 did not run GTFS matching; this comparison cannot establish improved accuracy.", true);
      txt("debug-diff-status", "Route 29 sources and GTFS diagnostics · Not an accuracy comparison"); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true); $("debug-diff-summary").hidden = false;
      const counts = diff.source_counts || {}, metrics = [["base_avl", "Baseline Route 29 AVL", counts.base?.avl], ["current_avl", "Current Route 29 AVL", counts.current?.avl], ["base_apc", "Baseline Route 29 APC", counts.base?.apc], ["current_apc", "Current Route 29 APC", counts.current?.apc], ["identity_changes", "Source identity changes", summary.violations?.route_29_identity_changed]];
      $("debug-diff-summary").replaceChildren(...metrics.map(([key, label, value]) => { const item = node("div", "diff-stat"); item.dataset.metric = key; item.append(node("strong", "", fmt(value)), node("span", "", label)); return item; }));
      $("debug-diff-list").replaceChildren(node("p", "stream-notice", "The versions operate at different processing levels. Source identity and retained counts can be checked, but GTFS status is diagnostic only; it cannot establish matching or segmentation improvement.")); txt("debug-diff-json", json(diff)); return;
    }
    if (diff.comparison_status === "direction_segmentation_comparison") {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), "Experiment 02 comparison checks only PI/PO exclusion, APC retention, source lineage and direction-candidate counts; it does not claim improved matching.", true);
      txt("debug-diff-status", "Direction-candidate segmentation comparison · No matching accuracy"); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true); $("debug-diff-summary").hidden = false;
      const labels = { avl_removed_by_pi_po_filter: "AVL excluded as PI/PO", unexpected_removed: "Unexplained removals", unexpected_added: "Unexplained additions", original_fields_changed: "Original-field changes", base_segments: "Baseline candidates", current_segments: "Current candidates", current_conflicts: "Current conflict windows", current_conflict_observations: "Records involved in conflicts" };
      $("debug-diff-summary").replaceChildren(...Object.entries(labels).map(([key, label]) => { const item = node("div", "diff-stat"); item.dataset.metric = key; item.append(node("strong", "", fmt(summary[key])), node("span", "", label)); return item; }));
      $("debug-diff-list").replaceChildren(node("p", "stream-notice", `APC: ${summary.apc_unchanged === true ? "Original records verified unchanged" : "Retention needs checking"}; source lineage: ${summary.source_lineage_conserved === true ? "Complete" : "Needs checking"}. Candidate counts are not actual trip counts, and conflict windows are not independent conflict events. PI/PO exclusion and deduplication retain audits; complete source-ID differences are below.`)); txt("debug-diff-json", json(diff)); return;
    }
    if (diff.not_comparable || diff.comparable === false || diff.comparison_status === "not_comparable") {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), "Source-observation collection and segmentation / matching are different processing levels. No artificial matching-improvement or boundary-change metrics are produced.", true);
      txt("debug-diff-status", "Different processing levels · Matching performance is not comparable"); $("debug-diff-summary").replaceChildren(); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true);
      $("debug-diff-list").replaceChildren(node("p", "stream-notice", "The runs use different segmentation / matching levels. Matching improvement and removed-boundary counts cannot be directly compared."), node("p", "stream-order-note", typeof diff.reason === "string" ? diff.reason : json(diff.reason)));
      txt("debug-diff-json", json(diff)); return;
    }
    if (diff.comparison_status === "source_stream_comparison" || (isStream() && diff.representation === "vehicle_observation_stream")) {
      modeText(document.querySelector("#debug-diff .panel-subtitle"), diff.avl_deduplication_comparison ? "Compare exact AVL duplicate merging and source lineage; check APC retention separately. Matching accuracy is not evaluated." : "Check complete source rid lineage and stable display order only; neither run performs trip segmentation or matching decisions.", true);
      txt("debug-diff-status", "Source records and ordering only · Matching not performed"); $("debug-diff-summary").replaceChildren(); $("debug-boundary-svg").replaceChildren(); hideBoundaryComparison(true);
      const list = $("debug-diff-list"); list.replaceChildren(node("p", "stream-notice", "Both runs contain same-vehicle observation streams. Only source rid, retention and display order are checked; no match-status, performance or trip-boundary changes are calculated."));
      if (diff.avl_deduplication_comparison) {
        txt("debug-diff-status", "Exact AVL deduplication comparison · Not matching performance"); $("debug-diff-summary").hidden = false;
        const labels = { dedup_explained_removals: "Exact duplicate merges", dedup_explained_additions: "Duplicates restored in reverse comparison", unexplained_removals: "Unexplained removals", unexplained_additions: "Unexplained additions", display_ordinals_renumbered: "Display ordinals renumbered" };
        $("debug-diff-summary").append(...Object.entries(labels).map(([key, label]) => { const item = node("div", "diff-stat"); item.dataset.metric = key; item.append(node("strong", "", fmt(summary[key])), node("span", "", label)); return item; }));
        const retained = diff.retained_counts || {}; list.append(node("p", "stream-order-note", `Retained AVL: ${fmt(retained.base?.avl)} → ${fmt(retained.current?.avl)}; retained APC: ${fmt(retained.base?.apc)} → ${fmt(retained.current?.apc)}. APC: ${summary.apc_unchanged === true ? "Source records verified unchanged" : summary.apc_unchanged === false ? "Changed; needs checking" : "Conservation not yet checked"}; AVL source lineage: ${summary.source_lineage_conserved === true ? "Complete" : summary.source_lineage_conserved === false ? "Incomplete; needs checking" : "Not yet checked"}.`), node("p", "stream-order-note", "Exact duplicate merging reduces displayed row counts, not necessarily source evidence. Check unexplained changes and source rid lineage together. display_order may be renumbered after deduplication; source times and relative ordering do not change."));
        if (diff.deduplication_note) list.append(node("p", "stream-order-note", typeof diff.deduplication_note === "string" ? diff.deduplication_note : json(diff.deduplication_note)));
      }
      txt("debug-diff-json", json(diff)); return;
    }
    hideBoundaryComparison(false);
    modeText(document.querySelector("#debug-diff .panel-subtitle"), "", false);
    txt("debug-diff-status", diff.source_compatible === false ? "Source evidence is incompatible; differences cannot be attributed to the algorithm" : `${fmt(summary.apc_common)} common APC rows · ${diff.truncated ? "Returned results truncated" : "Current vehicle"}`);
    const labels = { changed_rows: "Changed APC", status_changed: "Status changes", support_changed: "Support-level changes", owner_membership_changed: "Segment-membership changes", evidence_changed: "GPS evidence changes", decision_changed: "Decision-detail changes", boundaries_added: "Added boundaries", boundaries_removed: "Removed boundaries", boundary_reasons_changed: "Boundary-reason changes" };
    $("debug-diff-summary").replaceChildren(...Object.entries(labels).map(([key, label]) => { const item = node("div", "diff-stat"); item.append(node("strong", "", fmt(summary[key] ?? 0)), node("span", "", label)); return item; }));
    const list = $("debug-diff-list"); list.replaceChildren();
    for (const row of arr(diff.changes).slice(0, 200)) {
      const b = node("button", "diff-row"); b.type = "button"; b.dataset.apcRid = row.apc_rid;
      b.append(node("span", "mono", `${time(row.arrival, true)} · ${row.apc_rid}`), node("span", "", `${STATUS[row.base?.status] || row.base?.status || "No record"} → ${STATUS[row.current?.status] || row.current?.status || "No record"}`), node("small", "", arr(row.change_types).join(" · ")));
      b.addEventListener("click", () => openDiffRow(row)); list.append(b);
    }
    if (!arr(diff.changes).length) list.append(node("div", "empty-small", "No source-rid-level decision changes for this vehicle. Boundary changes are shown separately."));
    if (arr(diff.changes).length > 200) list.append(node("p", "diff-limit", `Showing the first 200 / ${fmt(diff.changes.length)} rows; complete differences are in the traceable JSON below.`));
    txt("debug-diff-json", json(diff)); renderBoundaryDiff();
  }
  async function openDiffRow(row) {
    const existing = arr(state.detail?.apc).find((r) => rid(r.rid) === rid(row.apc_rid));
    if (existing) { selectAPC(row.apc_rid); return; }
    const anchor = { rid: row.apc_rid, at: timestamp(row.arrival), cursor: timestamp(row.arrival), view: finite(timestamp(row.arrival)) ? [timestamp(row.arrival) - 300, timestamp(row.arrival) + 300] : null, vehicle: state.vehicle };
    const target = anchorCase(anchor); if (!target) { toast("No review window in the current run contains this APC row; its baseline record is retained in the difference JSON."); return; }
    if (!confirmLeave()) return;
    if (!state.filtered.includes(target)) { for (const id of ["route-filter", "status-filter", "review-filter", "case-search"]) $(id).value = ""; state.lastSearch = ""; filterCases(false); }
    await loadCase(target.id, anchor);
  }
  function renderBoundaryDiff() {
    const diff = state.comparison; if (!diff || diff.not_comparable || diff.comparable === false || diff.comparison_status === "source_stream_comparison" || isStream()) return;
    const root = $("debug-boundary-svg"), d = dimensions(root, 150), g = document.createDocumentFragment();
    let [a, b] = state.detail ? bounds() : [timestamp(`${state.debug.date}T00:00:00`), timestamp(`${state.debug.date}T00:00:00`) + 86400];
    if (!finite(a) || !finite(b) || b <= a) return; const x = axes(g, d, a, b);
    const lanes = [["removed", "Removed from baseline", 35, COLORS.counter], ["added", "Added in current", 65, COLORS.avl], ["changed", "Reason changed", 95, COLORS.apc]];
    for (const [key, label, y, color] of lanes) {
      g.append(svg("text", { x: d.left - 8, y: y + 3, class: "chart-label", "text-anchor": "end" }, label));
      g.append(svg("line", { x1: d.left, x2: d.width - d.right, y1: y, y2: y, stroke: "#e0e9e1", "stroke-width": 2 }));
      for (const boundary of arr(diff.boundaries?.[key])) { const t = timestamp(boundary.at); if (!finite(t) || t < a || t > b) continue; const mark = svg("line", { x1: x(t), x2: x(t), y1: y - 10, y2: y + 10, stroke: color, "stroke-width": 3, class: "svg-clickable", tabindex: 0 }); mark.append(svg("title", {}, `${label} ${time(t, true)} · ${boundary.before_rid || ""} → ${boundary.after_rid || ""}\n${key === "changed" ? `${arr(boundary.base_reasons).join(", ")} → ${arr(boundary.current_reasons).join(", ")}` : arr(boundary.reasons).join(", ")}`)); const focus = () => { if (!state.detail) return; state.cursor = t; renderCharts(); renderMap(); }; mark.addEventListener("click", focus); mark.addEventListener("keydown", (e) => { if (e.key === "Enter") focus(); }); g.append(mark); }
    }
    g.append(svg("text", { x: d.width - d.right, y: 13, class: "chart-axis", "text-anchor": "end" }, state.detail ? "Follows the viewing window below · Unchanged boundaries are omitted" : "Fixed single-day scope · Unchanged boundaries omitted")); root.replaceChildren(g);
  }
  async function init() {
    bind();
    try {
      state.meta = await api("/api/meta");
      // Older frozen-only servers do not implement /api/debug. Their meta has
      // no debug marker, so opening them must not create a spurious HTTP error.
      if (state.meta.debug_enabled || state.meta.debug_date) { try { state.debug = await api("/api/debug"); } catch (e) { if (e.status !== 404) toast(`Single-day experiment console unavailable: ${e.message}`); } }
      if (state.debug?.enabled) { state.run = state.meta.run_id || state.debug.runs?.[0]?.id || state.debug.runs?.[0]?.run_id || ""; state.base = currentDebugRun()?.parent_run_id || debugRunId(arr(state.debug.runs).at(-1) || {}) || state.run; renderDebug(); }
      options($("method-select"), arr(state.meta.methods).map((m) => [m, METHOD[m] || m]));
      options($("vehicle-select"), arr(state.meta.vehicles).map((v) => typeof v === "object" ? [rid(v.id), `Vehicle ${v.label || v.id}`] : [rid(v), `Vehicle ${v}`]));
      state.method = state.meta.default_method || $("method-select").value;
      state.vehicle = rid(state.meta.default_vehicle || $("vehicle-select").value);
      $("method-select").value = state.method; $("vehicle-select").value = state.vehicle;
      txt("run-label", `${state.meta.run_id || "LOCAL RUN"} · Read-only source data`);
      if (!state.vehicle || !state.method) throw new Error("No vehicles or merge results are available. Check the server's data directory.");
      await loadCatalog();
    } catch (e) { error(e.message); $("case-list").replaceChildren(node("div", "empty-small", "Data could not be loaded. Check the server and input run directory, then refresh.")); }
  }
  async function loadCatalog(anchor = null) {
    const seq = ++state.sequence; clearTimeout(state.searchTimer); state.catalogLoading = true; state.cases = []; state.filtered = []; state.reviews = {}; error(""); clearComparison(); state.detail = null; state.caseId = ""; state.selected = null; state.dirty = false; renderQueue();
    renderRepresentation();
    $("case-workspace").hidden = true; $("case-loading").hidden = false; $("no-case").hidden = true;
    $("case-list").replaceChildren(node("div", "empty-small", isStream() ? "Loading all same-vehicle observations…" : "Loading candidate segments…"));
    updateNavigation();
    try {
      const data = await api(`/api/catalog?method=${encodeURIComponent(state.method)}&vehicle=${encodeURIComponent(state.vehicle)}${matchingQuery()}`);
      if (seq !== state.sequence) return;
      state.catalogLoading = false; state.cases = arr(data.cases); state.reviews = data.reviews || {}; state.limit = 100;
      options($("direction-filter"), [...new Set(state.cases.map(directionLabel))].sort().map((v) => [v, v]), "All directions");
      options($("date-filter"), [...new Set(state.cases.map((c) => c.date || date(c.start)))].sort().map((v) => [v, v]), "All dates");
      if (state.debug?.enabled) { $("date-filter").value = state.debug.date; $("date-filter").disabled = true; }
      options($("route-filter"), [...new Set(state.cases.flatMap((c) => arr(c.routes).map(String)))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).map((v) => [v, v]), "All routes");
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
      else { ++state.sequence; state.caseId = ""; state.detail = null; state.selected = null; state.dirty = false; $("case-workspace").hidden = true; $("case-loading").hidden = true; $("no-case").hidden = false; txt("case-title", isStream() && !isDirection() ? "No matching vehicle-day observations" : "No matching candidate segments"); txt("case-subtitle", "Adjust the filters to continue reviewing."); for (const id of ["metric-avl", "metric-apc", "metric-support", "metric-attention"]) txt(id, "—"); }
    }
  }
  function renderQueue() {
    txt("case-count", fmt(state.filtered.length));
    const reviewed = state.filtered.filter((c) => state.reviews[c.id]?.verdict && state.reviews[c.id].verdict !== "unreviewed").length;
    const percent = state.filtered.length ? Math.round(reviewed / state.filtered.length * 100) : 0;
    txt("review-progress-text", `${fmt(reviewed)} / ${fmt(state.filtered.length)} reviewed`); txt("review-progress-percent", `${percent}%`); $("review-progress").value = percent;
    const frag = document.createDocumentFragment();
    if (!state.filtered.length) frag.append(node("div", "empty-small", isStream() && !isDirection() ? "No matching vehicle-day observations." : "No matching candidate segments."));
    for (const c of state.filtered.slice(0, state.limit)) {
      const b = node("button", `case-item${c.id === state.caseId ? " active" : ""}`); b.type = "button"; b.dataset.caseId = c.id; if (c.gtfs_status) b.dataset.gtfsStatus = c.gtfs_status; b.setAttribute("role", "listitem"); b.setAttribute("aria-current", c.id === state.caseId ? "true" : "false");
      const title = node("div", "case-item-title"); title.append(node("span", "", c.kind === "vehicle_stream" ? `${c.date || date(c.start)} · Full vehicle day` : `${isDirection() ? directionLabel(c) + " · " : ""}${candidateRange(c)}`)); if (isDirection()) { b.dataset.direction = directionLabel(c); b.dataset.conflictCount = String(conflictCount(c)); }
      const review = state.reviews[c.id]?.verdict || "unreviewed"; title.append(node("span", `review-indicator ${review}`, { accepted: "✓", problem: "!", uncertain: "?", unreviewed: "" }[review])); b.append(title);
      const kind = c.kind === "direction_unresolved" ? "Unlocated direction review bucket (not a candidate trip)" : c.kind === "vehicle_stream" ? "Full-day same-vehicle observations" : c.kind === "apc_only" ? "Unassociated APC window" : c.kind === "unresolved" ? "Unresolved-membership window" : "Candidate segment";
      b.append(node("div", "case-item-meta", `${c.date || date(c.start)} · ${arr(c.routes).length ? "Route " + c.routes.join(" / ") : kind}`));
      const gpsLabel = c.kind === "vehicle_stream" ? `${fmt(c.avl_count)} AVL source rows` : c.kind === "segment" ? `${fmt(c.avl_member_count ?? c.avl_count)} member GPS` : c.window_avl_count != null ? `${fmt(c.window_avl_count)} same-window GPS` : "See window details for GPS";
      const counts = node("div", "case-item-bottom"); counts.append(node("span", "", `${gpsLabel} · ${fmt(c.apc_count)} APC`));
      const attention = ["ambiguous", "unmatched", "ineligible"].reduce((n, s) => n + (c.status_counts?.[s] || 0), 0);
      if (attention) { const s = node("span", "mini-status"); const dot = node("i", "red-bg"); s.append(dot, document.createTextNode(String(attention))); counts.append(s); } b.append(counts);
      if (isDirection()) b.append(node("div", conflictCount(c) ? "direction-conflict-badge" : "table-route-name", `Direction-conflict windows ${fmt(conflictCount(c))} · ${c.kind === "direction_unresolved" ? "Unlocated review bucket, not a candidate trip" : isGTFS() ? `GTFS ${c.gtfs_status || "Pending inspection"} (not ground truth)` : "Candidate, not matched"}`));
      if (shortCandidate(c)) b.append(node("div", "direction-conflict-badge", shortCandidateNote(c)));
      if (c.gap_split) { b.dataset.parentCaseId = c.parent_case_id; b.append(node("div", "gap-fragment-badge", c.gap_unlocated ? "Joint-gap segmentation · Time unlocated; source record retained" : `Joint-gap subsegment ${c.gap_fragment_index} / ${c.gap_fragment_count}`)); }
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
      txt("case-title", isStream() ? `${c.vehicle_label || c.vehicle || state.vehicle} · ${c.date || date(c.start)} full-day observations` : `${c.vehicle_label || c.vehicle || state.vehicle} · ${range(c.start, c.end)}`);
      txt("case-subtitle", `${c.date || date(c.start)} · ${arr(c.routes).length ? "Source routes " + c.routes.join(" / ") : "Route unknown"} · ${isStream() ? `${isDedup() ? "Exact AVL duplicates merged; " : ""}Same-vehicle grouping and time ordering only; no segmentation or matching · ` : state.debug?.enabled ? "Single-day observation slice, not trip endpoints · " : ""}${c.id || id}`);
      const windowGPS = arr(detail.avl).filter((p) => p.in_window !== false && validGPS(p)).length;
      txt("metric-avl", fmt(windowGPS)); txt("metric-avl-note", isStream() ? `Daily AVL ${isDedup() ? "retained deduplicated" : "source"} rows ${fmt(c.avl_count)} · Does not imply a match` : c.kind === "segment" ? `Segment members: ${fmt(c.avl_member_count ?? c.avl_count)} rows · Window includes context` : "Same-window evidence ≠ association · This card is not an AVL segment"); txt("metric-apc", fmt(c.apc_count)); renderDedupSummary();
      txt("metric-support", `${fmt(c.status_counts?.direct || 0)} / ${fmt(c.status_counts?.partial || 0)}`);
      txt("metric-attention", fmt(["ambiguous", "unmatched", "ineligible"].reduce((n, s) => n + (c.status_counts?.[s] || 0), 0)));
      if (isDirection()) { txt("case-kicker", `${c.kind === "direction_unresolved" ? "UNLOCATED DIRECTION REVIEW" : "DIRECTION CANDIDATE"} · ${state.method}`); txt("case-title", `${c.vehicle_label || c.vehicle || state.vehicle} · ${c.kind === "direction_unresolved" ? "Unlocated direction review window" : directionLabel(c)} · ${candidateRange(c)}`); txt("case-subtitle", `${c.date || date(c.start)} · ${c.kind === "direction_unresolved" ? "Review bucket, not a direction candidate or actual trip" : "Direction candidate, not a confirmed trip"}${shortCandidate(c) ? " · " + shortCandidateNote(c) : ""} · Direction-conflict windows ${fmt(conflictCount(c))} · ${c.id}`); txt("metric-avl-note", `Candidate AVL members ${fmt(c.avl_member_count ?? c.avl_count)} · Loaded valid GPS ${fmt(windowGPS)} (may include context)`); $("observation-conflicts-only").checked = false; renderDirectionPanel(); }
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
      if (isStream()) { state.selectedObservation = state.selected ? { source: "apc", rid: state.selected } : null; if (anchor?.observationSource === "avl") { const retained = findSourceObservation("avl", anchor.observationRid); if (retained) { state.selectedObservation = { source: "avl", rid: observationRid(retained) }; state.selected = null; if (observationRid(retained) !== anchor.observationRid) toast("The selected AVL row was an exact duplicate. Its retained representative is now selected; all original IDs remain in deduplication lineage. No APC match was inferred."); } } $("observation-search").value = ""; $("observation-source-filter").value = ""; $("observation-window-only").checked = false; renderObservations(); }
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
  function eventTimeText(value) { const t = timestamp(value); if (t == null) return "— (missing time retained)"; return new Date(t * 1000).toISOString().replace("T", " ").replace("Z", ""); }
  function sourceTimeText(row) { return row.source === "avl" ? `time ${eventTimeText(row.time)}` : `arrival ${eventTimeText(row.arrival)} → departure ${eventTimeText(row.departure)}`; }
  function observationSelected(row) { return state.selectedObservation?.source === row.source && state.selectedObservation?.rid === observationRid(row); }
  function renderDedupSummary() {
    const box = $("dedup-summary"); box.hidden = !isDedup(); box.replaceChildren(); if (!isDedup() || !state.detail) return;
    const counts = state.detail.counts || {}, c = state.detail.case || {}, original = counts.source_avl_total ?? c.source_counts?.avl, retained = counts.retained_avl_total ?? c.retained_counts?.avl, removed = counts.removed_avl_duplicates ?? c.removed_avl, apc = counts.retained_apc_total ?? c.apc_count;
    box.append(node("strong", "", "Current vehicle · Exact AVL deduplication only:"));
    for (const [id, label, value] of [["original-avl", "Original AVL", original], ["retained-avl", "Retained AVL", retained], ["removed-avl", "Merged duplicates", removed], ["apc", "All APC retained", apc]]) { const item = node("span", "dedup-count"); item.append(document.createTextNode(` ${label} `)); const number = node("strong", "", fmt(value)); number.id = `dedup-${id}`; number.dataset.count = String(value ?? ""); item.append(number, document.createTextNode(" rows ·")); box.append(item); }
    box.append(node("span", "", " Rows are merged only when all 13 original CSV fields match; identical times or coordinates alone are insufficient. Search any original source rid to find its retained representative and full lineage. Original files and earlier experiments are unchanged."));
    if (isDirection()) { box.replaceChildren(node("strong", "", "Full-day processing for this vehicle:")); for (const [key, label] of [["source_avl_total", "Original AVL"], ["deduplicated_avl_total", "AVL after exact deduplication"], ["filtered_avl_pi_po", "PI/PO excluded"], ["retained_avl_total", "Retained AVL"], ["source_apc_total", "All APC retained"]]) { const span = node("span", "dedup-count", ` ${label} ${fmt(counts[key])} rows ·`); span.dataset.countKey = key; span.dataset.count = String(counts[key] ?? ""); box.append(span); } box.append(node("span", "", ` The current candidate contains ${fmt(c.avl_count)} AVL / ${fmt(c.apc_count)} APC records. Context is not membership; records shared across candidates must not be double-counted. Original CSVs, filtered records and duplicate lineage are retained.`)); }
    if (isGTFS()) { box.replaceChildren(node("strong", "", "Route 29 · Derived review scope:"), node("span", "", ` This vehicle day includes ${fmt(counts.vehicle_observations)} source records and excludes ${fmt(counts.excluded_other_route_count)} other-route records (including 929, which is not merged with 29). Current candidate members: ${fmt(c.avl_count)} AVL / ${fmt(c.apc_count)} APC; loaded context is retained separately. The parent experiment's all-route APC retention and AVL deduplication audit remains in the earlier version; this page does not display APC for all routes.`)); }
  }
  function renderDirectionPanel() {
    renderGapPanel();
    if (!isDirection() || !state.detail) return;
    const c = state.detail.case || {}, conflicts = directionConflicts(), list = $("direction-conflict-list"); list.replaceChildren();
    txt("direction-conflict-count", `${fmt(conflicts.length)} loaded conflict windows`); txt("direction-conflict-summary", `${c.kind === "direction_unresolved" ? "Unlocated review bucket, not a direction candidate" : directionLabel(c) + " candidate"} · Conflict records are retained; orange-red does not mean matching failure`);
    txt("direction-candidate-note", `${c.kind === "direction_unresolved" ? "This is a review bucket for unlocated records, not a generated candidate trip." : `Candidate boundaries ${candidateRange(c)}; original member coverage ${range(c.source_start, c.source_end)}; first definite direction ${time(c.first_definite_at, true)}; total unknown-direction duration ${fmt(c.unknown_duration_s)} seconds.${c.scope_censored_start || c.scope_censored_end || c.scope_boundary_start || c.scope_boundary_end ? " Clipped to the day scope; these are not trip endpoints." : ""}`} An APC closed interval crossing a boundary may retain multiple candidate_segment_ids; unique membership is not forced. Context helps explain conflicts but does not automatically become a candidate member. Heading is only a source angle and is not used to determine operating direction.`);
    if (c.gap_split && !c.gap_unlocated) txt("direction-candidate-note", `Observed member coverage ${candidateRange(c)}; persisted candidate interval ${range(c.start, c.end)} (outer edges may be day truncation, not actual trip endpoints). Direction is inherited from the parent. Original parent candidate_segment_ids and current review_candidate_ids are stored separately. Only conflicts that actually overlap this subsegment are shown here; all parent conflicts remain in the audit above. Context is not automatically a subsegment member.`);
    txt("direction-rules", `${c.gap_split ? "The following are the parent experiment's direction rules; this run is further subdivided at joint APC + AVL observation gaps, see the subsegment evidence above.\n" : ""}Fixed v1: AVL Destination yields only explicit direction words; direction evidence is held forward until the next differing AVL time, for at most 30 seconds. Unknown direction and PI/PO also cut off the hold, and the last AVL record is point evidence only. APC uses forward-ordered closed arrival–departure intervals; a missing endpoint or reversed order falls back to explicitly flagged point evidence, without repairing source times. Only non-conflicting, definite direction reversals form candidate boundaries; conflicts stay in the preceding candidate, and unknown intervals do not create trips out of nothing. No spatiotemporal matcher and no APC distance projection.\n\n` + json({ rules: state.meta.direction_segmentation || state.meta.direction_rules || state.meta.segmentation_rules || currentDebugRun()?.rules, candidate: c, boundaries: state.detail.boundaries, segments: state.detail.segments }));
    txt("direction-filtered-count", `(${fmt(arr(state.detail.filtered_avl).length)} rows)`); txt("direction-filtered-json", $("direction-filter-audit").open ? json(state.detail.filtered_avl || []) : "");
    for (const conflict of conflicts) {
      const card = node("div", "direction-conflict-card"); card.dataset.conflictId = rid(conflict.id); const instant = (value) => eventTimeText(value).slice(11), interval = conflict.kind === "point" ? `${instant(conflict.start)} · Point conflict` : `${conflict.start_inclusive === false ? "(" : "["}${instant(conflict.start)}–${instant(conflict.end)}${conflict.end_inclusive === false ? ")" : "]"} · Interval`; const focus = node("button", "direction-conflict-focus", `${interval} · ${arr(conflict.types).join(" / ") || "Direction conflict"}`); focus.type = "button"; focus.dataset.focusConflict = rid(conflict.id); focus.addEventListener("click", () => focusDirectionConflict(conflict)); card.append(focus);
      const sources = node("div", "direction-conflict-sources"); for (const source of arr(conflict.source_rids)) { const row = findSourceObservation(source.source, source.rid), button = node("button", "mono", `${String(source.source).toUpperCase()} ${row ? directionLabel(row) : ""} · ${rid(source.rid)}`); button.type = "button"; button.dataset.source = source.source; button.dataset.rid = rid(source.rid); button.disabled = !row; button.title = row ? "Inspect this source record; no APC–AVL match is created" : "This source record is outside the loaded range; its full ID is retained"; button.addEventListener("click", () => { if (row) selectObservation(row); }); sources.append(button); } card.append(sources); list.append(card);
    }
    if (!conflicts.length) list.append(node("p", "stream-order-note", "No direction conflict is marked for this candidate; this does not prove its trip identity."));
  }
  function renderGapPanel() {
    const c = state.detail?.case || {}, panel = $("gap-segmentation-panel"); panel.hidden = !c.gap_split; if (!c.gap_split) return;
    panel.dataset.parentCaseId = rid(c.parent_case_id); panel.dataset.caseId = rid(c.id);
    txt("gap-fragment-title", c.gap_unlocated ? "Retained records without locatable times" : `Joint-observation-gap subsegment ${c.gap_fragment_index} / ${c.gap_fragment_count}`);
    txt("gap-parent-id", `Parent direction candidate ${c.parent_case_id}`);
    txt("gap-segmentation-note", `${c.gap_unlocated ? "These members lack valid locatable times and remain in a separate review bucket; no timed trajectory is generated." : `Current subsegment ${candidateRange(c)}; observed two-source coverage ${range(c.source_start, c.source_end)}.`} Retain the parent direction decision and split only at gaps between joint APC + AVL coverage that strictly exceed the threshold. APC arrival–departure intervals may bridge gaps; APC is not converted into AVL points. Focus includes only persisted subsegment members; context is limited to nearby windows. This is not a confirmed trip.`);
    const list = $("gap-boundary-list"); list.replaceChildren();
    for (const [side, gap] of [["before", c.gap_boundary_before], ["after", c.gap_boundary_after]]) {
      if (!gap) continue; const item = node("div", "gap-boundary"); item.dataset.side = side; item.dataset.durationSeconds = String(gap.duration_s); item.dataset.start = String(gap.start); item.dataset.end = String(gap.end);
      item.append(node("strong", "", `Joint-source gap ${side === "before" ? "before" : "after"} segment · ${fmt(gap.duration_s / 60)} minutes (${fmt(gap.duration_s)} seconds)`), node("span", "", `${eventTimeText(gap.start)} → ${eventTimeText(gap.end)}`), node("small", "", `Split condition: gap ${gap.operator || ">"} ${fmt(gap.threshold_s)} seconds; boundary source IDs are retained in the audit below.`)); list.append(item);
    }
    if (!list.children.length) list.append(node("p", "stream-order-note", c.gap_unlocated ? "Unlocated records are excluded from gap-duration calculations." : "No joint-gap boundary was added to this segment; its original parent direction boundaries are retained."));
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
    for (const conflict of directionConflicts()) { const start = timestamp(conflict.start), end = timestamp(conflict.end); if (start == null || end == null || end < a || start > b) continue; const mark = svg("rect", { x: x(Math.max(a, start)) - (start === end ? 1.5 : 0), y: dim.top, width: Math.max(3, x(Math.min(b, end)) - x(Math.max(a, start))), height: dim.height - dim.top - dim.bottom, fill: COLORS.counter, "fill-opacity": .13, stroke: COLORS.counter, "stroke-opacity": .4, "stroke-width": .8, "data-conflict-id": conflict.id, class: "svg-clickable" }); mark.append(svg("title", {}, `Direction conflict ${conflict.id} · ${arr(conflict.source_rids).map((r) => `${r.source}:${r.rid}`).join(" / ")}`)); mark.addEventListener("click", (event) => { event.stopPropagation(); focusDirectionConflict(conflict); }); g.append(mark); }
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
      const type = node("td"); type.append(node("div", "table-mono", `#${fmt(row.display_order)}`), node("span", `source-badge source-${row.source === "apc" ? "apc" : "avl"}`, String(row.source || "Unknown").toUpperCase())); tr.append(type);
      const instant = node("td"); instant.append(node("div", "table-mono", eventTimeText(row.event_time)), node("div", "table-route-name mono", observationTimeBasis(row))); tr.append(instant);
      const identity = node("td", "observation-rid mono", observationRid(row)); identity.title = observationRid(row); if (row.source === "avl" && row.deduplication?.duplicate_group_size > 1) { tr.dataset.duplicateGroupSize = String(row.deduplication.duplicate_group_size); identity.append(node("small", "dedup-source-badge", `Exact-duplicate group: ${fmt(row.deduplication.duplicate_group_size)} rows · Retained representative`)); } tr.append(identity);
      const route = node("td"); route.append(node("div", "", fmt(row.route)), node("div", "table-route-name", fmt(row.route_name)), node("div", "table-mono", `Branch ${fmt(row.branch)}`)); tr.append(route);
      if (isDirection()) { const ids = candidateIds(row), conflicts = sourceConflictIds(row.source, row), direction = node("td", "direction-source-cell"); tr.dataset.direction = directionLabel(row); tr.dataset.conflictCount = String(conflicts.length); tr.dataset.candidateIds = JSON.stringify(ids); direction.append(node("div", "", directionLabel(row)), node("small", "table-route-name", row.direction_basis || "Direction basis not provided"), node("div", "table-route-name", row.is_candidate_member ? "Member of this candidate (not a match)" : "Context / review record"), node("small", ids.length > 1 ? "direction-conflict-badge" : "table-route-name", ids.length ? `${ids.length} candidates${ids.length > 1 ? " · Non-unique membership" : ""}` : "Not located in a candidate; record retained")); if (conflicts.length) direction.append(node("small", "direction-conflict-badge", `${conflicts.length} direction-conflict windows`)); direction.title = ids.join("\n"); tr.append(direction); }
      tr.append(node("td", "table-mono", `${row.lat ?? "—"}, ${row.lon ?? "—"}`), node("td", "observation-source-time mono", sourceTimeText(row)));
      tr.addEventListener("click", () => selectObservation(row)); tr.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectObservation(row); } }); fragment.append(tr);
    }
    if (!page.length) { const tr = node("tr"), td = node("td", "empty-small", "No records match the search / source / time-window filters. Filtering does not delete source data."); td.colSpan = isDirection() ? 7 : 6; tr.append(td); fragment.append(tr); }
    $("observation-table-body").replaceChildren(fragment); txt("observation-count", `${isDirection() ? "Loaded candidate" : "Full day"}: ${fmt(observations.length)} ${isDedup() ? "retained records" : "source rows"} · All rows available by paging`);
    txt("observation-page-info", `${fmt(rows.length)} / ${fmt(observations.length)} rows match · Showing ${rows.length ? fmt(first + 1) : "0"}–${fmt(first + page.length)} · Page ${state.observationPage + 1} / ${pages}`);
    $("observation-page").value = state.observationPage + 1; $("observation-page").max = pages;
    $("observation-first").disabled = $("observation-previous").disabled = state.observationPage === 0; $("observation-last").disabled = $("observation-next").disabled = state.observationPage >= pages - 1;
    const selectedHidden = state.selectedObservation && !rows.some(observationSelected); $("observation-selection-note").hidden = !selectedHidden; txt("observation-selection-note", "The selected record is excluded by the table filters; source details and map selection remain. Clear the source / search / time-window filters to reveal it.");
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
    setBadge($("selected-status"), row ? "not_evaluated" : "Not selected");
    if (!row) { const empty = node("div", "inspector-empty"); empty.append(node("strong", "", "Select an APC or AVL source observation"), node("p", "", "Click a source record in the combined observation table. This view has no trip identity, matching score or accepted evidence.")); box.append(empty); return; }
    box.append(node("h4", "record-heading", `${String(row.source).toUpperCase()} source observation`), node("div", "record-id", observationRid(row)));
    const dl = node("dl", "record-dl"); dlPair(dl, "Source display ordinal", row.display_order); dlPair(dl, "event_time", eventTimeText(row.event_time)); dlPair(dl, "Time basis", observationTimeBasis(row)); dlPair(dl, "VehicleID", row.vehicle); dlPair(dl, "Route", row.route); dlPair(dl, "RouteName", row.route_name); dlPair(dl, "Branch", row.branch); dlPair(dl, "Location", `${row.lat ?? "—"}, ${row.lon ?? "—"}`);
    if (row.source === "apc") { dlPair(dl, "Source arrival", eventTimeText(row.arrival)); dlPair(dl, "Source departure", eventTimeText(row.departure)); dlPair(dl, "Stop / direction", `${fmt(row.stop)} / ${fmt(row.direction)}`); dlPair(dl, "Boarding / alighting", `${fmt(row.boarding)} / ${fmt(row.alighting)}`); dlPair(dl, "Arrival / departure load", `${fmt(row.arrival_load)} / ${fmt(row.departure_load)}`); }
    else { dlPair(dl, "Source location time", eventTimeText(row.time)); dlPair(dl, "Speed km/h", row.speed); dlPair(dl, "Run", row.run); dlPair(dl, "Destination", row.destination); }
    if (isDirection()) { dlPair(dl, "Candidate operating direction", directionLabel(row)); dlPair(dl, "Direction evidence basis", row.direction_basis); dlPair(dl, "Direction evidence interval", `${eventTimeText(row.direction_evidence_start)} → ${eventTimeText(row.direction_evidence_end)} · ${fmt(row.direction_interval_kind)}`); dlPair(dl, "Original Heading", row.heading ?? row.source_fields?.heading); dlPair(dl, "Current membership", row.is_candidate_member ? "Member of this candidate, not an APC–AVL match" : "Context / unlocated review record"); dlPair(dl, "Candidate IDs (possibly multiple)", candidateIds(row).length ? candidateIds(row).join("\n") : "No direction candidate located; source record retained"); dlPair(dl, "Crosses candidate boundary", row.candidate_boundary_overlap ? "Yes · Unique membership is not forced" : "Not flagged"); dlPair(dl, "Direction conflict IDs", sourceConflictIds(row.source, row).join("\n") || "No flagged conflicts"); }
    if (state.detail.case?.gap_split) { dlPair(dl, "Original parent direction candidate IDs", arr(row.candidate_segment_ids).join("\n") || "No parent candidate annotation on the source record"); dlPair(dl, "Current review_candidate_ids", candidateIds(row).join("\n") || "Not a member of the current subsegment catalog"); dlPair(dl, "Subsegment membership basis", "Persisted source_rids; not inferred from parent candidate_segment_ids"); }
    box.append(dl, node("div", "reason-box", isDirection() ? "This is a direction-segmentation candidate, not a confirmed trip. Direction / Destination provide operating-direction evidence; Heading is not used for segmentation. No APC–AVL spatiotemporal matching or APC progress projection is performed. Direction conflicts and multiple memberships remain unchanged." : "Grouped by vehicle ID only: no spatiotemporal matching, trip segmentation or APC progress projection. The same vehicle, time or coordinates do not automatically indicate the same stop visit."));
    if (isGTFS()) {
      box.querySelector(".reason-box").textContent = `${state.detail.case?.gap_split ? "Source records and parent direction annotations are retained. Current subsegment membership is defined strictly by source_rids; context is not membership." : "Source records and direction-candidate memberships are unchanged."} GTFS plots use independent reference projections / APC stop evidence, not APC progress fabricated from nearest AVL points. OFF_REFERENCE records do not enter s–t curves or valid support.`;
      const evidence = (row.source === "avl" ? gtfsProjection() : gtfsAPCEvidence()).filter((p) => rid(p.rid || p.source_rid) === observationRid(row)), originalProjection = row.source === "avl" ? arr(gtfsData().projection?.points).filter((p) => rid(p.rid || p.source_rid) === observationRid(row)) : [], audit = node("details", "gtfs-source-evidence"); audit.open = true; audit.append(node("summary", "", `GTFS ${row.source === "avl" ? "reference-projection" : "stop-interval"} evidence · ${evidence.length} items`), node("pre", "mono", json({ reference_evidence: evidence, source_projection_diagnostics: originalProjection }))); box.append(audit);
    }
    if (row.source === "avl" && row.deduplication) {
      const group = row.deduplication, trace = node("details", "dedup-trace"); trace.id = "dedup-source-trace"; trace.open = group.duplicate_group_size > 1; trace.dataset.representativeRid = rid(group.representative_rid); trace.dataset.groupSize = String(group.duplicate_group_size);
      trace.append(node("summary", "", `AVL deduplication lineage · ${fmt(group.duplicate_group_size)} original rows → 1 retained row`), node("p", "stream-order-note", "Group by exact equality of all 13 original CSV fields and retain the smallest source_row. The list includes representatives and merged duplicates; source row numbers and original IDs are unchanged."));
      const ids = node("pre", "mono", observationAliases(row).map((id, index) => `${id === rid(group.representative_rid) ? "Retained" : "Merged duplicate"} · source_row ${arr(group.source_rows)[index] ?? "—"} · ${id}`).join("\n")); ids.id = "dedup-source-rids"; trace.append(ids); box.append(trace);
    }
    const raw = node("details"); raw.open = row.source === "avl"; raw.append(node("summary", "", "Complete source fields (display encoding)"), node("p", "stream-order-note", "Large integers are shown as decimal strings to preserve precision. Times use the original local-time axis; source files are unchanged."), node("pre", "mono", json(row.source_fields || row))); box.append(raw);
  }
  function renderInspector() {
    if (isStream()) { renderSourceInspector(); return; }
    const box = $("inspector"), r = selectedAPC(); box.replaceChildren();
    if (!r) { setBadge($("selected-status"), "Not selected"); const e = node("div", "inspector-empty"); e.append(node("span", "empty-crosshair", "⌖"), node("strong", "", "No APC observation selected"), node("p", "", "This segment may contain only AVL. Enable the context layer to view nearby records.")); box.append(e); return; }
    const d = decision(r); setBadge($("selected-status"), rowStatus(r));
    box.append(node("h4", "record-heading", `Stop ${fmt(r.stop)}`), node("div", "record-id", `APC ${rid(r.rid)}`));
    const dl = node("dl", "record-dl");
    dlPair(dl, "Original arrival", `${date(r.arrival)} ${time(r.arrival, true)}`); dlPair(dl, "Original departure", `${date(r.departure)} ${time(r.departure, true)}`);
    dlPair(dl, "Route / direction", `${fmt(r.route)} / ${fmt(r.direction)}`); dlPair(dl, "RouteName", r.route_name ?? r.RouteName ?? r.source?.RouteName); dlPair(dl, "Branch", r.branch ?? r.Branch ?? r.source?.Branch); dlPair(dl, "Source route consistency", { concordant: "Labels agree (route identity not confirmed)", mixed: "GPS support includes multiple route labels", conflict: "APC / GPS route labels disagree", unknown: "Unknown" }[d.route_state] || d.route_state); dlPair(dl, "Support extent", LEVEL[r.support_level || d.support_level] || r.support_level || d.support_level);
    dlPair(dl, "Evidence type", { point: "Observed GPS point", interpolation: "Adjacent-GPS interpolation (derived)", mixed: "Observed points + derived interpolation" }[d.support_kind] || d.support_kind);
    dlPair(dl, "Segment context", { single_segment: "Single segment", multiple_segments: "Multiple segments · Membership unconfirmed", unmapped: "Unmapped", no_assignment: "No assignment" }[d.context_status] || d.context_status);
    dlPair(dl, "Current relationship", RELATION[r.relation] || r.relation); dlPair(dl, "Score / margin", `${fmt(d.score)} / ${fmt(d.margin)} (not a probability)`);
    dlPair(dl, "Candidate visit count", d.candidate_count ?? d.episode_count); dlPair(dl, "Boarding / alighting", `${fmt(r.boarding)} / ${fmt(r.alighting)}`);
    dlPair(dl, "Arrival / departure load", `${fmt(r.arrival_load)} / ${fmt(r.departure_load)}`);
    dlPair(dl, "APC coordinates", validCoords(r) ? `${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}` : "Invalid or missing (not placed on map)"); box.append(dl);
    box.append(node("div", "reason-box", `Decision reason: ${r.reason || d.reason || "Not provided"}${d.grain_uncertain ? " · APC observation granularity is unconfirmed" : ""}${d.interval_excursion ? " · Observations move away from the stop inside the interval" : ""}${d.coverage_start_missing || d.coverage_end_missing ? " · Source coverage boundaries are incomplete" : ""}`));
    witnessList(box, arr(d.witnesses), false); witnessList(box, arr(d.counter_witnesses), true);
    const candidates = arr(d.candidates);
    if (candidates.length) { const det = node("details"); det.append(node("summary", "", `All retained candidates (${candidates.length})`), node("pre", "mono", json(candidates))); box.append(det); }
    const raw = node("details"); raw.append(node("summary", "", "Complete decision and source IDs"), node("pre", "mono", json(d))); box.append(raw);
  }
  function witnessList(box, witnesses, counter) {
    box.append(node("div", "evidence-label", `${counter ? "Spatiotemporal counterevidence" : "Observed supporting evidence"} · ${witnesses.length}`));
    if (!witnesses.length) { box.append(node("div", "muted", counter ? "No retained counterevidence (does not imply none exists)" : "No accepted positive supporting evidence")); return; }
    for (const w of witnesses) {
      const item = node("div", `witness-item${counter ? " counter" : ""}`);
      item.append(node("strong", "", `${w.kind === "interpolation" ? "◇ Derived interpolation" : "● Observed point"} · ${{ arrival: "Arrival", departure: "Departure", interior: "Interior" }[w.target] || w.target || "Observation"}`));
      item.append(node("div", "", `${time(w.support_time, true)} · Distance ${fmt(w.distance_m)} m · Time difference ${fmt(w.gap_s)} s${w.kind === "interpolation" ? ` · GPS bracket ${fmt(w.bracket_gap_s)} s` : ""}`));
      item.append(node("div", "mono", `GPS ${arr(w.ping_ids).map(rid).join(" · ") || "—"}`));
      const all = new Map(arr(state.detail?.avl).map((p) => [rid(p.rid), p]));
      if (arr(w.ping_ids).some((id) => all.get(rid(id))?.in_window === false)) item.append(node("div", "", "Includes real source GPS outside the display window; inspect the complete decision IDs."));
      if (counter) item.append(node("div", "", "Counterevidence depends on uncorrected clocks and current motion constraints; it does not prove a source-data error.")); box.append(item);
    }
  }
  function renderTable() {
    if (!state.detail) return; const q = $("apc-search").value.trim().toLowerCase();
    const rows = arr(state.detail.apc).filter((r) => !q || `${r.rid} ${r.stop} ${r.route} ${r.route_name || r.source?.RouteName || ""} ${r.branch || ""} ${r.direction} ${r.reason || decision(r).reason} ${STATUS[rowStatus(r)]}`.toLowerCase().includes(q)); state.tableRows = rows;
    txt("table-count", `${rows.length} rows${arr(state.detail.apc).some((r) => r.in_focus === false) ? " · Includes context" : ""}`);
    const frag = document.createDocumentFragment(), displayed = rows.slice(0, 1000);
    const selectedRow = rows.find((r) => rid(r.rid) === state.selected);
    if (selectedRow && !displayed.includes(selectedRow)) displayed[displayed.length - 1] = selectedRow;
    const scrollTop = $("apc-table-body").closest(".table-scroll").scrollTop;
    for (const r of displayed) {
      const tr = node("tr", `${rid(r.rid) === state.selected ? "selected " : ""}${r.in_focus === false ? "table-context" : ""}`); tr.dataset.rid = rid(r.rid); tr.tabIndex = 0; tr.setAttribute("aria-selected", String(rid(r.rid) === state.selected));
      const stop = node("td"); stop.append(node("div", "table-stop", `Stop ${fmt(r.stop)}`), node("div", "table-rid mono", rid(r.rid))); tr.append(stop);
      tr.append(node("td", "table-mono", `${time(r.arrival, true)} → ${time(r.departure, true)}`));
      const routeCell = node("td"); routeCell.append(node("div", "", `${fmt(r.route)} · ${fmt(r.direction)}`), node("div", "table-route-name", fmt(r.route_name ?? r.source?.RouteName)), node("div", "table-mono", `Branch ${fmt(r.branch ?? r.source?.Branch)}`)); tr.append(routeCell);
      const statusCell = node("td"); const badge = node("span"); setBadge(badge, rowStatus(r)); statusCell.append(badge); tr.append(statusCell);
      tr.append(node("td", "table-mono", `${fmt(r.boarding)} / ${fmt(r.alighting)}`), node("td", "table-mono", `${fmt(r.arrival_load)} / ${fmt(r.departure_load)}`), node("td", "", RELATION[r.relation] || r.relation || "—"));
      tr.addEventListener("click", () => selectAPC(r.rid)); tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectAPC(r.rid); } }); frag.append(tr);
    }
    if (!rows.length) { const tr = node("tr"), td = node("td", "empty-small", "No APC records match the filters."); td.colSpan = 7; tr.append(td); frag.append(tr); }
    $("apc-table-body").replaceChildren(frag); $("apc-table-body").closest(".table-scroll").scrollTop = scrollTop;
    const selectedHidden = state.selected != null && !selectedRow;
    $("table-limit").hidden = rows.length <= 1000 && !selectedHidden;
    txt("table-limit", selectedHidden ? "The selected APC row does not match the table search. Clear the search to reveal it. The map, curves and evidence panel retain the selection." : "Up to 1,000 rows are shown for responsiveness, including the selected row if it matches the search. Narrow the search to see more; charts are not limited by this row cap.");
  }
  function renderWarnings() {
    const warnings = arr(state.detail?.warnings).map((w) => typeof w === "string" ? w : json(w));
    if (isStream()) { warnings.unshift(isDirection() ? "Only direction candidate trips are defined; actual trips are not confirmed. Conflicts are counted as time windows, not independent events. APC multiple memberships and unlocated records are retained." : "Daily APC / AVL is grouped by source VehicleID only: no trip identity, stop matching, scoring or accepted evidence.", isDedup() ? `Only AVL duplicates identical across all 13 original CSV fields are merged${isDirection() ? ", and AVL Route=PI/PO is excluded" : ""}; all APC is retained. Representatives and merged source rid values remain traceable. Retained rows use display_order; equal event_time values do not establish real-world ordering.` : "All source and duplicate records are retained by display_order. Stable ordering at the same event_time does not establish their real-world order.", "APC progress remains unknown and is not projected using the nearest AVL point. The AVL curve shows observed cumulative distance within continuous visualization fragments, not trip completion.", "Adjacent-day context is missing at both day edges; day truncation and plot gaps are not trip boundaries."); $("case-warnings").replaceChildren(...warnings.map((w) => node("li", "", w))); txt("case-provenance", json({ run_id: state.run || state.meta?.run_id, representation: state.meta?.representation || "vehicle_observation_stream", method: state.method, case_id: state.caseId, observations: arr(state.detail?.observations).length, counts: state.detail?.counts, avl_deduplication: isDedup(), observation_matching_used: false, trip_segmentation_used: isDirection(), time_basis: state.meta?.time_basis, provenance: state.detail?.provenance })); return; }
    if (!warnings.length) warnings.push("No additional window warnings were supplied; no warnings does not mean an association is verified.");
    warnings.push("APC granularity, source clocks and actual trip identity still require external verification. Human reviews are saved separately and do not overwrite source decisions.", "Distance accumulates only within continuous valid GPS fragments and resets at breaks. Display windows are viewing slices; source segments are not resegmented.", "Context records may recur in adjacent candidates. Do not sum this view's passenger counts across trips.");
    if (state.debug?.enabled) warnings.unshift(`${state.debug.date} is a fixed single-day source-evidence slice. Day-edge truncation is not a trip start or end; missing cross-day context cannot be automatically reconstructed.`);
    if (state.detail?.case?.date_clipped_start || state.detail?.case?.date_clipped_end || state.detail?.date_clipping?.start || state.detail?.date_clipping?.end) warnings.unshift("This window is clipped to the date scope; distinguish original observation times from display boundaries.");
    $("case-warnings").replaceChildren(...warnings.map((w) => node("li", "", w)));
    txt("case-provenance", json({ run_id: state.meta?.run_id, method: state.method, case_id: state.caseId, window: state.detail?.window, counts: state.detail?.counts, time_basis: state.meta?.time_basis, provenance: state.detail?.provenance }));
  }
  function loadReview(review) { state.revision = review.revision || 0; state.dirty = false; $("review-notes").value = review.notes || ""; const v = Object.hasOwn(VERDICT, review.verdict) ? review.verdict : "unreviewed"; document.querySelector(`#review-verdict input[value="${v}"]`).checked = true; txt("review-save-state", review.updated_at ? `Saved · ${VERDICT[v]}` : VERDICT[v]); $("review-save-state").classList.remove("dirty"); }
  function dirty() { if (!state.detail) return; state.dirty = true; txt("review-save-state", "Unsaved changes"); $("review-save-state").classList.add("dirty"); }
  async function saveReview(next = false) {
    if (!state.detail || state.saving) return; const identity = { method: state.method, vehicle: state.vehicle, case_id: state.caseId, ...(state.debug?.enabled ? { run_id: state.run } : {}) };
    const verdict = document.querySelector("#review-verdict input:checked")?.value || "unreviewed", notes = $("review-notes").value;
    const nextId = state.filtered[state.filtered.findIndex((c) => c.id === state.caseId) + 1]?.id;
    state.saving = true; $("save-review").disabled = true; $("save-next").disabled = true; txt("review-save-state", "Saving…");
    try {
      const saved = await api("/api/reviews", { method: "POST", headers: { "Content-Type": "application/json", "X-Review-Token": state.meta.csrf_token }, body: JSON.stringify({ ...identity, verdict, notes, revision: state.revision }) });
      if (identity.case_id === state.caseId && identity.method === state.method && identity.vehicle === state.vehicle && (!state.debug?.enabled || identity.run_id === state.run)) {
        state.reviews[state.caseId] = saved; state.revision = saved.revision || state.revision + 1;
        const changedDuringSave = $("review-notes").value !== notes || document.querySelector("#review-verdict input:checked")?.value !== verdict;
        if (!changedDuringSave) loadReview(saved); else dirty(); renderQueue();
        toast(changedDuringSave ? "The submitted version was saved; newer changes remain unsaved." : isStream() ? "Review notes saved; source observations are unchanged and no matching was performed." : "Review saved. Source data and merge results are unchanged.");
        if (next && !changedDuringSave && nextId) { filterCases(false); await loadCase(nextId); }
        else if (next && !nextId) toast(isStream() && !isDirection() ? "Saved. This is the last vehicle-day item in the current filter." : "Saved. This is the last candidate review item in the current filter.");
      }
    } catch (e) { txt("review-save-state", "Save failed · Changes retained"); error(e.status === 409 ? "This review was modified in another window. Your notes remain here. Copy them before reloading, merge the changes and save again." : e.message); }
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
      if ($("layer-pings").checked || isDirection() && sourceConflictIds("avl", p).length) { const c = svg("circle", { cx: xy[0], cy: xy[1], r: isDirection() && sourceConflictIds("avl", p).length ? 3.5 : 2.1, fill: sourceColor("avl", p), opacity: p.in_focus === false ? .5 : .8 }); c.append(svg("title", {}, `GPS ${p.rid} · ${time(p.t, true)}${isDirection() && sourceConflictIds("avl", p).length ? " · Direction conflict" : ""}`)); if (isStream()) { c.setAttribute("class", "svg-clickable"); c.setAttribute("data-rid", p.rid); c.addEventListener("click", (event) => { event.stopPropagation(); if (state.mapDragged) return; const row = arr(state.detail.observations).find((r) => r.source === "avl" && observationRid(r) === rid(p.rid)); if (row) selectObservation(row); }); } frag.append(c); }
    }
    for (const r of visibleAPC()) { if (!validCoords(r)) continue; const xy = project(r.lat, r.lon), chosen = rid(r.rid) === state.selected;
      if (chosen) frag.append(svg("circle", { cx: xy[0], cy: xy[1], r: 13, fill: COLORS.apc, opacity: .12, stroke: COLORS.apc, "stroke-width": 1 }));
      const circle = svg("circle", { cx: xy[0], cy: xy[1], r: chosen ? 6 : 4.1, fill: sourceColor("apc", r), stroke: "#fff", "stroke-width": chosen ? 2 : 1.2, opacity: r.in_focus === false && !chosen ? .45 : .95, class: `svg-clickable${chosen ? " selected" : ""}`, "data-selected": String(chosen), "data-rid": rid(r.rid), tabindex: 0, "aria-label": `APC ${r.rid} Stop ${fmt(r.stop)} ${STATUS[rowStatus(r)]}` });
      circle.append(svg("title", {}, `APC ${r.rid} · Stop ${fmt(r.stop)} · ${range(r.arrival, r.departure)} · ${STATUS[rowStatus(r)]}`));
      circle.addEventListener("click", (e) => { if (!state.mapDragged) { e.stopPropagation(); selectAPC(r.rid); } }); circle.addEventListener("keydown", (e) => { if (e.key === "Enter") selectAPC(r.rid); }); frag.append(circle);
    }
    const selected = selectedAPC(); if (selected && !isStream()) { drawMapWitnesses(frag, selected, arr(decision(selected).witnesses), false); if ($("layer-counters").checked) drawMapWitnesses(frag, selected, arr(decision(selected).counter_witnesses), true); }
    const rawSelected = isStream() ? selectedObservationRow() : null;
    if (rawSelected?.source === "avl" && validCoords(rawSelected) && (!state.view || (timestamp(rawSelected.event_time) >= state.view[0] && timestamp(rawSelected.event_time) <= state.view[1]))) { const xy = project(rawSelected.lat, rawSelected.lon), ring = svg("circle", { cx: xy[0], cy: xy[1], r: 10, fill: "none", stroke: "#506d8a", "stroke-width": 3, "data-source-rid": observationRid(rawSelected), "pointer-events": "none" }); ring.append(svg("title", {}, "Selected source AVL location; not associated with APC")); frag.append(ring); }
    const nearest = nearestGPS(state.cursor); if (nearest) { const p = project(nearest.lat, nearest.lon); frag.append(svg("circle", { cx: p[0], cy: p[1], r: 7, fill: "#fff", stroke: "#254c3c", "stroke-width": 2, "pointer-events": "none" }), svg("circle", { cx: p[0], cy: p[1], r: 2.5, fill: "#254c3c", "pointer-events": "none" })); }
    if (!data.some(validGPS) && !visibleAPC().some(validCoords)) frag.append(svg("text", { x: w / 2, y: h / 2, class: "chart-empty" }, "No usable geographic coordinates in this window"));
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
  function renderWindowControls() { const [a, b] = bounds(), [start, end] = sourceBounds(); syncWindowSize(); txt("window-label", `${date(a)} ${time(a, true)}–${time(b, true)}${date(a) !== date(b) ? " · Across days" : ""}${state.view ? " · Partial view" : isStream() && !isDirection() ? " · Full-day window" : " · Full loaded window"}`); $("window-previous").disabled = !state.view || a <= start; $("window-next").disabled = !state.view || b >= end; $("window-focus").disabled = !selectedAPC(); renderTimelinePan(); }
  function renderTimelinePan() {
    if (!state.detail) return;
    const [start, end] = sourceBounds(), [a, b] = bounds(), size = b - a, movable = !!state.view && end - start > size + .001, control = $("timeline-pan"), title = isStream() && !isDirection() ? "Browse the full day horizontally" : "Browse the loaded time range";
    txt("timeline-pan-title", title); control.setAttribute("aria-label", title); control.disabled = !movable;
    // Keep short windows reachable even across a full day: slider keyboard steps
    // must not leap over more than one tenth of the currently visible duration.
    const fractionStep = movable ? Math.min(1, 1000 * size / 10 / (end - start - size)) : 1;
    control.step = String(Math.max(.000001, 10 ** Math.floor(Math.log10(fractionStep))));
    control.value = movable ? (a - start) / (end - start - size) * 1000 : 0;
    for (const [key, value] of Object.entries({ viewStart: a, viewEnd: b, domainStart: start, domainEnd: end, windowSeconds: size })) control.dataset[key] = String(value);
    control.setAttribute("aria-valuetext", `${eventTimeText(a)} to ${eventTimeText(b)}${movable ? "; move the visible window left or right" : "; full range displayed"}`);
    txt("timeline-pan-label", `${date(start)} · Current ${time(a, true)}–${time(b, true)}${date(a) !== date(b) ? " (next day)" : ""} · ${movable ? `${fmt(size / 60)}-minute window` : isStream() && !isDirection() ? "Full-day overview displayed" : "Full loaded range displayed"}`);
    $("timeline-pan-detail").hidden = movable || end - start <= 60; txt("timeline-pan-detail", `Use a ${end - start > 1800 ? "30" : end - start > 600 ? "10" : "1"}-minute detail window`);
    txt("timeline-pan-help", `${movable ? "Drag horizontally or use Home / End / arrow keys to move the viewing window. The cursor keeps its relative position; the map, curves and current-window-only table stay synchronized." : "The full range is displayed. Select a detail window to pan horizontally."} Green marks are AVL source times; blue-gray bars are APC arrival–departure intervals. Dense overlap does not imply continuous coverage. The lower slider controls only the time cursor.`);
    const root = $("timeline-pan-overview"), width = root.parentElement.clientWidth || 700, height = 83, left = 39, right = 14, x = (t) => left + (t - start) / (end - start) * (width - left - right), g = document.createDocumentFragment();
    root.setAttribute("viewBox", `0 0 ${width} ${height}`); root.dataset.viewStart = String(a); root.dataset.viewEnd = String(b);
    root.setAttribute("aria-label", `${title}: green AVL times, blue-gray APC intervals; current window ${range(a, b)}`);
    for (let i = 0; i <= 4; i++) { const t = start + (end - start) * i / 4, xx = x(t); g.append(svg("line", { x1: xx, x2: xx, y1: 8, y2: 57, class: "chart-grid" }), svg("text", { x: xx, y: 74, class: "chart-axis", "text-anchor": i === 4 ? "end" : i === 0 ? "start" : "middle" }, i === 4 && date(t) !== date(start) ? `${time(t)} +1 day` : time(t))); }
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
  function applyWindowSize(seconds) { if (!state.detail) return; const [start, end] = sourceBounds(); if (seconds == null) state.view = null; else { if (!Number.isFinite(seconds) || seconds < .001) { toast("Window length must be positive, in seconds or minutes."); return; } const size = Math.min(seconds, end - start), at = Math.max(start, Math.min(state.cursor ?? start, end - size)); state.view = size >= end - start ? null : [at, at + size]; } refreshWindow(); }
  function shiftWindow(direction) { if (!state.view) return; const [start, end] = sourceBounds(), size = state.view[1] - state.view[0], at = Math.max(start, Math.min(end - size, state.view[0] + direction * size)); state.view = [at, Math.min(end, at + size)]; state.cursor = at; refreshWindow(); }
  function focusSelected() { const r = selectedAPC(); if (!r) return; const t = timestamp(r.arrival) ?? timestamp(r.departure); if (!finite(t)) return; const [start, end] = sourceBounds(); state.view = [Math.max(start, t - 300), Math.min(end, t + 300)]; if (state.view[1] <= state.view[0]) state.view = null; $("window-size").value = "600"; state.cursor = t; refreshWindow(); }
  function dimensions(root, height) { const width = root.parentElement.clientWidth || 700; root.setAttribute("viewBox", `0 0 ${width} ${height}`); return { width, height, left: width < 500 ? 44 : 64, right: 23, top: 18, bottom: 30 }; }
  function axes(g, dim, a, b) { const x = (t) => dim.left + (t - a) / (b - a) * (dim.width - dim.left - dim.right); const ticks = dim.width < 500 ? 4 : 7; for (let i = 0; i < ticks; i++) { const t = a + (b - a) * i / (ticks - 1), xx = x(t); g.append(svg("line", { x1: xx, x2: xx, y1: dim.top, y2: dim.height - dim.bottom, class: "chart-grid" }), svg("text", { x: xx, y: dim.height - 11, class: "chart-axis", "text-anchor": "middle" }, time(t, b - a < 600))); } return x; }
  function nearestGPS(t) { if (!finite(t)) return null; let best = null, distance = Infinity; for (const p of rowsAVL()) if (validGPS(p)) { const d = Math.abs(timestamp(p.t) - t); if (d < distance) { best = p; distance = d; } } return best; }
  function updateCursor() { const [a, b] = bounds(); state.cursor = Math.min(b, Math.max(a, state.cursor ?? a)); $("time-scrubber").value = Math.round((state.cursor - a) / (b - a) * 1000); txt("scrubber-time", time(state.cursor, true)); const nearest = nearestGPS(state.cursor); txt("cursor-label", nearest ? `Nearest observed GPS ${time(nearest.t, true)} · Difference ${fmt(Math.abs(timestamp(nearest.t) - state.cursor))} s` : "No observed GPS in this window; vehicle positions are not fabricated"); }
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
      const tick = svg("line", { x1: x(t), x2: x(t), y1: 30, y2: 38, stroke: sourceColor("avl", p), "stroke-width": 1, opacity: .65, "data-source": "avl", "data-rid": p.rid }); tick.append(svg("title", {}, `AVL ${p.rid} · ${eventTimeText(p.t)} · Original location time`)); if (isGTFS()) gtfsMarkSource(tick, "avl", p); g.append(tick); prev = p;
    }
    let lanes = Array(5).fill(-Infinity); const events = visibleAPC().slice().sort((p, q) => (timestamp(p.arrival) ?? Infinity) - (timestamp(q.arrival) ?? Infinity));
    for (const r of events) { let start = timestamp(r.arrival), end = timestamp(r.departure); if (start == null && end == null) continue; start ??= end; end ??= start; const lo = Math.min(start, end), hi = Math.max(start, end); if (hi < a || lo > b) continue;
      let lane = lanes.findIndex((last) => last < lo); if (lane < 0) lane = lanes.indexOf(Math.min(...lanes)); lanes[lane] = hi; const selected = rid(r.rid) === state.selected, y = 61 + lane * 8;
      const bar = svg("rect", { x: x(Math.max(a, lo)) - (lo === hi ? 2 : 0), y, width: Math.max(4, x(Math.min(b, hi)) - x(Math.max(a, lo))), height: 6, rx: 2, fill: sourceColor("apc", r), opacity: selected ? 1 : r.in_focus === false ? .3 : .58, stroke: selected ? "#3d5d48" : "none", "stroke-width": selected ? 1.5 : 0, class: `svg-clickable${selected ? " selected" : ""}`, "data-selected": String(selected), "data-rid": rid(r.rid), "data-source": "apc" });
      bar.append(svg("title", {}, `APC ${r.rid} · Stop ${fmt(r.stop)} · ${range(r.arrival, r.departure)}`)); bar.addEventListener("click", (e) => { e.stopPropagation(); selectAPC(r.rid); }); g.append(bar);
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
      mark.setAttribute("class", `svg-clickable${chosen ? " selected" : ""}`); mark.setAttribute("data-selected", String(chosen)); mark.setAttribute("data-rid", rid(r.rid)); mark.setAttribute("opacity", chosen ? 1 : .55); mark.append(svg("title", {}, `APC ${r.rid} · ${witness.kind === "interpolation" ? "Derived interpolated distance" : "Observed GPS support location"} · ${fmt(distance)} m`)); mark.addEventListener("click", (e) => { e.stopPropagation(); selectAPC(r.rid); }); g.append(mark);
    }
    if (!distances.length) g.append(svg("text", { x: d.width / 2, y: 100, class: "chart-empty" }, "No usable observed cumulative distance in this window · APC progress is not extrapolated"));
    drawCursor(g, d, x); root.replaceChildren(g);
  }
  // GTFS coordinates come only from the persisted reference / evidence adapter.
  // Never derive APC s from a nearby AVL ping or connect different fragments.
  const GTFS_COLORS = ["#426aab", "#aa6cbd", "#cc8744"];
  const GTFS_DIAGNOSTIC_COLORS = ["#b47435", "#a45569", "#83744a"];
  const GTFS_STATUS = { MATCHED: "MATCHED · Algorithm-selected", AMBIGUOUS: "AMBIGUOUS · Multiple candidates", PATTERN_ONLY: "PATTERN_ONLY · Pattern only", UNMATCHED: "UNMATCHED · No acceptable candidate", BOUNDARY_UNCERTAIN: "BOUNDARY_UNCERTAIN · Boundary uncertain" };
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
    const label = svg("text", { x: right, y: 12, "text-anchor": "end", fill: "#267e6d", "font-size": 9 }, `${MATCHING_MODES[state.matchingMode]} · Dark: decision evidence; faint: context only`); bands.append(label); root.append(bands);
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
  const gtfsCandidateLabel = (c) => c.diagnostic_only ? `Diagnostic ${c.diagnostic_rank || gtfsDiagnosticCandidates().indexOf(c) + 1}` : `Candidate ${gtfsCandidates().indexOf(c) + 1}`;
  const gtfsPrecise = (value) => finite(value) ? value.toLocaleString("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 6 }) : "Unknown";
  const gtfsGateValue = (value, unit) => value == null ? "Unknown" : Array.isArray(value) ? `[${value.map((item) => gtfsGateValue(item, unit)).join(", ")}]` : unit === "fraction" ? finite(value) ? `${gtfsPrecise(value * 100)}%` : "Unknown" : typeof value === "number" ? gtfsPrecise(value) : String(value);
  const gtfsValid = (row) => ["VALID", "ON_REFERENCE", "VALID_PROJECTION", "STOP_INTERVAL_ONLY", "VALID_STOP", "STOP_MATCHED"].includes(String(row.status || row.projection_status || "").toUpperCase());
  const gtfsOnReference = (row) => rid(row.reference_id) === state.gtfsReference && row.ambiguous !== true && gtfsValid(row) && gtfsS(row) != null;
  function drawGTFSMap(group) {
    const reference = gtfsReferences().find((r) => gtfsReferenceId(r) === state.gtfsReference); let previous = null;
    for (const p of arr(reference?.geometry)) { if (!validCoords(p)) { previous = null; continue; } if (previous) group.append(mapLine(project(previous.lat, previous.lon), project(p.lat, p.lon), { stroke: "#627aa6", "stroke-width": 2, "stroke-dasharray": "6 4", opacity: .5, "data-reference-id": state.gtfsReference })); previous = p; }
    const [a, b] = bounds();
    for (const p of gtfsProjection()) { if (String(p.status).toUpperCase() !== "OFF_REFERENCE" || timestamp(p.t) < a || timestamp(p.t) > b || rid(p.reference_id) !== state.gtfsReference) continue; const raw = validCoords(p) ? p : findSourceObservation("avl", p.rid || p.source_rid); if (!raw || !validCoords(raw)) continue; const xy = project(raw.lat, raw.lon), mark = svg("circle", { cx: xy[0], cy: xy[1], r: 5, fill: "none", stroke: "#d18b4f", "stroke-width": 2, "data-status": "OFF_REFERENCE", "data-reference-id": p.reference_id }); mark.append(svg("title", {}, `OFF_REFERENCE · ${p.rid} · ${fmt(p.projection_distance_m)} m · Diagnostic only; excluded from s–t curves`)); gtfsMarkSource(mark, "avl", p); group.append(mark); }
  }
  function appendGTFSContinuousEvidence(card, candidate) {
    const components = [
      ["absolute_delay_level_cost", "Absolute delay level"],
      ["conditioned_fit_cost", "Fit conditioned on shared delay"],
      ["delay_dynamics_cost", "Delay dynamics"],
      ["APC_stop_time_cost", "APC stop-time consistency"],
      ["projection_and_coverage_cost", "Projection and coverage"],
      ["pattern_progress_cost", "Route pattern and progress"],
      ["vehicle_sequence_cost", "Vehicle-day sequence"],
    ];
    if (candidate.occurrence_model_enabled) components.push(["occurrence_displacement_cost", "Local occurrence displacement"]);
    card.dataset.scorer = candidate.scorer;
    card.append(node("p", "gtfs-continuous-method", "Continuous scheduled-passage scoring; no resampled-GTFS DTW distance"));
    const scroll = node("div", "gtfs-continuous-table-scroll"), table = node("table", "gtfs-score-components"), head = node("thead"), header = node("tr"), body = node("tbody");
    table.setAttribute("aria-label", candidate.occurrence_model_enabled ? "Eight continuous time–space score components" : "Seven continuous time–space score components");
    for (const label of ["Score component", "Raw cost", "Weight", "Weighted contribution"]) header.append(node("th", "", label)); head.append(header); table.append(head);
    for (const [key, label] of components) {
      const row = node("tr"); row.dataset.component = key;
      row.append(node("th", "", label));
      for (const value of [candidate.score_components_unweighted?.[key], candidate.score_component_weights?.[key], candidate.score_components?.[key]]) { const cell = node("td", "mono", gtfsPrecise(value)); cell.title = finite(value) ? String(value) : "Unknown"; row.append(cell); }
      body.append(row);
    }
    table.append(body); scroll.append(table); card.append(scroll);
    const coverageScroll = node("div", "gtfs-continuous-table-scroll"), coverageTable = node("table", "gtfs-continuous-coverage"), coverageHead = node("thead"), coverageHeader = node("tr"), coverageBody = node("tbody");
    coverageTable.setAttribute("aria-label", "Three-tier evidence coverage");
    for (const label of ["Evidence coverage tier", "Observation-point ratio", "Adjacent-interval duration ratio"]) coverageHeader.append(node("th", "", label)); coverageHead.append(coverageHeader); coverageTable.append(coverageHead);
    for (const [key, label, definition] of [["projection", "Projection coverage", "Valid projected evidence / all spatial evidence"], ["schedule", "Schedule alignment coverage", "Schedule-supported valid evidence / valid projected evidence"], ["end_to_end", "End-to-end evidence coverage", "Schedule-supported valid evidence / all spatial evidence"]]) {
      const row = node("tr"); row.dataset.coverage = key; row.title = definition; row.append(node("th", "", label));
      for (const metric of ["point_ratio", "duration_ratio"]) { const value = candidate.coverage_metrics?.[key]?.[metric]; const cell = node("td", "mono", finite(value) ? `${fmt(value * 100)}%` : "Unknown"); cell.dataset.metric = metric; cell.title = finite(value) ? String(value) : "Unknown"; row.append(cell); }
      coverageBody.append(row);
    }
    coverageTable.append(coverageBody); coverageScroll.append(coverageTable); card.append(coverageScroll);
    card.append(node("p", "gtfs-continuous-note", "Coverage is not accuracy. Adjacent intervals use physical time and fragment boundaries; sustained OFF_REFERENCE runs and GPS gaps are not bridged."));
    const delay = node("dl", "gtfs-continuous-delay"), prior = ({ independently_validated_history: "Independently validated historical delay distribution", local_scheduled_headway: "Local scheduled headway (uncalibrated prior)", uncalibrated_operational_defaults: "Asymmetric operational delay mixture prior (uncalibrated)", independently_validated_operational_hierarchy: "Independently validated hierarchical operational delay prior" })[candidate.delay_prior_source] || "Not provided";
    const process = arr(candidate.whole_alignment?.delay_process), headways = process.map((point) => point.headway_s).filter((value) => finite(value) && value > 0), stored = candidate.headway_summary_s;
    const low = stored?.min ?? (headways.length ? headways.reduce((a, b) => Math.min(a, b)) : null), high = stored?.max ?? (headways.length ? headways.reduce((a, b) => Math.max(a, b)) : null);
    for (const [label, value] of [["Shared delay median", finite(candidate.estimated_delay_s) ? `${fmt(candidate.estimated_delay_s)} s` : "Unknown"], ["Delay uncertainty scale (not a calibrated confidence interval)", finite(candidate.delay_uncertainty_s) ? `${fmt(candidate.delay_uncertainty_s)} s` : "Unknown"], ["Delay-prior source", prior], ["Local scheduled headway range", finite(low) && finite(high) ? `${fmt(low)}–${fmt(high)} s` : "Unknown"], ["APC-supported stops", fmt(candidate.apc_support?.supported_stops)], ["Consecutive supporting windows", fmt(candidate.continuous_support_windows)]]) { delay.append(node("dt", "", label), node("dd", "", value)); }
    card.append(delay, node("p", "gtfs-continuous-note", candidate.occurrence_model_enabled ? "Operational delay prior, occurrence displacement and candidate retrieval are separate; rescue inclusion does not imply equal prior probability." : "Charts retain original scheduled times. The delay prior is normalized by headway or independent history, not a fixed hourly penalty."));
    if (candidate.occurrence_model_enabled) {
      const evidence = candidate.occurrence_displacement || {}, context = evidence.context || {}, details = node("dl", "gtfs-occurrence-evidence");
      for (const [label, value] of [["Operational prior model", candidate.delay_prior_source || "Unknown"], ["Local passage gap source", context.headway_source || "Unknown"], ["Local passage gap confidence", gtfsPrecise(context.headway_confidence)], ["Previous passage gap / seconds", gtfsPrecise(context.previous_headway_s)], ["Next passage gap / seconds", gtfsPrecise(context.next_headway_s)], ["Competing scheduled passages crossed", gtfsPrecise(evidence.crossed_occurrences)], ["Distance outside occurrence cell / seconds", gtfsPrecise(evidence.outside_cell_s)]]) details.append(node("dt", "", label), node("dd", "mono", value));
      const guard = candidate.occurrence_decision_guard;
      if (guard) details.append(node("dt", "", "Occurrence identity guard diagnostics"), node("dd", "mono", JSON.stringify(guard)));
      card.append(details);
    }
  }
  function initGTFS() {
    state.gtfsDecisionAVL = new Set(arr(gtfsData().decision_selection?.selected_avl_ids)); state.gtfsDecisionAPC = new Set(arr(gtfsData().decision_selection?.selected_apc_ids));
    $("direction-candidate-note").append(document.createTextNode(state.detail.case?.gap_split ? " Only Route 29 is shown. Parent direction candidates are further split at joint APC + AVL observation gaps, which subsegments do not cross. Direction labels still come from the parent experiment and may reflect other-route evidence (including 929); direction was not independently re-inferred. Original candidate_segment_ids are parent annotations; review_candidate_ids define current subsegment membership." : " Only Route 29 is shown. Direction boundaries are inherited from the parent and may still reflect other-route evidence (including 929); this is not independent Route 29 resegmentation."));
    state.gtfsVisible = new Set((gtfsCandidates().length ? gtfsCandidates() : gtfsDiagnosticCandidates()).map(gtfsCandidateId));
    state.gtfsColors = new Map([...gtfsCandidates().map((c, i) => [gtfsCandidateId(c), GTFS_COLORS[i % GTFS_COLORS.length]]), ...gtfsDiagnosticCandidates().map((c, i) => [gtfsCandidateId(c), GTFS_DIAGNOSTIC_COLORS[i % GTFS_DIAGNOSTIC_COLORS.length]])]);
    // Choose from this case's stored primary projection, never the preceding case's selected axis.
    const references = gtfsReferences(), projection = gtfsData().projection, primaryPoints = Array.isArray(projection) ? projection : arr(projection?.points), preferred = rid(gtfsCandidates()[0]?.reference_id || gtfsDiagnosticCandidates()[0]?.reference_id || primaryPoints.find((p) => gtfsValid(p))?.reference_id); state.gtfsReference = references.some((r) => gtfsReferenceId(r) === preferred) ? preferred : gtfsReferenceId(references[0] || {});
    renderGTFSPanel(); renderMatchingControls();
  }
  function renderGTFSPanel() {
    if (!isGTFS()) return; const data = gtfsData(), result = data.result || {}, candidates = gtfsCandidates(), status = result.status || result.decision || "UNKNOWN", continuous = result.scorer === "continuous_passage_v1";
    txt("gtfs-result-status", GTFS_STATUS[status] || `${status} · Status not declared`); $("gtfs-result-status").dataset.status = status; $("gtfs-result-status").className = `status-badge gtfs-status-${rid(status).toLowerCase()}`;
    txt("gtfs-candidate-count", `${candidates.length} final candidates`); $("gtfs-candidate-panel").dataset.candidateCount = String(candidates.length);
    txt("gtfs-result-summary", `${MATCHING_MODES[state.matchingMode]} · Reference date 2023-11-06 · Route 29 · ${result.calibration_status || result.calibration?.status || result.calibration?.calibration_status || "UNCALIBRATED"}${candidates.length > 3 ? " · Warning: candidate count exceeds the 0–3 output contract" : ""}`);
    txt("gtfs-uncertainty-note", continuous ? "Continuous time–space costs are not probabilities. Final scheduled candidates require both an independently calibrated credible set and absolute-quality gates; uncalibrated results appear only as diagnostics." : `Calibrated probability: ${result.probability == null ? "unknown" : "see raw diagnostics; scores alone cannot establish it"}. Candidate scores are not probabilities; MATCHED is an experimental algorithm decision, not a verified trip identity.${arr(data.warnings || result.warnings).map((warning) => ` ${warning}`).join(";")}`);
    const summary = $("gtfs-evidence-summary"); summary.replaceChildren();
    for (const [key, label, value] of [["pattern_support", "Pattern support score (not a probability)", result.pattern_confidence?.support_score], ["trip_support", continuous ? "Calibrated occurrence probability" : "Trip support score (not a probability)", continuous ? result.trip_confidence?.calibrated_probability : result.trip_confidence?.support_score], ["margin", continuous ? "Runner-up minus best cost (not a probability)" : "Top1–Top2 support-score margin", result.top1_top2_margin], ["delay", "Estimated model time shift / seconds", result.estimated_delay], ["windows", "Supporting windows (overlaps cannot be added)", result.supporting_window_count]]) {
      const item = node("div", "diff-stat"); item.dataset.metric = key;
      const display = key === "margin" ? finite(value) ? value !== 0 && Math.abs(value) < 0.000001 ? value.toExponential(4) : value.toLocaleString("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 6 }) : "Unknown" : fmt(value);
      const number = node("strong", "", display); if (key === "margin") number.title = finite(value) ? String(value) : "Unknown";
      item.append(number, node("span", "", label)); summary.append(item);
    }
    const list = $("gtfs-candidate-list"), diagnosticList = $("gtfs-diagnostic-list"), diagnostics = gtfsDiagnosticCandidates(); list.replaceChildren(); diagnosticList.replaceChildren();
    for (const [i, c] of gtfsAllCandidates().entries()) {
      const diagnostic = c.diagnostic_only === true, continuousCandidate = c.scorer === "continuous_passage_v1", id = gtfsCandidateId(c, i), labelText = gtfsCandidateLabel(c), card = node("div", diagnostic ? "gtfs-diagnostic-card" : "gtfs-candidate-card"); card.dataset.candidateId = id; card.dataset.tripId = rid(c.trip_id); card.dataset.diagnosticOnly = String(diagnostic); card.dataset.referenceId = rid(c.reference_id); card.style.setProperty("--candidate-color", gtfsColor(c));
      const label = node("label", diagnostic ? "gtfs-diagnostic-toggle" : "gtfs-candidate-toggle"), checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.gtfsVisible.has(id); checkbox.dataset.candidateId = id; checkbox.dataset.tripId = rid(c.trip_id); checkbox.setAttribute("aria-label", `Show ${labelText} ${c.trip_id || id}`);
      checkbox.addEventListener("change", () => { if (checkbox.checked) state.gtfsVisible.add(id); else state.gtfsVisible.delete(id); renderCharts(); renderMap(); });
      label.append(checkbox, node("strong", "", `${labelText} · ${c.trip_id || id}`));
      const color = node("input", diagnostic ? "gtfs-diagnostic-color" : "gtfs-candidate-color"); color.type = "color"; color.value = gtfsColor(c); color.dataset.candidateId = id; color.setAttribute("aria-label", `${labelText} color`); color.addEventListener("input", () => { state.gtfsColors.set(id, color.value); card.style.setProperty("--candidate-color", color.value); renderCharts(); renderMap(); });
      const heading = node("div", "gtfs-candidate-heading"); heading.append(label, color); card.append(heading);
      if (diagnostic) card.append(node("p", "gtfs-diagnostic-warning", continuousCandidate ? "Quality or calibrated credible-set gate not met · Diagnostic only, excluded from final scheduled candidates" : "Below threshold · Diagnostic only; excluded from final candidates, status and probability"));
      if (!diagnostic && typeof c.decision_gate?.absolute_cost_passed === "boolean") {
        const passed = c.decision_gate.absolute_cost_passed, gate = node("p", `gtfs-cost-gate${passed ? "" : " gtfs-cost-rejected"}`, passed ? "Cost gate passed (still a candidate, not a verified trip)" : "Near-equivalent competitor (above cost gate; retained for ambiguity checks)");
        card.dataset.absoluteCostPassed = String(passed); card.dataset.candidateRole = c.decision_gate.role || ""; gate.dataset.absoluteCostPassed = String(passed); gate.title = `${c.decision_gate.role || "Final-decision cost gate"} · Threshold ${fmt(c.decision_gate.absolute_cost_threshold)}; passing the cost gate does not establish verified trip identity.`; card.append(gate);
      }
      card.append(node("p", "", `${c.route_short_name || c.route_id || "29"} · ${c.trip_headsign || c.headsign || "Headsign not declared"} · reference ${c.reference_id || "Not provided"}`));
      const score = c.score ?? c.total_score, scoreLine = node("p", "gtfs-candidate-score", continuousCandidate ? "" : `Uncalibrated score ${gtfsPrecise(score)} · Probability ${c.probability == null ? "unknown" : "not presented as calibrated"}`); if (continuousCandidate) scoreLine.append(node("span", "", "Continuous time–space cost (lower is better)"), node("span", "mono", ` ${gtfsPrecise(score)} · `), node("span", "", "Calibrated probability"), node("span", "mono", c.calibrated === true && finite(c.probability) ? ` ${gtfsPrecise(c.probability)}` : " Unknown")); scoreLine.title = finite(score) ? String(score) : "Unknown"; card.append(scoreLine);
      if (continuousCandidate) appendGTFSContinuousEvidence(card, c);
      if (!continuousCandidate && finite(c.coverage)) card.append(node("p", "", `Valid AVL record coverage ${fmt(c.coverage * 100)}% (not time coverage or accuracy) · Consecutive supporting windows ${fmt(c.continuous_support_windows)}`));
      if (finite(c.estimated_delay_s)) card.append(node("p", "", `Model time shift ${fmt(c.estimated_delay_s)} s · Charts retain original GTFS scheduled times; both source clocks are unchanged`));
      if (!continuousCandidate && finite(c.estimated_delay_s) && finite(data.config?.score_delay_prior_weight)) {
        const weight = data.config.score_delay_prior_weight, penalty = weight * Math.abs(c.estimated_delay_s) / 3600;
        const line = node("p", "gtfs-delay-penalty", `Time-shift penalty ${gtfsPrecise(penalty)} · Weight ${gtfsPrecise(weight)} / hour (symmetric for early and late shifts)`);
        line.dataset.weight = String(weight); line.dataset.penalty = String(penalty); line.title = "This is only the absolute time-shift term in the final score. The same weight also affects delay fitting and the boundary model."; card.append(line);
      }
      if (diagnostic) {
        const gates = node("ul", "gtfs-failed-gates");
        for (const gate of arr(c.failed_gate_checks)) { const item = node("li", "", `${gate.label || gate.metric}: ${gtfsGateValue(gate.actual, gate.unit)}; required ${gate.operator === "in" ? "∈" : gate.operator || ""} ${gtfsGateValue(gate.threshold, gate.unit)}`); item.dataset.metric = rid(gate.metric); item.dataset.actual = gate.actual == null ? "" : String(gate.actual); item.dataset.threshold = gate.threshold == null ? "" : String(gate.threshold); item.dataset.passed = String(gate.passed); item.title = json(gate); gates.append(item); }
        if (!gates.childElementCount) gates.append(node("li", "", "No specific rejection gates were returned; this must not be interpreted as a pass.")); card.append(gates);
      }
      const referenceButton = node("button", "subtle-button gtfs-candidate-reference", "View this reference axis"); referenceButton.type = "button"; referenceButton.disabled = !gtfsReferences().some((r) => gtfsReferenceId(r) === rid(c.reference_id)); referenceButton.addEventListener("click", () => { state.gtfsReference = rid(c.reference_id); renderGTFSPanel(); renderCharts(); renderMap(); }); card.append(referenceButton);
      const detail = node("details"); detail.append(node("summary", "", "Candidate provenance, time windows and score components"), node("pre", "", json(c))); card.append(detail); (diagnostic ? diagnosticList : list).append(card);
    }
    if (!candidates.length) list.append(node("p", "stream-notice", "This direction candidate has no final GTFS trip candidate. Below-threshold candidates below are for diagnostic comparison only and do not change this abstention."));
    const diagnosticSummary = data.diagnostic_candidate_summary || {}; txt("gtfs-diagnostic-count", `${diagnostics.length} items`); $("gtfs-diagnostic-candidates").dataset.candidateCount = String(diagnostics.length);
    txt("gtfs-diagnostic-note", `Candidates with whole-trip scores that failed a threshold are shown from lowest to highest original score, excluding those already displayed above. ${fmt(diagnosticSummary.total_failed)} failed; ${fmt(diagnosticSummary.already_displayed_failed)} are retained in the final list; ${diagnostics.length} are shown here. Retrieved NO_EVIDENCE items without valid scores are not assigned fabricated scores. ${candidates.length ? "Diagnostic curves are off by default; select them to compare." : "No final candidates are available, so diagnostic curves are overlaid by default; this does not mean the match is accepted."}`);
    if (!diagnostics.length) diagnosticList.append(node("p", "stream-notice", data.diagnostic_candidates ? "No additional scored, below-threshold candidates are available." : "This version provides no additional diagnostic candidates; original results are unchanged."));
    const refs = gtfsReferences(); options($("gtfs-reference-select"), refs.map((r) => [gtfsReferenceId(r), `${r.label || r.direction_label || r.direction || "Canonical"} · ${gtfsReferenceId(r)}`])); $("gtfs-reference-select").value = state.gtfsReference; $("gtfs-reference-select").disabled = refs.length < 2;
    txt("gtfs-reference-note", "Only the same reference_id is overlaid; switching candidates never mixes s coordinates from different reference axes.");
    const projection = gtfsProjection(), valid = projection.filter(gtfsOnReference), off = projection.filter((p) => String(p.status).toUpperCase() === "OFF_REFERENCE");
    txt("gtfs-coverage", `Selected reference: ${fmt(valid.length)} valid AVL projections; ${fmt(off.length)} OFF_REFERENCE points are diagnostic only. ${fmt(gtfsAPCEvidence().filter((p) => rid(p.reference_id) === state.gtfsReference && p.ambiguous === true).length)} ambiguous APC stop-position candidates are not treated as unique s support. See source evidence below for coverage, NO_EVIDENCE windows and thresholds; missing evidence does not imply no service.`);
    renderGTFSWindows(); txt("gtfs-diagnostics", json({ matching_mode: data.matching_mode, decision_selection: data.decision_selection, result, diagnostic_candidate_summary: data.diagnostic_candidate_summary, diagnostic_candidates: diagnostics, evidence_coverage: result.evidence_coverage, windows: data.windows, projection_summary: Array.isArray(data.projection) ? undefined : data.projection, apc_evidence_summary: Array.isArray(data.apc_evidence) ? undefined : data.apc_evidence, references: refs.map(({ geometry, ...r }) => r), off_reference_runs: data.off_reference_runs, config: data.config, warnings: data.warnings }));
  }
  const gtfsWindowQuality = (w) => w.quality_by_reference?.[state.gtfsReference] || w.quality || w;
  function renderGTFSWindows() {
    const windows = gtfsWindows(), filter = $("gtfs-window-filter").value, rows = windows.filter((w) => !filter || gtfsWindowQuality(w).status === filter), list = $("gtfs-window-list"); list.replaceChildren();
    txt("gtfs-window-count", `· ${rows.length} / ${windows.length} multiscale windows, not independent events`);
    for (const w of rows) { const q = gtfsWindowQuality(w), status = q.status || "Not declared", button = node("button", "gtfs-window-row"); button.type = "button"; button.dataset.windowId = rid(w.id || w.window_id); button.dataset.status = status; button.append(node("strong", "", `${w.kind || "window"} · ${time(w.start, true)}–${time(w.end, true)} · ${status}`), node("span", "", `Valid points ${fmt(q.point_count)} · Observed duration ${fmt(q.observed_duration_s)} s · Observed forward progress ${fmt(q.observed_progress_m)} m`), node("small", "", arr(q.reasons).join(" · ") || "Source weights are normalized across scales; overlapping windows are not independent repeated evidence"));
      const coverage = w.quality_by_reference?.[state.gtfsReference] || w;
      if (finite(coverage.valid_projection_point_ratio) || finite(coverage.valid_projection_duration_ratio)) button.append(node("small", "", `Valid-point ratio ${finite(coverage.valid_projection_point_ratio) ? fmt(coverage.valid_projection_point_ratio * 100) + "%" : "Unknown"} · Valid-duration ratio ${finite(coverage.valid_projection_duration_ratio) ? fmt(coverage.valid_projection_duration_ratio * 100) + "%" : "Unknown"} · Not accuracy`));
      button.addEventListener("click", () => { const [start, end] = sourceBounds(), a = Math.max(start, timestamp(w.start) ?? start), b = Math.min(end, timestamp(w.end) ?? end); if (b <= a) { toast("This evidence window is outside the currently loaded source range; its original interval remains traceable."); return; } state.view = [a, b]; state.cursor = a; $("gtfs-windows-panel").dataset.selectedWindow = rid(w.id || w.window_id); refreshWindow(); }); list.append(button); }
    if (!rows.length) list.append(node("p", "stream-order-note", windows.length ? "No evidence windows match the filter." : "The backend returned no locatable windows; continuous coverage cannot be inferred."));
  }
  function gtfsMarkSource(mark, source, row) {
    mark.setAttribute("data-source", source); mark.setAttribute("data-rid", rid(row.rid || row.source_rid)); mark.setAttribute("class", "svg-clickable");
    mark.addEventListener("click", (event) => { event.stopPropagation(); const original = findSourceObservation(source, row.rid || row.source_rid); if (original) selectObservation(original); else toast("This source record is not in the loaded window; its complete source rid remains in the GTFS diagnostics."); });
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
      for (const p of trajectory) { const t = timestamp(p.t), mark = svg("line", { x1: x(t), x2: x(t), y1: y - 6, y2: y + 6, stroke: color, "stroke-width": p.kind === "departure" ? 2 : 1, "stroke-dasharray": diagnostic ? "2 2" : null, "data-trip-id": c.trip_id, "data-candidate-id": gtfsCandidateId(c), "data-stop-id": p.stop_id, "data-diagnostic-only": String(diagnostic), "data-reference-id": c.reference_id }); mark.append(svg("title", {}, `${gtfsCandidateLabel(c)} · ${c.trip_id} · ${p.stop_id} · ${p.kind} · ${eventTimeText(p.t)}${diagnostic ? " · Below threshold; diagnostic only" : ""}`)); marks.append(mark);
        if (previous && p.kind === "departure" && previous.stop_id === p.stop_id && t >= timestamp(previous.t)) marks.append(svg("line", { x1: x(timestamp(previous.t)), x2: x(t), y1: y, y2: y, stroke: color, "stroke-width": 6, "stroke-dasharray": diagnostic ? "2 3" : null, opacity: .5, "data-kind": "scheduled-dwell", "data-candidate-id": gtfsCandidateId(c), "data-diagnostic-only": String(diagnostic), "data-reference-id": c.reference_id })); previous = p;
      }
    }
    if (!candidates.length) g.append(svg("text", { x: d.width / 2, y: 108, class: "chart-empty" }, "No GTFS candidates are displayed; scheduled times are not fabricated"));
    txt("gtfs-schedule-caption", `${candidates.filter((c) => !c.diagnostic_only).length} final / ${candidates.filter((c) => c.diagnostic_only).length} diagnostic candidates visible · Dash-dot lines show below-threshold schedules for diagnosis only`); drawCursor(g, d, x); root.replaceChildren(g);
  }
  function renderGTFSProgress() {
    const root = $("gtfs-progress-svg"), d = dimensions(root, 285), [a, b] = bounds(), g = document.createDocumentFragment(), x = axes(g, d, a, b);
    root.dataset.referenceId = state.gtfsReference || "";
    const points = gtfsProjection().filter(gtfsOnReference), evidence = gtfsAPCEvidence().filter(gtfsOnReference), candidates = gtfsVisibleCandidates().filter((c) => rid(c.reference_id) === state.gtfsReference), reference = gtfsReferences().find((r) => gtfsReferenceId(r) === state.gtfsReference);
    const values = [...points.map(gtfsS), ...evidence.map(gtfsS), ...candidates.flatMap((c) => gtfsTrajectory(c).map(gtfsS)), ...arr(reference?.geometry).map(gtfsS)].filter(finite), lo = values.length ? Math.min(...values) : 0, hi = values.length ? Math.max(...values) : 1, span = Math.max(hi - lo, 1), y = (s) => d.height - d.bottom - (s - lo) / span * (d.height - d.top - d.bottom);
    for (let i = 0; i < 4; i++) { const s = lo + span * i / 3; g.append(svg("line", { x1: d.left, x2: d.width - d.right, y1: y(s), y2: y(s), class: "chart-grid" }), svg("text", { x: d.left - 8, y: y(s) + 3, class: "chart-axis", "text-anchor": "end" }, fmt(s / 1000))); }
    g.append(svg("text", { x: d.left - 8, y: 11, class: "chart-label", "text-anchor": "end" }, "s · km"));
    const marks = gtfsClip(g, d, "gtfs-progress-clip");
    for (const c of candidates) { let previous = null; for (const p of gtfsTrajectory(c)) { const t = timestamp(p.t), s = gtfsS(p); if (t == null || s == null) { previous = null; continue; } if (previous && t >= timestamp(previous.t)) { const line = mapLine([x(timestamp(previous.t)), y(gtfsS(previous))], [x(t), y(s)], { stroke: gtfsColor(c), "stroke-width": p.stop_id === previous.stop_id ? 3.5 : 1.6, "stroke-dasharray": c.diagnostic_only ? "2 4" : p.stop_id === previous.stop_id ? null : "5 3", opacity: .75, "data-candidate-id": gtfsCandidateId(c), "data-trip-id": c.trip_id, "data-reference-id": c.reference_id, "data-diagnostic-only": String(c.diagnostic_only === true), "data-kind": p.stop_id === previous.stop_id ? "scheduled-dwell" : "scheduled-trajectory" }); line.append(svg("title", {}, `${gtfsCandidateLabel(c)} · ${c.trip_id} · GTFS schedule, not observed${c.diagnostic_only ? " · Below threshold; diagnostic only" : ""}`)); marks.append(line); } previous = p; } }
    let previous = null;
    // Iterate all projection records so invalid / OFF_REFERENCE points break paths.
    for (const p of gtfsProjection()) {
      const t = timestamp(p.t); if (!gtfsOnReference(p) || t == null) { previous = null; continue; }
      if (previous && p.fragment_id != null && rid(p.fragment_id) === rid(previous.fragment_id) && t >= timestamp(previous.t) && !p.break_before) marks.append(mapLine([x(timestamp(previous.t)), y(gtfsS(previous))], [x(t), y(gtfsS(p))], { stroke: COLORS.avl, "stroke-width": 1.8, "data-fragment-id": p.fragment_id, "data-source": "avl", "data-from-rid": previous.rid || previous.source_rid, "data-to-rid": p.rid || p.source_rid }));
      if (t >= a && t <= b) { const mark = svg("circle", { cx: x(t), cy: y(gtfsS(p)), r: 2.6, fill: COLORS.avl, "data-fragment-id": p.fragment_id, "data-status": p.status, "data-reference-id": p.reference_id }); mark.append(svg("title", {}, `AVL ${p.rid} · ${eventTimeText(p.t)} · s=${fmt(gtfsS(p))} m · fragment ${p.fragment_id} · Projection distance ${fmt(p.projection_distance_m)} m`)); gtfsMarkSource(mark, "avl", p); marks.append(mark); } previous = p;
    }
    for (const p of evidence) { const start = timestamp(p.arrival ?? p.start), end = timestamp(p.departure ?? p.end) ?? start; if (start == null || end < start || end < a || start > b) continue; const mark = svg("line", { x1: x(start), x2: x(end) + .7, y1: y(gtfsS(p)), y2: y(gtfsS(p)), stroke: COLORS.apc, "stroke-width": 4, opacity: .8, "data-reference-id": p.reference_id, "data-status": p.status, "data-kind": "apc-stop-interval" }); mark.append(svg("title", {}, `APC ${p.rid} · stop ${p.stop_id} · ${eventTimeText(start)} → ${eventTimeText(end)} · Original stop-reference s=${fmt(gtfsS(p))} m`)); gtfsMarkSource(mark, "apc", p); marks.append(mark); }
    const validVisible = points.filter((p) => timestamp(p.t) >= a && timestamp(p.t) <= b).length;
    txt("gtfs-progress-note", `reference ${state.gtfsReference || "not provided"} · ${fmt(validVisible)} valid AVL points in the current window; green: valid projection; yellow: APC stop arrival–departure evidence; colored dashed lines: schedules; short dash-dot lines: below-threshold diagnostic schedules only. ${validVisible ? "Paths break at fragment_id changes; no lines bridge invalid / OFF_REFERENCE records." : "No valid AVL projection in this window. APC evidence is retained separately; this does not imply the vehicle did not operate."}${candidates.length < gtfsVisibleCandidates().length ? " Candidates on other reference axes are not overlaid; switch the reference above." : ""}`);
    if (!state.gtfsReference || !values.length) g.append(svg("text", { x: d.width / 2, y: 130, class: "chart-empty" }, "No usable canonical s evidence; source records remain available for review"));
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
    $("map-basemap").addEventListener("click", () => { if (!state.basemap && !window.confirm("Enabling the street map requests tiles for the current map area from OpenStreetMap at tile.openstreetmap.org. The third party will receive your IP address, browser information and an inferable map viewport. No APC/AVL source records or review notes will be sent. Enable for this page session only?")) return; state.basemap = !state.basemap; $("map-basemap").setAttribute("aria-pressed", String(state.basemap)); $("map-attribution").hidden = !state.basemap; txt("map-mode", state.basemap ? "OSM street map · Drag to pan / Scroll to zoom" : "Offline coordinate map · Drag to pan / Scroll to zoom"); renderMap(); });
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
