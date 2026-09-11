"""Post-save panel: comparison, logged reveals, revisions, adjudication.

Everything on this page exists only after the blind judgment is durably
saved. Reveals are explicit, logged actions; a post-reveal change of mind is
a new record that never overwrites the blind save; adjudication is a third
record that overwrites neither pass.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ..case_loader import CaseBundleV2, load_diagnostics
from ..tables import show_table

FIRST_PASS_FIELDS = (
    "raw_primary_label",
    "raw_selected_card",
    "raw_selected_candidate_id",
    "raw_confidence",
    "raw_rationale",
    "raw_notes",
)


def _blind_summary(save: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "case_grain",
        "reviewability",
        "scheduled_judgment",
        "selected_candidate_alias",
        "selected_candidate_key",
        "fragment_role",
        "activity_state",
        "quality_state",
        "grain_confidence",
        "match_confidence",
        "evidence_quality",
    )
    return [
        {"字段": key, "你的盲审判断": save.get(key)}
        for key in keys
        if save.get(key) is not None
    ]


def render_post_save(bundle: CaseBundleV2, review_store, store_ctx) -> None:
    case_id = bundle.case_id
    case_state = review_store.case_state(case_id)
    save = case_state.blind_save or {}
    st.success(
        "盲审判断已存盘，且不可修改。下方的任何操作都会另存为一条独立的、"
        "带日志的记录 —— 这次盲审存盘本身不会被改动。"
    )
    show_table(_blind_summary(save))
    comparison = case_state.comparison
    if comparison is not None:
        outcome = comparison.get("outcome")
        st.markdown(f"**一审 / 二审比对结果：** `{outcome}`")
        st.caption(comparison.get("basis", ""))
        if outcome in ("FIRST_SECOND_DISAGREE", "NOT_EVALUABLE_AT_CURRENT_GRAIN"):
            st.warning("该案例已进入裁决队列。系统不会自动偏袒任何一遍的判断。")
    if bundle.is_child:
        st.caption("子案例：不存在可比对的一审判断。")

    if not bundle.is_child:
        with st.expander("揭示一审判断（会记录日志）"):
            reveal_key = f"fprevealed::{case_id}"
            if st.button(
                "揭示该案例的一审标签",
                key=f"fpreveal::{case_id}",
            ):
                try:
                    st.session_state[reveal_key] = review_store.first_pass.for_case(
                        case_id, reviewer_id=review_store.reviewer_id
                    )
                except (KeyError, OSError) as error:
                    st.warning(f"一审记录不可用：{error}")
            first = st.session_state.get(reveal_key)
            if first:
                show_table(
                    [
                        {"字段": field, "一审": first.get(field)}
                        for field in FIRST_PASS_FIELDS
                    ]
                )

    with st.expander("揭示模型诊断（会记录日志）"):
        st.caption(
            "仅供学习与审计之用。这次揭示会写入评审历史；你已存盘的盲审判断"
            "不受任何影响。"
        )
        if st.button("揭示诊断信息", key=f"diagreveal::{case_id}"):
            review_store.record_diagnostics_reveal(case_id)
            st.session_state[f"diagrevealed::{case_id}"] = True
        if st.session_state.get(f"diagrevealed::{case_id}"):
            diagnostics = load_diagnostics(store_ctx.artifact_store, bundle)
            anchor = diagnostics.get("anchor_decision")
            if anchor:
                show_table([anchor])
            scores = diagnostics.get("candidate_scores") or []
            if scores:
                show_table(scores, max_rows=20)
            if not anchor and not scores:
                st.info("没有可用的诊断产物")

    with st.expander("记录「看过之后改变了想法」（另存为新记录）"):
        st.caption(
            "如果在看到比对结果或诊断信息之后你的判断会不同，请在这里描述"
            "修正后的判断。它会以 post_reveal_revision 的形式存储，只引用"
            "盲审存盘记录，绝不替换它。"
        )
        revised = st.text_area(
            "修正后的判断（自由文字；结构化的修正在裁决环节进行）",
            key=f"prtext::{case_id}",
        )
        if st.button("记录这条事后说明", key=f"prsave::{case_id}"):
            if revised.strip():
                review_store.log_event(
                    {
                        "event": "post_reveal_revision",
                        "case_id": case_id,
                        "reviewer_id": review_store.reviewer_id,
                        "post_reveal_revision_of": save.get("record_id"),
                        "revised_judgment_text": revised.strip(),
                    }
                )
                st.success("已作为一条独立的事后事件记录")
            else:
                st.info("请先写出修正后的判断")

    if "REQUIRES_ADJUDICATION" in case_state.states:
        with st.expander("裁决（第三条记录；两遍原始判断都不覆盖）"):
            st.caption(
                "最终判断由你选择的处理方式派生：采信某一遍，就记录那一遍的"
                "判断；选择暂缓，则不记录任何最终判断。"
            )
            resolution = st.selectbox(
                "处理方式",
                [
                    "second_pass_upheld",
                    "first_pass_upheld",
                    "new_judgment",
                    "not_evaluable_at_current_grain",
                    "deferred",
                ],
                key=f"adjres::{case_id}",
            )
            adjudication: dict = {"case_id": case_id, "resolution": resolution}
            if resolution == "new_judgment":
                adjudication["resolved_case_grain"] = st.selectbox(
                    "裁定的案例粒度",
                    [
                        "single_trip_candidate",
                        "multi_trip_chunk",
                        "local_fragment",
                        "boundary_spanning",
                        "turnaround_or_idle",
                        "data_quality_invalid",
                        "coverage_censored",
                        "unknown",
                    ],
                    key=f"adjgrain::{case_id}",
                )
                adjudication["resolved_scheduled_judgment"] = st.selectbox(
                    "裁定的计划班次判断",
                    [
                        "not_applicable",
                        "matched",
                        "no_scheduled_match",
                        "ambiguous_candidates",
                        "insufficient_evidence",
                    ],
                    key=f"adjjudg::{case_id}",
                )
                if adjudication["resolved_scheduled_judgment"] == "matched":
                    complete = [
                        c
                        for c in bundle.real_candidates
                        if c.kind == "complete_scheduled_trip"
                    ]
                    alias = st.selectbox(
                        "裁定的候选（取自冻结的候选全集）",
                        [c.alias for c in complete],
                        key=f"adjalias::{case_id}",
                    )
                    picked = bundle.by_alias(alias)
                    adjudication["resolved_candidate_key"] = (
                        picked.candidate_key if picked else None
                    )
            rationale = st.text_area(
                "裁决理由（必填）",
                key=f"adjrat::{case_id}",
            )
            if st.button("保存裁决", key=f"adjsave::{case_id}"):
                adjudication["resolution_rationale"] = rationale.strip()
                try:
                    review_store.save_adjudication(adjudication)
                except Exception as error:  # noqa: BLE001 - surfaced to user
                    st.error(str(error))
                else:
                    st.success("裁决已记录")
                    st.rerun()

    with st.expander("申请追加一个子案例"):
        task = st.selectbox(
            "子案例任务类型",
            [
                "subtrip_scheduled_match",
                "boundary_interval",
                "inbound_context",
                "outbound_context",
                "fragment_role",
                "stitch_decision",
                "source_time_quality",
                "activity_state",
                "sequence_review",
            ],
            key=f"ccr::task::{case_id}",
        )
        detail = st.text_input(
            "这个子案例需要评审什么？",
            key=f"ccr::detail::{case_id}",
        )
        if st.button("记录子案例申请", key=f"ccr::save::{case_id}"):
            if detail.strip():
                review_store.record_child_case_request(
                    bundle.parent_case_id,
                    child_task_type=task,
                    detail=detail.strip(),
                )
                st.success("申请已写入评审历史（并未因此创建任何子案例标签）")
            else:
                st.info("请先描述这个申请的内容")

    with st.expander("存盘记录原文（restricted-local，仅本地）"):
        st.code(json.dumps(save, indent=2, sort_keys=True, default=str))
