"""Common context strip and evidence panels shared by every task page.

Shows operational metadata only: no packet ``summary``, no anchor status,
no model scores, no first-pass fields, and — for parents — no task or
routing vocabulary before the grain decision. The synthetic packet fields
``n_events``/``span_rel_s`` are deliberately absent.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import polars as pl
import streamlit as st

from .. import state
from ..case_loader import CandidateDetail, CaseBundleV2, load_candidate_detail
from ..parsing import fmt_duration_s, fmt_local
from ..plots import (
    fig_boundary,
    fig_map,
    fig_stop_ladder,
    fig_time_progress,
    fig_time_tracks,
    fig_timeline,
    safe_plot,
)

PANELS = ("map", "timeline", "time_progress", "stop_ladder", "time_tracks")

# 显示名 -> 记录进 panels_viewed 的英文 slug（存储值不随界面语言变化）
PANEL_SLUGS = {
    "地图": "map",
    "时间线": "timeline",
    "进度曲线": "time_progress",
    "停站梯图": "stop_ladder",
    "时间轨对比": "time_tracks",
}

NEUTRAL_TASK_LABEL = "盲审中 —— 任务名将在你回答粒度问题后揭示"


def render_context_strip(bundle: CaseBundleV2, task_label: str) -> None:
    routing = bundle.routing
    st.subheader(f"案例 {bundle.case_id}")
    st.caption(f"当前任务：{task_label}")
    columns = st.columns(4)
    duration = bundle.chunk.duration_s
    columns[0].metric("chunk 时长", fmt_duration_s(duration) if duration else "无")
    columns[1].metric("AVL ping 数", routing.get("avl_ping_count") or "无")
    columns[2].metric(
        "APC 记录数",
        routing.get("apc_record_count")
        if routing.get("apc_record_count") is not None
        else "n/a",
        help=str(routing.get("apc_record_count_scope") or ""),
    )
    columns[3].metric("候选数", routing.get("candidate_count") or 0)
    lines: list[str] = []
    if bundle.avl_route_labels:
        lines.append(f"出现过的线路标签：{', '.join(bundle.avl_route_labels)}")
    observed = None
    if bundle.apc_events is not None and "source_stop_code" in (
        bundle.apc_events.columns
    ):
        observed = bundle.apc_events["source_stop_code"].drop_nulls().n_unique()
    if observed is not None:
        lines.append(f"附近观测到的 APC stop code 种类数：{observed}")
    before = routing.get("context_before_s")
    after = routing.get("context_after_s")
    lines.append(
        "可调取的上下文："
        f"前 {fmt_duration_s(before) if before is not None else '无'} / "
        f"后 {fmt_duration_s(after) if after is not None else '无'}"
        "（前=本 chunk 起点减前一个 chunk 起点；后=后一个 chunk 终点减本 chunk 终点；"
        "「无」表示同一运行段内该侧没有相邻 chunk）"
    )
    if bundle.vehicle_id is not None:
        lines.append(f"车辆识别来源：{bundle.vehicle_source}（车辆 ID 仅保留在本地）")
    if bundle.dst_crossing:
        lines.append(
            ":warning: 本 chunk 窗口跨越了时钟变更 —— 图表改用真实经过时间轴，"
            "墙钟文字不可靠"
        )
    if routing.get("stratum"):
        lines.append(f"抽样层：`{routing['stratum']}` —— {_caveat_text(routing)}")
    for note in bundle.availability.values():
        lines.append(f":warning: {note}")
    st.caption("  \n".join(lines))


# 冻结产物里存的是英文原文，此处仅在显示层翻译，绝不改动产物本身。
_STRATUM_CAVEAT_EN = "Original packet sampling stratum — not a human truth label."
_STRATUM_CAVEAT_ZH = (
    "这只是原始数据包的抽样分层，不是人工给出的真值标签；"
    "该层仅由 chunk 时长阈值决定，不含额外信息。"
)


def _caveat_text(routing) -> str:
    caveat = routing.get("stratum_caveat")
    if caveat == _STRATUM_CAVEAT_EN:
        return _STRATUM_CAVEAT_ZH
    return str(caveat)


def _window_caption(bundle: CaseBundleV2) -> str:
    start, end = bundle.chunk.start_epoch, bundle.chunk.end_epoch
    if start is None or end is None:
        return "chunk 窗口不可用"
    return f"当前标注窗口：{fmt_local(start)} → {fmt_local(end)}"


def _without_adjacent(bundle: CaseBundleV2) -> CaseBundleV2:
    """A view of the bundle with all temporally adjacent evidence removed:
    no prev/next chunks and no out-of-window padded APC events."""

    apc = bundle.apc_events
    if (
        apc is not None
        and bundle.chunk.start_epoch is not None
        and bundle.chunk.end_epoch is not None
        and "arrival_epoch_s" in apc.columns
    ):
        apc = apc.filter(
            pl.col("arrival_epoch_s").is_between(
                bundle.chunk.start_epoch, bundle.chunk.end_epoch
            )
        )
    return replace(bundle, prev_chunk=None, next_chunk=None, apc_events=apc)


def render_evidence_panels(
    bundle: CaseBundleV2,
    store_ctx: Any,
    *,
    detail_aliases: list[str],
    boundary_interval: tuple[int, int] | None = None,
) -> dict[str, CandidateDetail]:
    """Render the evidence panels; returns loaded candidate details.

    Panels render one at a time behind a selector (not tabs) so the logged
    ``panels_viewed`` reflects what the reviewer actually opened. The
    adjacent-context toggle governs every panel: off means no prev/next
    chunk traces and no out-of-window APC anywhere, so the logged flag is
    honest in both directions.
    """

    details: dict[str, CandidateDetail] = {}
    for alias in detail_aliases:
        cache_key = f"detail::{bundle.case_id}::{alias}"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = load_candidate_detail(
                store_ctx.artifact_store, bundle, alias
            )
        detail = st.session_state[cache_key]
        if detail is not None:
            details[alias] = detail
    st.caption(_window_caption(bundle))
    show_adjacent = st.toggle(
        "显示相邻上下文（前一个/后一个 chunk，以及 ±30 分钟内的 APC）",
        value=False,
        key=f"adj::{bundle.case_id}",
        help=(
            "相邻上下文以浅淡样式绘制，与当前标注窗口明确区分。打开这个开关"
            "会被记录，存盘表单还会再问你一次判断是否依赖了它。"
        ),
    )
    if show_adjacent and (
        bundle.prev_chunk is not None
        or bundle.next_chunk is not None
        or bundle.apc_events is not None
    ):
        if not state.adjacent_context_viewed(bundle.case_id):
            store_ctx.review_store.record_interaction(
                bundle.case_id,
                kind="adjacent_context_view",
                detail={"source": "adjacent_context_toggle"},
            )
        state.note_adjacent_context(bundle.case_id)
    shown = bundle if show_adjacent else _without_adjacent(bundle)
    panel = st.radio(
        "证据面板",
        ["地图", "时间线", "进度曲线", "停站梯图", "时间轨对比"],
        horizontal=True,
        key=f"panel::{bundle.case_id}",
        label_visibility="collapsed",
    )
    panel_slug = PANEL_SLUGS[panel]
    if panel_slug not in state.panels_viewed(bundle.case_id):
        store_ctx.review_store.record_interaction(
            bundle.case_id, kind="panel_view", detail={"panel": panel_slug}
        )
    state.note_panel_view(bundle.case_id, panel_slug)
    if panel == "地图":
        safe_plot(
            fig_map,
            shown,
            show_prev=show_adjacent,
            show_next=show_adjacent,
            show_candidate_shapes=details or None,
            empty_msg="这个窗口没有可用的 AVL ping，无法绘制地图",
        )
    elif panel == "时间线":
        extra = (
            [
                {
                    "label": "提议的边界",
                    "start_epoch": (bundle.chunk.start_epoch or 0)
                    + boundary_interval[0],
                    "end_epoch": (bundle.chunk.start_epoch or 0) + boundary_interval[1],
                    "kind": "boundary",
                }
            ]
            if boundary_interval and bundle.chunk.start_epoch is not None
            else None
        )
        if not details:
            st.caption("在上方加载候选的详细证据，才能在这里画出它们的计划时间窗")
        safe_plot(
            fig_timeline,
            shown,
            candidate_windows=[bundle.by_alias(a) for a in sorted(details)],
            extra_intervals=extra,
            empty_msg="没有可用的时间线数据",
        )
    elif panel == "进度曲线":
        reference = None
        if details:
            pick = st.selectbox(
                "把行进进度投影到哪个候选的线形上",
                sorted(details),
                key=f"tp::{bundle.case_id}",
            )
            reference = details.get(pick)
        safe_plot(
            fig_time_progress,
            shown,
            reference,
            empty_msg="没有可用的进度数据",
        )
    elif panel == "停站梯图":
        if details:
            pick = st.selectbox(
                "按哪个候选绘制停站梯图",
                sorted(details),
                key=f"ladder::{bundle.case_id}",
            )
            safe_plot(
                fig_stop_ladder,
                shown,
                details[pick],
                empty_msg="该候选没有可用的计划停站",
            )
        else:
            st.info("先加载某个候选的详细证据，才能看到它的停站梯图")
    else:
        safe_plot(
            fig_time_tracks,
            shown,
            empty_msg=(
                "这个窗口不存在墙钟歧义 —— 竞争时间轨只在夏令时回拨的那一小时"
                "附近才有意义"
            ),
        )
    if boundary_interval is not None and bundle.chunk.start_epoch is not None:
        state.note_panel_view(bundle.case_id, "boundary_plot")
        safe_plot(
            fig_boundary,
            shown,
            interval_rel_s=boundary_interval,
            empty_msg="没有可用的边界上下文",
        )
    return details
