"""Complete-universe candidate browser with logged, reviewer-owned filters.

The default view is the complete neutrally ordered universe. Filters are a
reviewer convenience: the hidden count is always displayed, the null option
never disappears, and the applied filters plus hidden count are logged into
the save record. Nothing here orders or trims by model score.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from .. import state
from ..case_loader import CandidateInfo, CaseBundleV2
from ..parsing import fmt_duration_s, fmt_local
from ..tables import show_table

PAGE_SIZE = 12

# The candidate-universe artifact stores its warnings as the original English
# text (see r1spike.labeling_v2_universe). They are reworded here at the
# display layer only; the artifact itself is not touched by a single byte,
# and unlisted originals are shown as-is.
_WARNING_LABELS = {
    "explicit no-scheduled-trip alternative": (
        "Explicit 'no scheduled trip' alternative"
    ),
    "contract-unresolved sentinel component; shown with warning": (
        "Contains a contract-unresolved sentinel component; still shown, with a warning"
    ),
    "unparseable service date; shown with warning": (
        "Unparseable service date; still shown, with a warning"
    ),
    "key has no identity component; shown with warning": (
        "Key has no identity component; still shown, with a warning"
    ),
}


def _warning_text(warning: str | None) -> str:
    if not warning:
        return ""
    if warning in _WARNING_LABELS:
        return _WARNING_LABELS[warning]
    if warning.startswith("key has ") and "expected 5" in warning:
        parts = warning.split()[2]
        return f"Key has {parts} components; expected 5"
    return warning


def _filters_key(case_id: str) -> str:
    return f"candfilters::{case_id}"


def _apply_filters(
    candidates: list[CandidateInfo], filters: dict[str, Any]
) -> list[CandidateInfo]:
    result = candidates
    if filters.get("service_dates"):
        result = [
            c for c in result if (c.service_date or "?") in filters["service_dates"]
        ]
    if filters.get("feeds"):
        result = [c for c in result if (c.feed_id or "?") in filters["feeds"]]
    if filters.get("routes"):
        result = [c for c in result if (c.route_mapping_id or "?") in filters["routes"]]
    if filters.get("polarities"):
        result = [
            c
            for c in result
            if (c.polarity_hypothesis_id or "null") in filters["polarities"]
        ]
    start_range = filters.get("sched_start_range")
    if start_range:
        low, high = start_range
        result = [
            c
            for c in result
            if c.sched_start_epoch is None or low <= c.sched_start_epoch <= high
        ]
    return result


def render_candidate_browser(bundle: CaseBundleV2, review_store) -> dict[str, Any]:
    """Render filters + table; returns {'visible': [...], 'hidden_count': n}."""

    real = bundle.real_candidates
    case_id = bundle.case_id
    with st.expander(
        f"Candidate universe — {len(real)} candidates (complete, neutrally ordered)",
        expanded=True,
    ):
        st.caption(
            "Ordered by: service date → feed → route → scheduled start time. "
            "Alias letters are assigned only after ordering, so they reflect "
            "chronological position only and carry no model preference. The "
            "universe is always fully visible; there is no model-selected "
            "shortlist here, and the judgment form always offers the "
            "'no corresponding scheduled trip' null option."
        )
        columns = st.columns(5)
        dates = sorted({c.service_date or "?" for c in real})
        feeds = sorted({c.feed_id or "?" for c in real})
        routes = sorted({c.route_mapping_id or "?" for c in real})
        polarities = sorted({c.polarity_hypothesis_id or "null" for c in real})
        selected_dates = columns[0].multiselect(
            "Service date", dates, placeholder="Any", key=f"fd::{case_id}"
        )
        selected_feeds = columns[1].multiselect(
            "feed", feeds, placeholder="Any", key=f"ff::{case_id}"
        )
        selected_routes = columns[2].multiselect(
            "Route", routes, placeholder="Any", key=f"fr::{case_id}"
        )
        selected_polarities = columns[3].multiselect(
            "Polarity", polarities, placeholder="Any", key=f"fp::{case_id}"
        )
        starts = [c.sched_start_epoch for c in real if c.sched_start_epoch is not None]
        start_range = None
        if starts and min(starts) < max(starts):
            low, high = columns[4].slider(
                "Scheduled start time range",
                min_value=min(starts),
                max_value=max(starts),
                value=(min(starts), max(starts)),
                key=f"fs::{case_id}",
                format="",
                help=(
                    "Filter by scheduled start time; non-scheduled-trip "
                    "alternatives are never hidden by this filter"
                ),
            )
            if (low, high) != (min(starts), max(starts)):
                start_range = (low, high)
        reset_col, show_all_col = st.columns(2)
        if reset_col.button("Reset filters", key=f"freset::{case_id}"):
            for prefix in ("fd", "ff", "fr", "fp", "fs"):
                st.session_state.pop(f"{prefix}::{case_id}", None)
            st.rerun()
        show_all = show_all_col.toggle(
            "Show all candidates (ignore filters)",
            key=f"fall::{case_id}",
        )
        filters = {
            "service_dates": selected_dates,
            "feeds": selected_feeds,
            "routes": selected_routes,
            "polarities": selected_polarities,
            "sched_start_range": start_range,
        }
        active = {k: v for k, v in filters.items() if v}
        visible = real if (show_all or not active) else _apply_filters(real, active)
        hidden = len(real) - len(visible)
        if hidden:
            st.warning(
                f"{len(real)} candidates in total, {hidden} of them hidden by "
                "**filters you set yourself** — this is a reviewer convenience, "
                "not a system-provided shortlist; click 'Show all' or "
                "'Reset filters' to see all of them"
            )
        elif active and not show_all:
            st.caption(f"The current filters hide no candidates ({len(real)} in total)")
        previous = state.current_filters(case_id)
        state.note_filters(case_id, active, hidden)
        if active != previous.get("filters", {}):
            review_store.record_interaction(
                case_id,
                kind="candidate_filter_applied",
                detail={"filters": active, "hidden_count": hidden},
            )
        page_count = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = 1
        if page_count > 1:
            page = st.number_input(
                f"Page (chronological order, {page_count} pages)",
                min_value=1,
                max_value=page_count,
                value=1,
                key=f"fpage::{case_id}",
            )
        page_rows = visible[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        show_table(
            [_candidate_row(bundle, c) for c in page_rows],
        )
        if len(visible) > len(page_rows):
            st.caption(
                f"Page {page} / {page_count} — "
                f"{len(visible) - len(page_rows)} more visible candidates on other "
                "pages"
            )
    return {"visible": visible, "hidden_count": hidden, "filters": active}


def _candidate_row(bundle: CaseBundleV2, c: CandidateInfo) -> dict[str, Any]:
    overlap = c.overlap_s(bundle.chunk)
    start_resid = c.start_residual_s(bundle.chunk)
    end_resid = c.end_residual_s(bundle.chunk)
    sched_duration = (
        c.sched_end_epoch - c.sched_start_epoch
        if c.sched_start_epoch is not None and c.sched_end_epoch is not None
        else None
    )
    return {
        "Alias": c.alias,
        "Service date": c.service_date or "—",
        "feed": (c.feed_id or "—")[-12:],
        "Route": c.route_mapping_id or "—",
        "trip": c.scheduled_trip_id or "(no scheduled trip)",
        "Polarity": c.polarity_hypothesis_id
        or ("explicit null" if c.explicit_null_polarity else "—"),
        "Scheduled start": fmt_local(c.sched_start_epoch)
        if c.sched_start_epoch
        else "—",
        "Scheduled duration": fmt_duration_s(sched_duration) if sched_duration else "—",
        "Overlap": fmt_duration_s(overlap) if overlap is not None else "—",
        "Start residual": fmt_duration_s(start_resid)
        if start_resid is not None
        else "—",
        "End residual": fmt_duration_s(end_resid) if end_resid is not None else "—",
        "Scheduled stops": c.sched_n_stops if c.sched_n_stops is not None else "—",
        "Warning": _warning_text(c.unresolved_warning),
    }


def alias_selector(
    bundle: CaseBundleV2,
    visible: list[CandidateInfo],
    *,
    key: str,
    label: str,
    allow_none: bool = True,
    none_label: str = "— none / unresolved —",
) -> str | None:
    """Alias picker over the visible candidates (plus an explicit none)."""

    options: list[str] = []
    if allow_none:
        options.append(none_label)
    options.extend(c.alias for c in visible)
    choice = st.selectbox(label, options, key=key)
    return None if choice == none_label else choice
