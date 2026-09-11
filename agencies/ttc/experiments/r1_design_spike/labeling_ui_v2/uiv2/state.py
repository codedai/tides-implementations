"""Session-state helpers: interaction tracking and blind-gate bookkeeping.

Everything here is UI-side convenience; the enforcement of the blind
protocol lives in ``r1spike.labeling_v2_store``. These helpers make sure the
save payload carries an honest record of what the reviewer actually looked
at (panels, filters, adjacent context) for the current case.
"""

from __future__ import annotations

from typing import Any

import streamlit as st


def _case_key(case_id: str, suffix: str) -> str:
    return f"v2::{case_id}::{suffix}"


def note_panel_view(case_id: str, panel: str) -> None:
    key = _case_key(case_id, "panels_viewed")
    panels = st.session_state.setdefault(key, [])
    if panel not in panels:
        panels.append(panel)


def panels_viewed(case_id: str) -> list[str]:
    return list(st.session_state.get(_case_key(case_id, "panels_viewed"), []))


def note_adjacent_context(case_id: str) -> None:
    st.session_state[_case_key(case_id, "adjacent_context_viewed")] = True


def adjacent_context_viewed(case_id: str) -> bool:
    return bool(st.session_state.get(_case_key(case_id, "adjacent_context_viewed")))


def note_filters(case_id: str, filters: dict[str, Any], hidden_count: int) -> None:
    st.session_state[_case_key(case_id, "filters")] = {
        "filters": filters,
        "hidden_count": hidden_count,
    }


def current_filters(case_id: str) -> dict[str, Any]:
    return dict(
        st.session_state.get(
            _case_key(case_id, "filters"), {"filters": {}, "hidden_count": 0}
        )
    )


def note_review_started(case_id: str, started_at: str) -> str:
    key = _case_key(case_id, "review_started_at")
    if key not in st.session_state:
        st.session_state[key] = started_at
    return st.session_state[key]


def review_started_at(case_id: str) -> str | None:
    return st.session_state.get(_case_key(case_id, "review_started_at"))


def clear_case_state(case_id: str) -> None:
    prefix = f"v2::{case_id}::"
    for key in [k for k in st.session_state if str(k).startswith(prefix)]:
        del st.session_state[key]
