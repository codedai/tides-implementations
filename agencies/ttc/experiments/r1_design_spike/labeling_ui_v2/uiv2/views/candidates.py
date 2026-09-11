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

# 候选全集产物里存的警告是英文原文（见 r1spike.labeling_v2_universe）。
# 这里只在显示层翻译，产物本身一个字节都不动；未收录的原文原样显示。
_WARNING_LABELS = {
    "explicit no-scheduled-trip alternative": "显式的「无计划班次」替代项",
    "contract-unresolved sentinel component; shown with warning": (
        "含契约未解析的哨兵字段；仍然显示，但带警告"
    ),
    "unparseable service date; shown with warning": (
        "服务日期无法解析；仍然显示，但带警告"
    ),
    "key has no identity component; shown with warning": (
        "该 key 没有任何身份字段；仍然显示，但带警告"
    ),
}


def _warning_text(warning: str | None) -> str:
    if not warning:
        return ""
    if warning in _WARNING_LABELS:
        return _WARNING_LABELS[warning]
    if warning.startswith("key has ") and "expected 5" in warning:
        parts = warning.split()[2]
        return f"该 key 有 {parts} 个字段，应为 5 个"
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
        f"候选全集 —— 共 {len(real)} 个候选（完整、中性排序）",
        expanded=True,
    ):
        st.caption(
            "排序依据：服务日期 → feed → 线路 → 计划发车时刻。别名字母是排序"
            "之后才分配的，因此只反映时间先后，不含任何模型偏好。全集始终完整"
            "可见，此处不存在任何模型筛选出的短名单；判断表单里也始终提供"
            "「没有对应计划班次」这个空选项。"
        )
        columns = st.columns(5)
        dates = sorted({c.service_date or "?" for c in real})
        feeds = sorted({c.feed_id or "?" for c in real})
        routes = sorted({c.route_mapping_id or "?" for c in real})
        polarities = sorted({c.polarity_hypothesis_id or "null" for c in real})
        selected_dates = columns[0].multiselect(
            "服务日期", dates, placeholder="不限", key=f"fd::{case_id}"
        )
        selected_feeds = columns[1].multiselect(
            "feed", feeds, placeholder="不限", key=f"ff::{case_id}"
        )
        selected_routes = columns[2].multiselect(
            "线路", routes, placeholder="不限", key=f"fr::{case_id}"
        )
        selected_polarities = columns[3].multiselect(
            "极性", polarities, placeholder="不限", key=f"fp::{case_id}"
        )
        starts = [c.sched_start_epoch for c in real if c.sched_start_epoch is not None]
        start_range = None
        if starts and min(starts) < max(starts):
            low, high = columns[4].slider(
                "计划发车时刻范围",
                min_value=min(starts),
                max_value=max(starts),
                value=(min(starts), max(starts)),
                key=f"fs::{case_id}",
                format="",
                help="按计划发车时刻筛选；非计划班次的替代项永远不会被这个筛选器隐藏",
            )
            if (low, high) != (min(starts), max(starts)):
                start_range = (low, high)
        reset_col, show_all_col = st.columns(2)
        if reset_col.button("重置筛选", key=f"freset::{case_id}"):
            for prefix in ("fd", "ff", "fr", "fp", "fs"):
                st.session_state.pop(f"{prefix}::{case_id}", None)
            st.rerun()
        show_all = show_all_col.toggle(
            "显示全部候选（忽略筛选）",
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
                f"共 {len(real)} 个候选，其中 {hidden} 个被**你自己设置的筛选条件**"
                "隐藏了 —— 这只是审阅便利，不是系统给的短名单；点「显示全部」"
                "或「重置筛选」即可看到全部"
            )
        elif active and not show_all:
            st.caption(f"当前筛选条件没有隐藏任何候选（共 {len(real)} 个）")
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
                f"页码（按时间顺序，共 {page_count} 页）",
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
                f"当前第 {page} / {page_count} 页 —— 另有 "
                f"{len(visible) - len(page_rows)} 个可见候选在其他页"
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
        "别名": c.alias,
        "服务日期": c.service_date or "—",
        "feed": (c.feed_id or "—")[-12:],
        "线路": c.route_mapping_id or "—",
        "trip": c.scheduled_trip_id or "（无计划班次）",
        "极性": c.polarity_hypothesis_id
        or ("显式空值" if c.explicit_null_polarity else "—"),
        "计划发车": fmt_local(c.sched_start_epoch) if c.sched_start_epoch else "—",
        "计划时长": fmt_duration_s(sched_duration) if sched_duration else "—",
        "重叠": fmt_duration_s(overlap) if overlap is not None else "—",
        "起点残差": fmt_duration_s(start_resid) if start_resid is not None else "—",
        "终点残差": fmt_duration_s(end_resid) if end_resid is not None else "—",
        "计划停站数": c.sched_n_stops if c.sched_n_stops is not None else "—",
        "警告": _warning_text(c.unresolved_warning),
    }


def alias_selector(
    bundle: CaseBundleV2,
    visible: list[CandidateInfo],
    *,
    key: str,
    label: str,
    allow_none: bool = True,
    none_label: str = "— 无 / 未解决 —",
) -> str | None:
    """Alias picker over the visible candidates (plus an explicit none)."""

    options: list[str] = []
    if allow_none:
        options.append(none_label)
    options.extend(c.alias for c in visible)
    choice = st.selectbox(label, options, key=key)
    return None if choice == none_label else choice
