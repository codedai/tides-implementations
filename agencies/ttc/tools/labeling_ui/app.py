"""Inspection-first labeling UI for r1-spike scheduled_trip cases.

Run:
    streamlit run app.py -- --cases path/to/labeling_packet_redacted.csv \
                            --artifacts path/to/artifacts

See README.md for defaults, bias controls, and artifact assumptions.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

import polars as pl
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from labeling import APP_VERSION, keyboard  # noqa: E402
from labeling.adapters import discover, load_cases, load_run_report  # noqa: E402
from labeling.parsing import (  # noqa: E402
    duration_bucket,
    fmt_duration_s,
    fmt_local,
    parse_anchor_status,
    parse_chunk_duration_s,
    redact_summary,
)
from labeling.plots import fig_map, fig_stop_ladder, fig_time_distance, fig_timeline  # noqa: E402
from labeling.resolver import CaseBundle, build_bundle  # noqa: E402
from labeling.store import (  # noqa: E402
    FRAGMENT_IMPRESSIONS,
    RATIONALE_FLAGS,
    LabelRecord,
    LabelStore,
)

st.set_page_config(page_title="scheduled_trip labeling", layout="wide", page_icon="🚌")


# ---------------------------------------------------------------------------
# configuration


def _default_artifacts_root() -> Path | None:
    # tools/labeling_ui -> agencies/ttc/artifacts
    cand = APP_DIR.parents[1] / "artifacts"
    return cand if cand.is_dir() else None


def _default_cases(artifacts_root: Path | None) -> Path | None:
    if artifacts_root is None:
        return None
    packets = sorted(
        artifacts_root.glob("*/labeling_packet_redacted.csv"),
        key=lambda p: p.stat().st_mtime,
    )
    return packets[-1] if packets else None


def get_config() -> dict:
    # pinned per session: the "latest packet" default must not silently flip
    # to a regenerated run mid-review
    if "app_config" in st.session_state:
        return st.session_state["app_config"]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cases")
    parser.add_argument("--artifacts")
    parser.add_argument("--labels-dir")
    parser.add_argument("--reviewer")
    args, _ = parser.parse_known_args(sys.argv[1:])

    artifacts = (
        args.artifacts
        or os.environ.get("LABELING_UI_ARTIFACTS")
        or _default_artifacts_root()
    )
    cases = (
        args.cases
        or os.environ.get("LABELING_UI_CASES")
        or _default_cases(Path(artifacts) if artifacts else None)
    )
    labels_dir = (
        args.labels_dir
        or os.environ.get("LABELING_UI_LABELS_DIR")
        or str(APP_DIR / "labels")
    )
    reviewer = args.reviewer or os.environ.get("LABELING_UI_REVIEWER") or ""
    cfg = {
        "cases": str(cases) if cases else None,
        "artifacts": str(artifacts) if artifacts else None,
        "labels_dir": str(labels_dir),
        "reviewer": reviewer,
    }
    st.session_state["app_config"] = cfg
    return cfg


@st.cache_resource(show_spinner=False)
def cached_store(cases: str, artifacts: str | None):
    return discover(cases, artifacts)


@st.cache_resource(show_spinner=False)
def cached_cases(cases: str, artifacts: str | None) -> pl.DataFrame | None:
    return load_cases(cached_store(cases, artifacts))


@st.cache_resource(show_spinner="assembling case evidence…")
def cached_bundle(cases: str, artifacts: str | None, case_id: str) -> CaseBundle:
    df = cached_cases(cases, artifacts)
    row = df.filter(pl.col("case_id") == case_id).row(0, named=True)
    return build_bundle(cached_store(cases, artifacts), row)


# ---------------------------------------------------------------------------
# small helpers


def show_table(df: pl.DataFrame | list[dict]) -> None:
    """Render a compact table as markdown.

    Deliberately avoids st.dataframe: its pandas->pyarrow serialization
    segfaults in this environment (polars + pyarrow + protobuf loaded in the
    same script thread). The tables here are small, so markdown is fine.
    """
    rows = df.to_dicts() if isinstance(df, pl.DataFrame) else df
    if not rows:
        st.caption("(empty)")
        return
    cols = list(rows[0].keys())

    def cell(v) -> str:
        s = "" if v is None else str(v)
        return s.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    lines += ["| " + " | ".join(cell(r.get(c)) for c in cols) + " |" for r in rows]
    st.markdown("\n".join(lines))


def safe_plot(builder, *args, empty_msg: str, **kwargs) -> None:
    """Never let one broken panel take down the app."""
    try:
        fig = builder(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — degradation by design
        st.warning(f"plot failed: {exc}")
        return
    if fig is None:
        st.info(empty_msg)
    else:
        st.plotly_chart(fig, use_container_width=True)


def fmt_m(v: float | None) -> str:
    return "n/a" if v is None else f"{v:,.0f} m"


def fmt_frac(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def fmt_signed_s(v: int | None) -> str:
    if v is None:
        return "n/a"
    return ("+" if v >= 0 else "−") + fmt_duration_s(abs(v))


def case_duration(case: dict) -> int | None:
    return parse_chunk_duration_s(case.get("summary"))


def neutral_label_text(rec: LabelRecord | None) -> str:
    """The reviewer's own choice, phrased without revealing whether it
    matched the model's hidden pick (MATCH_TOP vs MATCH_OTHER stays in the
    persisted files only)."""
    if rec is None:
        return "—"
    if rec.label in ("MATCH_TOP_CANDIDATE", "MATCH_OTHER_CANDIDATE"):
        if rec.selected_candidate_letter:
            return f"matched card {rec.selected_candidate_letter}"
        return "matched a candidate"
    return {
        "NO_SCHEDULED_MATCH": "no scheduled match",
        "AMBIGUOUS": "ambiguous",
        "INSUFFICIENT_EVIDENCE": "insufficient evidence",
    }.get(rec.label, rec.label)


# ---------------------------------------------------------------------------
# state


def init_state(cfg: dict) -> None:
    ss = st.session_state
    ss.setdefault("session_id", uuid.uuid4().hex[:12])
    ss.setdefault("reviewer", cfg["reviewer"])
    ss.setdefault("current_case_id", None)
    ss.setdefault("revealed_cases", set())


def order_and_filter_cases(
    df: pl.DataFrame, labeled_ids: set[str]
) -> pl.DataFrame:
    ss = st.session_state
    out = df.with_columns(
        pl.col("summary")
        .map_elements(parse_chunk_duration_s, return_dtype=pl.Int64)
        .alias("duration_s")
    ).with_columns(
        pl.col("duration_s")
        .map_elements(duration_bucket, return_dtype=pl.String)
        .alias("bucket"),
        pl.col("case_id").is_in(sorted(labeled_ids)).alias("labeled"),
    )
    if ss.get("f_status") == "unlabeled":
        out = out.filter(~pl.col("labeled"))
    elif ss.get("f_status") == "labeled":
        out = out.filter(pl.col("labeled"))
    if ss.get("f_stratum"):
        out = out.filter(pl.col("stratum").is_in(ss["f_stratum"]))
    if ss.get("f_kind"):
        out = out.filter(pl.col("kind").is_in(ss["f_kind"]))
    if ss.get("f_bucket"):
        out = out.filter(pl.col("bucket").is_in(ss["f_bucket"]))
    if ss.get("f_search"):
        # search the REDACTED summary: verdict words must not be recoverable
        # via the filter box
        out = out.with_columns(
            pl.col("summary")
            .map_elements(redact_summary, return_dtype=pl.String)
            .alias("_summary_redacted")
        ).filter(
            pl.col("_summary_redacted")
            .fill_null("")
            .str.contains_any([ss["f_search"]], ascii_case_insensitive=True)
            | pl.col("case_id").str.contains_any(
                [ss["f_search"]], ascii_case_insensitive=True
            )
        )
    sort = ss.get("f_sort", "original order")
    if sort == "shortest chunk first":
        out = out.sort(["duration_s", "case_order"], nulls_last=True)
    elif sort == "longest chunk first":
        out = out.sort(
            ["duration_s", "case_order"], descending=[True, False], nulls_last=True
        )
    elif sort == "stratum, then case id":
        out = out.sort(["stratum", "case_id"])
    else:
        out = out.sort("case_order")
    return out


def goto_case(case_id: str) -> None:
    st.session_state["current_case_id"] = case_id


def step_case(visible: pl.DataFrame, delta: int) -> None:
    ids = visible["case_id"].to_list()
    if not ids:
        return
    cur = st.session_state.get("current_case_id")
    if cur in ids:
        idx = ids.index(cur)
        goto_case(ids[max(0, min(len(ids) - 1, idx + delta))])
    else:
        # current case filtered out: enter the visible list at the near end
        goto_case(ids[0] if delta > 0 else ids[-1])


# ---------------------------------------------------------------------------
# sidebar


def render_reviewer_input() -> str:
    """Rendered before anything reads the reviewer, so the whole run uses one
    consistent identity (no one-rerun-stale label/notes exposure between
    reviewers)."""
    with st.sidebar:
        st.markdown("### Case navigator")
        st.text_input("Reviewer initials", key="reviewer", max_chars=24)
    return st.session_state["reviewer"].strip()


def render_sidebar(
    visible: pl.DataFrame, all_cases: pl.DataFrame, labels: dict
) -> None:
    ss = st.session_state
    with st.sidebar:
        n_total = all_cases.height
        reviewer = ss["reviewer"].strip()
        n_labeled = sum(
            1 for cid in all_cases["case_id"].to_list()
            if (cid, reviewer) in labels
        )
        st.progress(n_labeled / n_total if n_total else 0.0)
        st.caption(f"{n_labeled}/{n_total} labeled by {reviewer or '—'}")

        with st.expander("Filters & sort", expanded=False):
            st.radio(
                "Status", ["all", "unlabeled", "labeled"], key="f_status",
                horizontal=True,
            )
            st.multiselect(
                "Stratum",
                sorted(all_cases["stratum"].drop_nulls().unique().to_list()),
                key="f_stratum",
            )
            st.multiselect(
                "Kind",
                sorted(all_cases["kind"].drop_nulls().unique().to_list()),
                key="f_kind",
            )
            st.multiselect(
                "Duration",
                ["< 10 min", "10–30 min", "30–60 min", "> 60 min", "unknown"],
                key="f_bucket",
            )
            st.text_input("Search summary / id", key="f_search")
            st.selectbox(
                "Sort",
                [
                    "original order",
                    "shortest chunk first",
                    "longest chunk first",
                    "stratum, then case id",
                ],
                key="f_sort",
            )

        c1, c2 = st.columns(2)
        c1.button(
            "◀ Prev", use_container_width=True,
            on_click=step_case, args=(visible, -1),
        )
        c2.button(
            "▶ Next", use_container_width=True,
            on_click=step_case, args=(visible, +1),
        )

        for row in visible.iter_rows(named=True):
            cid = row["case_id"]
            rec = labels.get((cid, reviewer))
            mark = "✅" if rec else "⬜"
            if rec and rec.flagged_for_revisit:
                mark = "🚩"
            dur = fmt_duration_s(row["duration_s"]) if row["duration_s"] else "?"
            sel = "▸ " if cid == ss.get("current_case_id") else ""
            st.button(
                f"{sel}{mark} {cid} · {row['stratum']} · {dur}",
                key=f"nav_{row['case_order']}",
                use_container_width=True,
                on_click=goto_case,
                args=(cid,),
            )

        st.caption(
            "Shortcuts: j/n next · k/p prev · s save · x save+next · "
            "a ambiguous · i insufficient (not while typing)"
        )


# ---------------------------------------------------------------------------
# main panels


def render_header(b: CaseBundle, rec: LabelRecord | None) -> None:
    case = b.case
    dur = case_duration(case)
    st.markdown(f"## {b.case_id}")
    cols = st.columns(6)
    cols[0].metric("stratum", str(case.get("stratum")))
    cols[1].metric("kind", str(case.get("kind")))
    cols[2].metric("chunk duration", fmt_duration_s(dur))
    cols[3].metric("n_events", str(case.get("n_events")))
    cols[4].metric("span_rel_s", str(case.get("span_rel_s")))
    cols[5].metric("label", neutral_label_text(rec))
    st.caption(f"summary: {redact_summary(case.get('summary')) or 'n/a'}")

    chunk = b.chunk
    facts = {
        "vehicle": (
            f"{b.vehicle_id} ({b.vehicle_source})" if b.vehicle_id else "n/a"
        ),
        "routes seen (AVL)": ", ".join(b.avl_route_labels) or "n/a",
        "start (local)": fmt_local(chunk.start_epoch) if chunk else "n/a",
        "end (local)": fmt_local(chunk.end_epoch) if chunk else "n/a",
        "AVL pings": (
            str(chunk.pings.height)
            if chunk is not None and chunk.pings is not None
            else (str(chunk.n_pings) if chunk and chunk.n_pings else "n/a")
        ),
        "APC events (±30 min)": (
            str(b.apc_events.height) if b.apc_events is not None else "n/a"
        ),
        "candidates": str(len(b.candidates)),
    }
    st.markdown(
        "<div style='font-size:0.9em;color:#555'>"
        + " · ".join(f"**{k}:** {v}" for k, v in facts.items())
        + "</div>",
        unsafe_allow_html=True,
    )
    if b.availability:
        with st.expander("⚠️ degraded evidence for this case", expanded=False):
            for k, v in b.availability.items():
                st.markdown(f"- **{k}**: {v}")


def render_visuals(b: CaseBundle) -> None:
    tab_map, tab_tl, tab_td, tab_ladder = st.tabs(
        ["🗺 Map", "⏱ Timeline", "📈 Time–distance", "🪜 Stop ladder"]
    )
    letters = [c.letter for c in b.candidates]

    with tab_map:
        tcols = st.columns([1, 1, 1, 1, 2])
        show_prev = tcols[0].checkbox("prev chunk", value=True)
        show_next = tcols[1].checkbox("next chunk", value=True)
        show_apc = tcols[2].checkbox("APC events", value=True)
        show_stops = tcols[3].checkbox("stops", value=True)
        shown = tcols[4].multiselect("candidates", letters, default=letters)
        safe_plot(
            fig_map,
            b,
            show_prev=show_prev,
            show_next=show_next,
            show_candidates=set(shown),
            show_stops=show_stops,
            show_apc=show_apc,
            empty_msg="no spatial evidence available for this case",
        )

    with tab_tl:
        safe_plot(fig_timeline, b, empty_msg="no temporal evidence available")

    with tab_td:
        ref = st.selectbox(
            "progress ruler (candidate shape)",
            letters or ["—"],
            help="which candidate's route geometry to measure progress along",
        )
        safe_plot(
            fig_time_distance,
            b,
            ref if ref != "—" else None,
            empty_msg="no AVL pings available for this case",
        )

    with tab_ladder:
        if not b.candidates:
            st.info("no candidates to compare against")
        else:
            pick = st.selectbox("candidate", letters, key="ladder_pick")
            cand = next(c for c in b.candidates if c.letter == pick)
            safe_plot(
                fig_stop_ladder, b, cand,
                empty_msg="no scheduled stop ladder available",
            )


def comparison_table(b: CaseBundle) -> None:
    if not b.candidates:
        return
    rows = []
    dur = None
    if b.chunk and b.chunk.start_epoch is not None and b.chunk.end_epoch is not None:
        dur = b.chunk.end_epoch - b.chunk.start_epoch
    for c in b.candidates:
        m = c.metrics
        rows.append(
            {
                "candidate": c.letter,
                "route": c.trip_meta.get("route_short_name") or "?",
                "direction": str(c.trip_meta.get("direction_id", "?")),
                "polarity": c.key.polarity or "—",
                "sched start": fmt_local(m.sched_start_epoch, with_date=False),
                "sched end": fmt_local(m.sched_end_epoch, with_date=False),
                "sched dur": fmt_duration_s(m.sched_duration_s),
                "chunk dur": fmt_duration_s(dur),
                "start resid": fmt_signed_s(m.start_residual_s),
                "end resid": fmt_signed_s(m.end_residual_s),
                "overlap": fmt_duration_s(m.overlap_s),
                "chunk inside": fmt_frac(m.chunk_containment),
                "stops covered": fmt_frac(m.stops_covered_frac),
                "dist→shape": fmt_m(m.median_dist_to_shape_m),
                "direction fit": (
                    "n/a"
                    if m.direction_consistency is None
                    else f"{m.direction_consistency:+.2f}"
                ),
                "APC hits": (
                    "n/a" if m.apc_stop_hits is None else str(m.apc_stop_hits)
                ),
                "route match": (
                    "n/a" if m.route_label_match is None
                    else ("yes" if m.route_label_match else "NO")
                ),
            }
        )
    show_table(rows)


def render_cards(b: CaseBundle) -> None:
    if not b.candidates:
        st.info(
            "No candidate cards "
            f"({b.availability.get('candidates', 'no candidate data')}). "
            "You can still label the case below."
        )
        return
    cards = st.columns(min(3, len(b.candidates)))
    for i, c in enumerate(b.candidates):
        m = c.metrics
        with cards[i % len(cards)].container(border=True):
            st.markdown(
                f"#### Candidate {c.letter} — route "
                f"{c.trip_meta.get('route_short_name') or '?'}"
            )
            headsign = c.trip_meta.get("trip_headsign")
            dirn = c.trip_meta.get("direction_id")
            pol = c.key.polarity
            st.caption(
                " · ".join(
                    x
                    for x in [
                        "date-level candidate (no specific trip)"
                        if c.trip_meta.get("route_level_only")
                        else None,
                        headsign,
                        f"direction {dirn}" if dirn is not None else None,
                        f"polarity {pol}" if pol else None,
                    ]
                    if x
                )
                or "no trip metadata"
            )
            st.markdown(
                f"- scheduled: **{fmt_local(m.sched_start_epoch, with_date=False)}"
                f" → {fmt_local(m.sched_end_epoch, with_date=False)}**"
                f" ({fmt_duration_s(m.sched_duration_s)})\n"
                f"- residuals: start **{fmt_signed_s(m.start_residual_s)}**,"
                f" end **{fmt_signed_s(m.end_residual_s)}**\n"
                f"- overlap **{fmt_duration_s(m.overlap_s)}**"
                f" ({fmt_frac(m.chunk_containment)} of chunk)\n"
                f"- stops covered **{fmt_frac(m.stops_covered_frac)}**"
                f" of {m.n_sched_stops or '?'}"
                f" · dist→shape **{fmt_m(m.median_dist_to_shape_m)}**\n"
                f"- direction fit **"
                + (
                    "n/a"
                    if m.direction_consistency is None
                    else f"{m.direction_consistency:+.2f}"
                )
                + f"** · APC hits **{m.apc_stop_hits if m.apc_stop_hits is not None else 'n/a'}**"
            )
            if m.tags:
                st.markdown(
                    " ".join(f"`{t}`" for t in m.tags)
                )
    with st.container(border=True):
        st.markdown("#### Null — no scheduled match")
        st.caption(
            "Choose this when no candidate window/geometry defensibly explains "
            "the chunk (e.g. deadhead, detour, off-schedule running)."
        )


# ---------------------------------------------------------------------------
# labeling form


def _form_defaults(
    b: CaseBundle, rec: LabelRecord | None, draft: dict | None
) -> dict:
    d = {
        "choice": "Ambiguous",
        "other_key": None,
        "confidence": 3,
        "fragment": "",
        "notes": "",
        "flag": False,
        **{f"r_{f}": False for f in RATIONALE_FLAGS},
    }
    src: dict | None = None
    if draft:
        src = draft
    elif rec:
        src = {
            "choice": _label_to_choice(b, rec),
            "other_key": rec.selected_candidate_id,
            "confidence": rec.confidence or 3,
            "fragment": rec.fragment_impression or "",
            "notes": rec.notes,
            "flag": rec.flagged_for_revisit,
            **{f"r_{f}": f in rec.rationale for f in RATIONALE_FLAGS},
        }
    if src:
        d.update({k: v for k, v in src.items() if k in d and v is not None})
    return d


def _label_to_choice(b: CaseBundle, rec: LabelRecord) -> str:
    if rec.label in ("MATCH_TOP_CANDIDATE", "MATCH_OTHER_CANDIDATE"):
        letter = rec.selected_candidate_letter
        if letter and any(c.letter == letter for c in b.candidates):
            return f"Match Candidate {letter}"
        return "Match other candidate…"
    return {
        "NO_SCHEDULED_MATCH": "No scheduled match",
        "AMBIGUOUS": "Ambiguous",
        "INSUFFICIENT_EVIDENCE": "Insufficient evidence",
    }.get(rec.label, "Ambiguous")


def render_form(
    b: CaseBundle,
    label_store: LabelStore,
    visible: pl.DataFrame,
    cfg: dict,
) -> None:
    ss = st.session_state
    reviewer = ss.get("reviewer", "").strip()
    rec = label_store.get(b.case_id, reviewer) if reviewer else None
    draft = label_store.get_draft(b.case_id, reviewer) if reviewer else None
    defaults = _form_defaults(b, rec, draft)

    choices = [f"Match Candidate {c.letter}" for c in b.candidates]
    if b.alternates is not None and b.alternates.height > len(b.candidates):
        choices.append("Match other candidate…")
    choices += ["No scheduled match", "Ambiguous", "Insufficient evidence"]

    key = f"form_{b.case_id}"
    st.markdown("### Decision")
    c_choice = st.radio(
        "Primary judgment",
        choices,
        index=(
            choices.index(defaults["choice"])
            if defaults["choice"] in choices
            else choices.index("Ambiguous")
        ),
        key=f"{key}_choice",
    )

    other_key = None
    if c_choice == "Match other candidate…" and b.alternates is not None:
        opts = b.alternates["candidate_key"].to_list()
        fmt = {
            r["candidate_key"]: (
                f"{r['service_date']} route {r['route_id']} trip {r['trip_id']}"
                + (f" [{r['polarity']}]" if r["polarity"] else "")
                + (f" (= card {r['on_card']})" if r["on_card"] else "")
            )
            for r in b.alternates.iter_rows(named=True)
        }
        other_key = st.selectbox(
            "Which candidate? (neutral order: date, route, trip)",
            opts,
            index=(
                opts.index(defaults["other_key"])
                if defaults.get("other_key") in opts
                else 0
            ),
            format_func=lambda k: fmt.get(k, k),
            key=f"{key}_other",
        )

    cc1, cc2 = st.columns([1, 1])
    confidence = cc1.slider(
        "Reviewer confidence", 1, 5, int(defaults["confidence"]), key=f"{key}_conf"
    )
    fragment = cc2.selectbox(
        "Fragment impression (optional)",
        FRAGMENT_IMPRESSIONS,
        index=FRAGMENT_IMPRESSIONS.index(defaults["fragment"])
        if defaults["fragment"] in FRAGMENT_IMPRESSIONS
        else 0,
        key=f"{key}_frag",
    )

    st.markdown("**Rationale** (tick what drove the decision)")
    rat_cols = st.columns(4)
    rationale: list[str] = []
    for i, flag in enumerate(RATIONALE_FLAGS):
        if rat_cols[i % 4].checkbox(
            flag.replace("_", " "),
            value=bool(defaults[f"r_{flag}"]),
            key=f"{key}_r_{flag}",
        ):
            rationale.append(flag)

    notes = st.text_area("Notes", value=defaults["notes"], key=f"{key}_notes")
    flagged = st.checkbox(
        "🚩 flag for revisit", value=bool(defaults["flag"]), key=f"{key}_flag"
    )

    # autosave draft when anything changed
    if reviewer:
        form_state = {
            "choice": c_choice,
            "other_key": other_key,
            "confidence": confidence,
            "fragment": fragment,
            "notes": notes,
            "flag": flagged,
            **{f"r_{f}": f in rationale for f in RATIONALE_FLAGS},
        }
        if form_state != ss.get(f"{key}_last_draft"):
            label_store.save_draft(b.case_id, reviewer, form_state)
            ss[f"{key}_last_draft"] = form_state

    def do_save(choice_override: str | None, advance: bool) -> None:
        # read the form from session_state, NOT render-time locals: widget
        # edits delivered in the same request as the click (e.g. notes whose
        # blur IS the click) are only present in session_state
        rv = ss.get("reviewer", "").strip()
        if not rv:
            st.error("enter reviewer initials in the sidebar before saving")
            return
        choice = choice_override or ss.get(f"{key}_choice", "Ambiguous")
        record = _make_record(
            b,
            cfg,
            label_store,
            choice,
            ss.get(f"{key}_other"),
            int(ss.get(f"{key}_conf", 3)),
            ss.get(f"{key}_frag", ""),
            [f for f in RATIONALE_FLAGS if ss.get(f"{key}_r_{f}", False)],
            ss.get(f"{key}_notes", ""),
            bool(ss.get(f"{key}_flag", False)),
        )
        label_store.save(record)
        st.toast(f"saved {b.case_id}: {neutral_label_text(record)}", icon="💾")
        if advance:
            step_case(visible, +1)

    b1, b2, b3, b4 = st.columns([1, 1, 1, 1])
    b1.button(
        "💾 Save label", use_container_width=True, type="primary",
        on_click=do_save, args=(None, False),
    )
    b2.button(
        "💾 Save & next ▶", use_container_width=True,
        on_click=do_save, args=(None, True),
    )
    b3.button(
        "⚡ Ambiguous + next", use_container_width=True,
        on_click=do_save, args=("Ambiguous", True),
    )
    b4.button(
        "⚡ Insufficient + next", use_container_width=True,
        on_click=do_save, args=("Insufficient evidence", True),
    )


def _make_record(
    b: CaseBundle,
    cfg: dict,
    label_store: LabelStore,
    choice: str,
    other_key: str | None,
    confidence: int,
    fragment: str,
    rationale: list[str],
    notes: str,
    flagged: bool,
) -> LabelRecord:
    ss = st.session_state
    letter = None
    cand_id = None
    if choice.startswith("Match Candidate "):
        letter = choice.rsplit(" ", 1)[-1]
        cand = next((c for c in b.candidates if c.letter == letter), None)
        cand_id = cand.key.raw if cand else None
        label = (
            "MATCH_TOP_CANDIDATE"
            if cand_id is not None and cand_id == b.anchor_best_key
            else "MATCH_OTHER_CANDIDATE"
        )
    elif choice == "Match other candidate…":
        cand_id = other_key
        letter = b.letter_of_key(other_key)
        label = (
            "MATCH_TOP_CANDIDATE"
            if cand_id is not None and cand_id == b.anchor_best_key
            else "MATCH_OTHER_CANDIDATE"
        )
    elif choice == "No scheduled match":
        label = "NO_SCHEDULED_MATCH"
    elif choice == "Insufficient evidence":
        label = "INSUFFICIENT_EVIDENCE"
    else:
        label = "AMBIGUOUS"

    reviewer = ss.get("reviewer", "").strip()
    revealed = (b.case_id, reviewer) in ss.get("revealed_cases", set()) or (
        b.case_id,
        reviewer,
    ) in label_store.reveals()
    return LabelRecord(
        case_id=b.case_id,
        reviewer=reviewer,
        label=label,
        selected_candidate_letter=letter,
        selected_candidate_id=cand_id,
        confidence=confidence,
        fragment_impression=fragment or None,
        rationale=rationale,
        notes=notes,
        diagnostics_revealed_before_save=revealed,
        session_id=ss.get("session_id", ""),
        app_version=APP_VERSION,
        chunk_id=b.chunk.chunk_id if b.chunk else None,
        cases_file=cfg.get("cases"),
        flagged_for_revisit=flagged,
    )


# ---------------------------------------------------------------------------
# diagnostics


def render_diagnostics(b: CaseBundle, store_obj, label_store: LabelStore) -> None:
    ss = st.session_state
    reviewer = ss.get("reviewer", "").strip()
    with st.expander("🔬 Model diagnostics (reveal only after forming a view)"):
        revealed = (b.case_id, reviewer) in ss["revealed_cases"] or (
            b.case_id,
            reviewer,
        ) in label_store.reveals()
        if not revealed:
            st.caption(
                "Hidden by default to limit anchoring. Revealing is recorded "
                "in the audit log and in labels saved afterwards."
            )
            if st.button("Reveal diagnostics for this case"):
                ss["revealed_cases"].add((b.case_id, reviewer))
                if reviewer:
                    label_store.record_reveal(b.case_id, reviewer)
                st.rerun()
            return

        st.markdown(f"raw summary: `{b.case.get('summary')}`")
        st.markdown(
            f"anchor status (from summary): "
            f"`{parse_anchor_status(b.case.get('summary')) or 'n/a'}`"
        )
        if b.anchor_diag:
            best = b.anchor_diag.get("best_key")
            st.markdown(
                f"**anchor decision**: status `{b.anchor_diag.get('status')}` · "
                f"best key `{best}` "
                f"(= card **{b.letter_of_key(best) or 'not on a card'}**) · "
                f"best_q {b.anchor_diag.get('best_q')} · "
                f"survival margin {b.anchor_diag.get('survival_margin_q')} q · "
                f"evidence margin {b.anchor_diag.get('evidence_margin_q')} q · "
                f"contract blockers {b.anchor_diag.get('n_contract_blockers')} · "
                f"censored considered {b.anchor_diag.get('censored_considered')}"
            )
        else:
            st.markdown("no anchor row for this chunk")
        if b.fragment_role_diag:
            st.markdown(
                f"**fragment role (model)**: `{b.fragment_role_diag.get('role')}` — "
                f"{b.fragment_role_diag.get('role_evidence') or 'no evidence note'}"
            )

        if b.candidates:
            st.markdown("**cards → model ids** (with scores, ranked)")
            rows = [
                {
                    "card": c.letter,
                    "candidate_key": c.key.raw,
                    "qscore": c.diagnostics.get("qscore"),
                    "regime": c.diagnostics.get("regime"),
                    "censor_class": c.diagnostics.get("censor_class"),
                    "n_aligned": c.diagnostics.get("n_aligned"),
                    "n_observed": c.diagnostics.get("n_observed"),
                }
                for c in b.candidates
            ]
            show_table(
                pl.DataFrame(rows).sort("qscore", descending=True, nulls_last=True)
            )
        if b.alternates is not None:
            st.markdown(f"full candidate list: {b.alternates.height} keys")
            show_table(b.alternates)

        for name in [
            "unique_survivor_report.json",
            "dual_margin_report.json",
            "evidence_reuse_report.json",
            "fragment_report.json",
        ]:
            payload = load_run_report(store_obj, name)
            if payload is not None:
                with st.expander(name, expanded=False):
                    st.json(payload)

        with st.expander("raw case JSON", expanded=False):
            st.json(
                {
                    "case": b.case,
                    "chunk": {
                        k: getattr(b.chunk, k)
                        for k in (
                            "chunk_id start_epoch end_epoch n_pings role "
                            "slice profile run_id"
                        ).split()
                    }
                    if b.chunk
                    else None,
                    "vehicle": {"id": b.vehicle_id, "source": b.vehicle_source},
                    "anchor": b.anchor_diag,
                    "fragment_role": b.fragment_role_diag,
                    "availability": b.availability,
                }
            )


# ---------------------------------------------------------------------------
# export helpers


def render_export(b: CaseBundle, rec: LabelRecord | None) -> None:
    lines = [
        f"# case {b.case_id}",
        "",
        f"- stratum: {b.case.get('stratum')} · kind: {b.case.get('kind')}",
        f"- summary: {redact_summary(b.case.get('summary'))}",
        f"- vehicle: {b.vehicle_id} ({b.vehicle_source})",
        (
            f"- window: {fmt_local(b.chunk.start_epoch)} → "
            f"{fmt_local(b.chunk.end_epoch)}"
            if b.chunk
            else "- window: n/a"
        ),
        f"- routes seen: {', '.join(b.avl_route_labels) or 'n/a'}",
        "",
        "## candidates",
    ]
    for c in b.candidates:
        m = c.metrics
        lines.append(
            f"- {c.letter}: route {c.trip_meta.get('route_short_name')} "
            f"{fmt_local(m.sched_start_epoch)}→{fmt_local(m.sched_end_epoch)} "
            f"overlap {fmt_duration_s(m.overlap_s)} "
            f"covered {fmt_frac(m.stops_covered_frac)} tags={m.tags}"
        )
    if rec:
        # neutral phrasing: the export must not disclose model agreement
        lines += ["", "## label",
                  f"- {neutral_label_text(rec)} (confidence {rec.confidence})",
                  f"- notes: {rec.notes}"]
    text = "\n".join(lines)
    c1, c2 = st.columns([1, 3])
    c1.download_button(
        "⬇ case summary (.md)",
        text,
        file_name=f"{b.case_id}_summary.md",
        use_container_width=True,
    )
    with c2.expander("copy case summary"):
        st.code(text, language="markdown")


# ---------------------------------------------------------------------------
# main


def main() -> None:
    cfg = get_config()
    init_state(cfg)

    if not cfg["cases"] or not Path(cfg["cases"]).exists():
        st.error(
            "No cases CSV found. Pass one with:\n\n"
            "`streamlit run app.py -- --cases path/to/labeling_packet_redacted.csv "
            "--artifacts path/to/artifacts`"
        )
        st.stop()

    store_obj = cached_store(cfg["cases"], cfg["artifacts"])
    cases_df = cached_cases(cfg["cases"], cfg["artifacts"])
    if cases_df is None or cases_df.is_empty():
        st.error(f"could not read any cases from {cfg['cases']}")
        st.stop()

    label_store = LabelStore(cfg["labels_dir"])
    # reviewer input is rendered FIRST so every later read this run —
    # labels shown, progress, saved-record prefill — uses the fresh identity
    reviewer = render_reviewer_input()
    labels = label_store.load_all()
    labeled_ids = {cid for (cid, rv) in labels if rv == reviewer}

    visible = order_and_filter_cases(cases_df, labeled_ids)
    if st.session_state.get("current_case_id") is None and visible.height:
        st.session_state["current_case_id"] = visible["case_id"][0]

    render_sidebar(visible, cases_df, labels)
    keyboard.install()

    cur_id = st.session_state.get("current_case_id")
    if not cur_id:
        st.info("no case selected (filters may exclude everything)")
        st.stop()
    if cases_df.filter(pl.col("case_id") == cur_id).is_empty():
        st.error(f"case {cur_id} not in the cases file")
        st.stop()

    bundle = cached_bundle(cfg["cases"], cfg["artifacts"], cur_id)
    rec = labels.get((cur_id, reviewer))

    render_header(bundle, rec)
    render_visuals(bundle)
    st.markdown("### Candidate comparison")
    comparison_table(bundle)
    render_cards(bundle)
    render_form(bundle, label_store, visible, cfg)
    render_diagnostics(bundle, store_obj, label_store)
    render_export(bundle, rec)
    st.caption(
        f"app v{APP_VERSION} · session {st.session_state['session_id']} · "
        f"labels → {cfg['labels_dir']}"
    )


main()
