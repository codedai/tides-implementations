"""粒度优先表单，以及六个任务专用的判断表单。

此处强制的契约要点（store 会在存盘时全部重新校验）：

- 粒度问题排在最前，没有预选答案，回答之前不显示任何审计建议；
- 计划班次的空选项始终可见，任何时候都不强迫给出匹配；
- 置信度 4-5 同时要求结构化理由和自由文字理由；
- 时区标签由时间戳推导，绝不采信审阅人输入的文字；
- 提议的切分若落在显示窗口之外，会给出显著警告。

界面文字为中文，但**存储值一律保持英文原样**：所有下拉/单选的 options
都是入库的枚举值，中文只出现在 ``format_func`` 的显示层。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ..case_loader import CaseBundleV2
from ..parsing import fmt_duration_s
from .candidates import alias_selector

GRAIN_OPTIONS: list[tuple[str, str]] = [
    ("single_trip_candidate", "一个可评的、trip 尺度的 chunk"),
    ("multi_trip_chunk", "包含多个 trip —— 需要切分"),
    ("local_fragment", "局部片段（不是一个完整 trip）"),
    ("boundary_spanning", "跨边界的 APC / 事件记录"),
    ("turnaround_or_idle", "周转 / 怠速 / 非营运活动"),
    ("data_quality_invalid", "源数据、时间或轨迹表征无效"),
    ("coverage_censored", "上下文不足 / 覆盖被截断"),
    ("unknown", "无法判断"),
]

REVIEWABILITY_BY_GRAIN: dict[str, list[str]] = {
    "single_trip_candidate": [
        "reviewable_for_schedule_match",
        "insufficient_context",
    ],
    "multi_trip_chunk": ["requires_segmentation_adjudication"],
    "boundary_spanning": ["requires_boundary_adjudication"],
    "local_fragment": [
        "not_reviewable_at_current_grain",
        "insufficient_context",
    ],
    "turnaround_or_idle": ["not_reviewable_at_current_grain"],
    "data_quality_invalid": ["invalid_source_state"],
    "coverage_censored": ["insufficient_context"],
    "unknown": ["insufficient_context", "not_reviewable_at_current_grain"],
}

REVIEWABILITY_LABELS = {
    "reviewable_for_schedule_match": "可以进行计划班次匹配评审",
    "insufficient_context": "上下文不足，无法评审",
    "requires_segmentation_adjudication": "需要分段裁决",
    "requires_boundary_adjudication": "需要边界裁决",
    "not_reviewable_at_current_grain": "在当前粒度下不可评审",
    "invalid_source_state": "源数据状态无效",
}

EVIDENCE_FAMILY_LABELS = {
    "interval_time_fit": "时间区间 / 起点 / 终点吻合度",
    "stop_sequence_fit": "停站序列吻合度",
    "spatial_shape_fit": "空间线形吻合度",
    "terminal_behavior": "终点站行为",
    "apc_signal": "APC 上下客信号",
    "direction_or_polarity": "方向 / 极性",
    "schedule_headway_separation": "与竞争班次的班距分离度",
    "adjacent_context": "相邻上下文",
    "data_quality_signal": "数据质量信号",
}

TRISTATE_OPTIONS = ["true", "false", "ambiguous", "not_applicable"]

TRISTATE_LABELS = {
    "true": "是",
    "false": "否",
    "ambiguous": "无法判断",
    "not_applicable": "不适用",
}

SCHEDULED_JUDGMENT_OPTIONS = [
    "matched",
    "no_scheduled_match",
    "ambiguous_candidates",
    "insufficient_evidence",
]

SCHEDULED_JUDGMENT_LABELS = {
    "matched": "匹配到某个计划班次",
    "no_scheduled_match": "没有对应的计划班次（空选项）",
    "ambiguous_candidates": "候选之间无法区分",
    "insufficient_evidence": "证据不足，无法判断",
}

ACTIVITY_OPTIONS = [
    "revenue_trip_candidate",
    "terminal_turnaround",
    "idling",
    "deadhead_or_nonrevenue_unconfirmed",
    "pullin_or_pullout_unconfirmed",
    "coverage_censored",
    "unknown",
]

ACTIVITY_LABELS = {
    "revenue_trip_candidate": "疑似营运班次",
    "terminal_turnaround": "终点站周转",
    "idling": "怠速停放",
    "deadhead_or_nonrevenue_unconfirmed": "空驶 / 非营运（未确认）",
    "pullin_or_pullout_unconfirmed": "回场 / 出场（未确认）",
    "coverage_censored": "覆盖被截断",
    "unknown": "无法判断",
}

QUALITY_FLAG_OPTIONS = [
    "simultaneous_positions",
    "impossible_speed",
    "unresolved_dst_fold",
    "trajectory_not_split",
    "missing_avl_coverage",
    "spatial_temporal_conflict",
    "candidate_universe_incomplete",
    "timezone_text_inconsistent",
    "other",
]

QUALITY_FLAG_LABELS = {
    "simultaneous_positions": "同一时刻出现多个位置",
    "impossible_speed": "速度不可能达到",
    "unresolved_dst_fold": "夏令时回拨重叠小时未能消歧",
    "trajectory_not_split": "轨迹本该切分却未切分",
    "missing_avl_coverage": "AVL 覆盖缺失",
    "spatial_temporal_conflict": "时空信息互相矛盾",
    "candidate_universe_incomplete": "候选全集不完整",
    "timezone_text_inconsistent": "时区文字前后不一致",
    "other": "其他",
}

FRAGMENT_ROLE_OPTIONS = [
    "trip_core",
    "arrival_fragment",
    "departure_fragment",
    "turnaround",
    "stitch_previous",
    "stitch_next",
    "independent_short_trip",
    "ambiguous_fragment",
]

FRAGMENT_ROLE_LABELS = {
    "trip_core": "trip 主体（这实际上是一整趟的核心部分）",
    "arrival_fragment": "到达片段（一趟车的尾段）",
    "departure_fragment": "出发片段（一趟车的首段）",
    "turnaround": "周转",
    "stitch_previous": "应与前一个对象缝合",
    "stitch_next": "应与后一个对象缝合",
    "independent_short_trip": "独立的短程 trip",
    "ambiguous_fragment": "无法判断角色的片段",
}

ARRIVAL_ROLE_LABELS = {
    "unresolved": "未解决",
    "trip_end": "某趟车的终点",
    "terminal_arrival": "终点站到达",
}

DEPARTURE_ROLE_LABELS = {
    "unresolved": "未解决",
    "trip_start": "某趟车的起点",
    "terminal_departure": "终点站发车",
}

COMPLETE_TRIP_LABELS = {
    "not_applicable": "不适用（这不是一整趟车）",
    "insufficient_evidence": "证据不足",
    "ambiguous_candidates": "候选之间无法区分",
}


def _labeler(mapping: dict[str, str]):
    """显示层翻译；未收录的值原样显示，绝不改变存储值。"""

    return lambda value: mapping.get(value, value)


def render_grain_form(bundle: CaseBundleV2) -> tuple[str | None, str | None]:
    """必答的第一个问题。返回 (grain, reviewability)。"""

    st.markdown("### 1 · 当前这个对象代表什么？")
    st.caption(
        "请只依据上方的证据作答。回答之前不会显示任何建议；无论这个案例"
        "当初被排进哪条队列，你都可以选择任意粒度。"
    )
    labels = [label for _, label in GRAIN_OPTIONS]
    picked = st.radio(
        "案例粒度",
        labels,
        index=None,
        key=f"grain::{bundle.case_id}",
    )
    if picked is None:
        return None, None
    grain = next(value for value, label in GRAIN_OPTIONS if label == picked)
    choices = REVIEWABILITY_BY_GRAIN[grain]
    reviewability = st.selectbox(
        "在这个粒度下的可评性",
        choices,
        format_func=_labeler(REVIEWABILITY_LABELS),
        key=f"reviewability::{bundle.case_id}",
    )
    return grain, reviewability


def confidence_block(
    case_id: str, *, include_match: bool, match_label: str = "匹配置信度"
) -> dict[str, Any]:
    columns = st.columns(3 if include_match else 2)
    grain_confidence = columns[0].select_slider(
        "粒度置信度",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"gc::{case_id}",
    )
    evidence_quality = columns[1].select_slider(
        "证据质量",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"eq::{case_id}",
    )
    result: dict[str, Any] = {
        "grain_confidence": grain_confidence,
        "evidence_quality": evidence_quality,
    }
    if include_match:
        result["match_confidence"] = columns[2].select_slider(
            match_label,
            options=[1, 2, 3, 4, 5],
            value=3,
            key=f"mc::{case_id}",
        )
    return result


def rationale_block(case_id: str, *, for_match: bool) -> dict[str, Any]:
    flags = st.multiselect(
        "结构化理由 —— 哪些证据族支持这个判断？（置信度 4-5 时必填"
        + ("；一个匹配通常需要两个证据族）" if for_match else "）"),
        list(EVIDENCE_FAMILY_LABELS),
        format_func=_labeler(EVIDENCE_FAMILY_LABELS),
        placeholder="选择支持你判断的证据族",
        key=f"flags::{case_id}",
    )
    single = False
    single_family = None
    if for_match:
        single = st.checkbox(
            "单一决定性依据 —— 只有一个证据族，但它本身就足以定论",
            key=f"single::{case_id}",
        )
        if single:
            single_family = st.selectbox(
                "哪一个证据族起决定作用？",
                list(EVIDENCE_FAMILY_LABELS),
                format_func=_labeler(EVIDENCE_FAMILY_LABELS),
                key=f"singlefam::{case_id}",
            )
    rationale_text = st.text_area(
        "自由文字理由（置信度 4-5 时必填）",
        key=f"rat::{case_id}",
        height=80,
    )
    notes = st.text_area("备注（选填）", key=f"notes::{case_id}", height=80)
    result: dict[str, Any] = {
        "evidence_flags": list(flags) + (["single_decisive_anchor"] if single else []),
        "rationale_text": rationale_text,
        "notes": notes,
    }
    if single_family:
        result["single_decisive_anchor_family"] = single_family
    return result


def render_scheduled_form(
    bundle: CaseBundleV2, visible: list, *, key_prefix: str = ""
) -> dict[str, Any] | None:
    case_id = bundle.case_id
    prefix = key_prefix or "sched"
    st.markdown("### 2 · 计划班次判断")
    judgment = st.radio(
        "判断",
        SCHEDULED_JUDGMENT_OPTIONS,
        index=None,
        format_func=_labeler(SCHEDULED_JUDGMENT_LABELS),
        key=f"{prefix}::judgment::{case_id}",
        help=(
            "空选项（没有对应的计划班次）始终可选。任何时候都不要求必须给出"
            "匹配，即使候选只显示了一个也是如此。"
        ),
    )
    alias = None
    if judgment == "matched":
        alias = alias_selector(
            bundle,
            visible,
            key=f"{prefix}::alias::{case_id}",
            label="匹配到的候选（从上方候选全集中选择）",
            allow_none=True,
            none_label="— 请选择一个候选 —",
        )
        if alias is None:
            st.info("选定匹配的候选后才能存盘")
    if judgment is None:
        return None
    record: dict[str, Any] = {"scheduled_judgment": judgment}
    if judgment == "matched":
        if alias is None:
            return None
        record["selected_candidate_alias"] = alias
    record.update(confidence_block(case_id, include_match=judgment == "matched"))
    record.update(rationale_block(case_id, for_match=judgment == "matched"))
    return record


def render_multi_trip_form(
    bundle: CaseBundleV2, visible: list
) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · 多 trip 分段")
    st.caption(
        "只描述你能站得住脚的子行程。边界填的是**相对 chunk 起点的秒数区间**，"
        "不要求给出精确的瞬时时刻。每个子行程另有子案例可供单独评审；"
        "这里不会复制任何此前的备注。"
    )
    duration = bundle.chunk.duration_s or 0
    count = int(
        st.number_input(
            "运营子行程的数量",
            min_value=1,
            max_value=8,
            value=2,
            key=f"mt::count::{case_id}",
        )
    )
    sequence: list[str | None] = []
    for index in range(count):
        alias = alias_selector(
            bundle,
            visible,
            key=f"mt::sub{index}::{case_id}",
            label=f"第 {index + 1} 个子行程对应的候选（或选择无 / 未解决）",
        )
        sequence.append(bundle.by_alias(alias).candidate_key if alias else None)
    intervals: list[list[int]] = []
    for index in range(max(0, count - 1)):
        columns = st.columns(2)
        low = int(
            columns[0].number_input(
                f"边界 {index + 1} —— 最早（chunk 起点后第几秒）",
                min_value=-86400,
                max_value=2 * 86400,
                value=0,
                key=f"mt::blo{index}::{case_id}",
            )
        )
        high = int(
            columns[1].number_input(
                f"边界 {index + 1} —— 最晚（chunk 起点后第几秒）",
                min_value=-86400,
                max_value=2 * 86400,
                value=min(duration, max(0, low)),
                key=f"mt::bhi{index}::{case_id}",
            )
        )
        if high < low:
            st.error("边界区间的终点早于起点")
            return None
        if low < 0 or (duration and high > duration):
            st.warning(
                f"边界区间 {index + 1} 落在所显示的 chunk 之外"
                f"（0 … {fmt_duration_s(duration)}）—— 存盘时会附带一条"
                "明确的越界警告"
            )
        intervals.append([low, high])
    unresolved = int(
        st.number_input(
            "无法归属的子段数量",
            min_value=0,
            max_value=8,
            value=0,
            key=f"mt::unres::{case_id}",
        )
    )
    split_confidence = st.select_slider(
        "切分置信度",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"mt::sc::{case_id}",
    )
    record: dict[str, Any] = {
        "subtrip_count": count,
        "ordered_candidate_sequence": sequence,
        "split_intervals_rel_s": intervals,
        "split_confidence": split_confidence,
        "unresolved_subsegments": unresolved,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_boundary_form(
    bundle: CaseBundleV2, visible: list, *, derived_tz_preview
) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · 跨边界双角色判断")
    st.caption(
        "进站侧和出站侧是相互独立的：两侧可以选不同的候选，任意一侧也都可以"
        "保持未解决。这条记录不会被强行归给某一个唯一的归属者。"
    )
    inbound = alias_selector(
        bundle,
        visible,
        key=f"bd::in::{case_id}",
        label="进站侧（到达方向）的候选",
    )
    outbound = alias_selector(
        bundle,
        visible,
        key=f"bd::out::{case_id}",
        label="出站侧（发车方向）的候选",
    )
    stop_hypothesis = st.text_input(
        "边界所在停站的假设（填 stop code，选填）",
        key=f"bd::stop::{case_id}",
    )
    duration = bundle.chunk.duration_s or 0
    columns = st.columns(2)
    low = int(
        columns[0].number_input(
            "边界区间 —— 最早（chunk 起点后第几秒）",
            min_value=-86400,
            max_value=2 * 86400,
            value=0,
            key=f"bd::lo::{case_id}",
        )
    )
    high = int(
        columns[1].number_input(
            "边界区间 —— 最晚（chunk 起点后第几秒）",
            min_value=-86400,
            max_value=2 * 86400,
            value=duration,
            key=f"bd::hi::{case_id}",
        )
    )
    if high < low:
        st.error("边界区间的终点早于起点")
        return None
    if low < 0 or (duration and high > duration):
        st.warning(
            "提议的边界落在所显示的 chunk **之外**"
            f"（0 … {fmt_duration_s(duration)}）；存盘时会附带一条明确的"
            "越界警告 —— 请先查看相邻上下文"
        )
    tz_label = derived_tz_preview((low + high) // 2)
    if tz_label:
        st.caption(
            f"该边界处推导出的时区为 **{tz_label}** "
            "（由时间戳推导 —— 审阅人手写的 EDT/EST 文字一律不采信）"
        )
    columns = st.columns(3)
    arrival_role = columns[0].selectbox(
        "到达侧的角色",
        ["unresolved", "trip_end", "terminal_arrival"],
        format_func=_labeler(ARRIVAL_ROLE_LABELS),
        key=f"bd::ar::{case_id}",
    )
    departure_role = columns[1].selectbox(
        "出发侧的角色",
        ["unresolved", "trip_start", "terminal_departure"],
        format_func=_labeler(DEPARTURE_ROLE_LABELS),
        key=f"bd::dr::{case_id}",
    )
    dual_role = columns[2].checkbox(
        "这条记录需要双重归属",
        value=True,
        key=f"bd::dual::{case_id}",
    )
    record: dict[str, Any] = {
        "inbound_candidate_key": (
            bundle.by_alias(inbound).candidate_key if inbound else None
        ),
        "outbound_candidate_key": (
            bundle.by_alias(outbound).candidate_key if outbound else None
        ),
        "boundary_stop_hypothesis": stop_hypothesis or None,
        "boundary_time_interval_rel_s": [low, high],
        "arrival_role": arrival_role,
        "departure_role": departure_role,
        "dual_role": dual_role,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_fragment_form(bundle: CaseBundleV2, visible: list) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · 片段角色与缝合")
    st.caption(
        "局部的计划班次上下文**不等于**执行班次身份：指出这个片段落在哪个"
        "班次的时刻表范围内，并不表示这个片段就是那一整趟车。"
    )
    role = st.radio(
        "片段角色",
        FRAGMENT_ROLE_OPTIONS,
        index=None,
        format_func=_labeler(FRAGMENT_ROLE_LABELS),
        key=f"fr::role::{case_id}",
    )
    if role is None:
        return None
    columns = st.columns(2)
    stitch_previous = columns[0].selectbox(
        "应与前一个对象缝合吗？",
        TRISTATE_OPTIONS,
        index=3,
        format_func=_labeler(TRISTATE_LABELS),
        key=f"fr::sp::{case_id}",
    )
    stitch_next = columns[1].selectbox(
        "应与后一个对象缝合吗？",
        TRISTATE_OPTIONS,
        index=3,
        format_func=_labeler(TRISTATE_LABELS),
        key=f"fr::sn::{case_id}",
    )
    local_alias = alias_selector(
        bundle,
        visible,
        key=f"fr::local::{case_id}",
        label="局部计划**上下文**候选（选填；这不是一个班次匹配）",
    )
    complete_trip = st.selectbox(
        "在当前粒度下，对「完整执行班次」的判断",
        ["not_applicable", "insufficient_evidence", "ambiguous_candidates"],
        format_func=_labeler(COMPLETE_TRIP_LABELS),
        key=f"fr::ct::{case_id}",
        help=(
            "片段在这里不能被提升为一个完整班次的匹配；如果你确信它其实就是"
            "一整趟车，请回到上面去修改案例粒度。"
        ),
    )
    fragment_confidence = st.select_slider(
        "片段角色置信度",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"fr::fc::{case_id}",
    )
    record: dict[str, Any] = {
        "fragment_role": role,
        "stitch_previous": stitch_previous,
        "stitch_next": stitch_next,
        "local_scheduled_context_candidate_key": (
            bundle.by_alias(local_alias).candidate_key if local_alias else None
        ),
        "complete_trip_judgment": complete_trip,
        "fragment_confidence": fragment_confidence,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_quality_form(bundle: CaseBundleV2) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · 源数据 / 时间质量判断")
    st.caption(
        "当轨迹表征本身无效时，不会要求你做计划班次选择 —— 质量问题不是候选之间的歧义。"
    )
    flags = st.multiselect(
        "质量标记（所有适用项）",
        QUALITY_FLAG_OPTIONS,
        format_func=_labeler(QUALITY_FLAG_LABELS),
        placeholder="选择所有适用的质量标记",
        key=f"q::flags::{case_id}",
    )
    if not flags:
        st.info("请至少选择一个质量标记")
        return None
    dominant = st.selectbox(
        "占主导的质量状态",
        flags,
        format_func=_labeler(QUALITY_FLAG_LABELS),
        key=f"q::state::{case_id}",
    )
    applicable = st.checkbox(
        "尽管存在上述问题，计划班次判断仍然适用",
        value=False,
        key=f"q::app::{case_id}",
    )
    record: dict[str, Any] = {
        "quality_state": dominant,
        "quality_flags": flags,
        "scheduled_match_applicable": applicable,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record


def render_activity_form(bundle: CaseBundleV2) -> dict[str, Any] | None:
    case_id = bundle.case_id
    st.markdown("### 2 · 活动状态判断")
    st.caption(
        "长时间停留并不自动等于站务停歇、回场/出场或空驶 —— 这些语义尚未"
        "确定（未决事项 D-11）；带 *_unconfirmed 的状态用于记录这个假设，"
        "但不声称它成立。"
    )
    activity = st.radio(
        "活动状态",
        ACTIVITY_OPTIONS,
        index=None,
        format_func=_labeler(ACTIVITY_LABELS),
        key=f"act::state::{case_id}",
    )
    if activity is None:
        return None
    applicable = st.checkbox(
        "对这个对象而言，计划班次判断是适用的",
        value=False,
        key=f"act::app::{case_id}",
    )
    no_match_confidence = st.select_slider(
        "「无计划班次匹配」的置信度",
        options=[1, 2, 3, 4, 5],
        value=3,
        key=f"act::nm::{case_id}",
    )
    record: dict[str, Any] = {
        "activity_state": activity,
        "scheduled_match_applicable": applicable,
        "match_confidence": no_match_confidence,
    }
    record.update(confidence_block(case_id, include_match=False))
    record.update(rationale_block(case_id, for_match=False))
    return record
