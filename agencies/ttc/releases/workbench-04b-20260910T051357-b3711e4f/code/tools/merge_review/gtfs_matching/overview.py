"""Read-only, compact day overview built from persisted matching evidence.

No projection, score, threshold, delay or final decision is recomputed here.
Incompatible references stay separate and APC remains stop-time intervals.
"""

from __future__ import annotations

import copy
from collections import Counter

from .decision import DEFAULTS
from .diagnostic_candidates import (
    DISPLAY_FIELDS,
    build_diagnostic_candidates,
    finite,
    gate_checks,
    identity,
    is_continuous,
    scheduled_knots,
)
from .retrieval import for_reference
from .scoring import fragments
from .windows import observation_key, usable

MATCHING_MODES = ("global", "early", "middle", "late")
MODE_LABELS = {"global": "全局", "early": "前期", "middle": "中期", "late": "后期"}


def validate_matching_mode(mode):
    if not isinstance(mode, str) or mode not in MATCHING_MODES:
        raise ValueError("matching_mode 必须是 global、early、middle 或 late")
    return mode


def mode_entry(artifact, matching_mode="global"):
    """Never silently substitute global evidence for an uncomputed partial mode."""
    validate_matching_mode(matching_mode)
    modes = artifact.get("matching_modes", {})
    if matching_mode in modes:
        entry = modes[matching_mode]
        if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
            raise ValueError("已保存的匹配模式结果格式不完整")
        return entry
    if matching_mode != "global":
        raise ValueError(
            f"此历史版本没有计算 {matching_mode} 匹配模式；请选择四模式版本"
        )
    return {
        "result": artifact["result"],
        "selection": {
            "mode": "global",
            "label": MODE_LABELS["global"],
            "basis": "original_full_segment",
            "status": "LEGACY_GLOBAL",
            "time_ranges": [],
        },
    }


def available_modes(artifact):
    return [
        m
        for m in MATCHING_MODES
        if m == "global" or m in artifact.get("matching_modes", {})
    ]


def visual_fragments(rows, config=None):
    """Render the selected scorer's physical continuity without joining gaps."""
    if not (config or {}).get("continuous_matching_enabled"):
        return fragments(rows, config)
    from .continuous.config import get_config
    from .continuous.evidence import valid_projection

    c = get_config(config)
    output, previous = [], None
    for row in sorted(
        (row for row in rows if finite(row.get("t"))),
        key=lambda row: (row["t"], observation_key(row)),
    ):
        if not valid_projection(row, c):
            previous = None
            continue
        if (
            previous is None
            or row.get("scope_separator_before")
            or row.get("reference_id") != previous.get("reference_id")
            or row.get("fragment_id") != previous.get("fragment_id")
            or row["t"] - previous["t"] > c["continuous_coverage_max_gap_s"]
            or row["s_m"] < previous["s_m"] - c.get("score_backtrack_tolerance_m", 25)
        ):
            output.append([])
        output[-1].append(row)
        previous = row
    return output


def select_overview_candidate(result, config=None):
    """Lowest-cost all-gates-pass aggregate, else first actual failed hypothesis.

    Final-list membership is not acceptance: guarded final lists may contain
    cost-failed near competitors, and valid aggregates can exceed the display cap.
    An accepted overlay still does not assert a unique or confirmed trip identity.
    """
    c = DEFAULTS | (config or {})
    accepted = []
    seen = set()
    for row in result.get("all_candidates", []):
        candidate_id = identity(row)
        if candidate_id is None or candidate_id in seen:
            continue
        seen.add(candidate_id)
        checks = gate_checks(row, c, result)
        if all(check["passed"] for check in checks):
            accepted.append((row["score"], candidate_id, row, checks))
    if accepted:
        _, candidate_id, original, checks = min(accepted, key=lambda item: item[:2])
        candidate = {
            key: copy.deepcopy(value)
            for key, value in original.items()
            if key in DISPLAY_FIELDS - {"scheduled_trajectory"}
        }
        candidate.update(
            candidate_id=candidate_id,
            trajectory=scheduled_knots(original),
            gate_checks=checks,
            failed_gate_checks=[],
            source="accepted",
            diagnostic_only=False,
        )
    else:
        # The overview has no final-list display exclusion. A genuinely failed
        # candidate already displayed in the detail panel remains a valid fallback.
        diagnostic = build_diagnostic_candidates({**result, "candidates": []}, c)
        if not diagnostic["candidates"]:
            return None
        candidate = diagnostic["candidates"][0]
        candidate.pop("scheduled_trajectory", None)
        candidate["source"] = "fallback"
    candidate.update(
        status=result.get("status", "UNMATCHED"),
        probability=candidate.get("probability") if is_continuous(candidate) else None,
        calibrated=candidate.get("calibrated") is True
        if is_continuous(candidate)
        else False,
        trip_identity_confirmed=False,
        schedule_time_basis="original_scheduled_wall_clock",
    )
    # The day overview only needs compact knots, not repeated per-stop mappings.
    candidate["trajectory"] = [
        {key: row[key] for key in ("t", "s", "kind", "stop_id") if key in row}
        for row in candidate["trajectory"]
        if finite(row.get("t")) and finite(row.get("s"))
    ]
    return candidate


def _overview_fragments(points, config, candidate, selection, mode):
    selected = set(
        selection.get("selected_valid_avl_ids", selection.get("selected_avl_ids", []))
    )
    groups = []
    used = set()
    candidate_reference = candidate.get("reference_id") if candidate else None
    if candidate_reference:
        trace = for_reference(points, candidate_reference, include_unprojectable=True)
        for group in visual_fragments(trace, config):
            groups.append((group, False))
            used.update(observation_key(row) for row in group)
    # Preserve valid primary context that cannot be projected to the selected
    # candidate reference. Mask already displayed rows so removal cannot connect
    # around an OFF_REFERENCE point, branch/reference switch or service gap.
    primary_context = [
        dict(row, projection_status="UNPROJECTABLE", quality=0)
        if observation_key(row) in used
        else row
        for row in points
    ]
    groups.extend(
        (group, bool(candidate_reference))
        for group in visual_fragments(primary_context, config)
    )
    output = []
    for index, (group, context_only) in enumerate(groups):
        reference_id = group[0].get("reference_id")
        output.append(
            {
                "reference_id": reference_id,
                "fragment_id": f"{reference_id}:overview-fragment:{index}",
                "context_only": context_only,
                "points": [
                    [
                        row["t"],
                        row["s_m"],
                        str(row["rid"]),
                        mode == "global" or observation_key(row) in selected,
                    ]
                    for row in group
                ],
            }
        )
    return output


def _overview_apc(evidence, selection, mode, candidate):
    selected = set(selection.get("selected_apc_ids", []))
    ranges = selection.get("time_ranges", [])
    preferred_reference = candidate.get("reference_id") if candidate else None
    output = []
    for row in evidence:
        start = row.get("arrival", row.get("start_time"))
        end = row.get("departure", row.get("end_time"))
        if not finite(start) or not finite(end) or end < start:
            continue
        intervals = row.get("canonical_stop_intervals", [])
        preferred = [
            i for i in intervals if i.get("reference_id") == preferred_reference
        ]
        for interval in preferred or intervals:
            reference_id = interval.get("reference_id")
            alternative = next(
                (
                    a
                    for a in row.get("projection_alternatives", [])
                    if a.get("reference_id") == reference_id
                ),
                {},
            )
            status = alternative.get("projection_status", row.get("projection_status"))
            if status not in {"ON_REFERENCE", "STOP_INTERVAL_ONLY"}:
                continue
            lower = interval.get("s_min_m", interval.get("s_m"))
            upper = interval.get("s_max_m", lower)
            if not finite(lower) or not finite(upper):
                continue
            selected_for_decision = mode == "global" or observation_key(row) in selected
            selected_intervals = []
            if selected_for_decision:
                if mode == "global":
                    selected_intervals = [{"start": start, "end": end}]
                else:
                    selected_intervals = [
                        {
                            "start": max(start, span["start"]),
                            "end": min(end, span["end"]),
                        }
                        for span in ranges
                        if finite(span.get("start"))
                        and finite(span.get("end"))
                        and start <= span["end"]
                        and end >= span["start"]
                        and not (
                            start == span["end"]
                            and not span.get("end_inclusive", False)
                        )
                    ]
            output.append(
                {
                    "reference_id": reference_id,
                    "rid": str(row["rid"]),
                    "start": start,
                    "end": end,
                    "s": lower
                    if lower == upper and not interval.get("ambiguous")
                    else None,
                    "s_min_m": lower,
                    "s_max_m": upper,
                    "boarding": row.get("boarding"),
                    "alighting": row.get("alighting"),
                    "confidence": row.get("confidence"),
                    "status": status,
                    "selected_for_decision": selected_for_decision,
                    "selected_intervals": selected_intervals,
                    "trajectory_fabricated": False,
                }
            )
    return output


def overview_case(
    artifact, summary, vehicle, vehicle_label, canonical, matching_mode="global"
):
    """One compact record; caller has already hash-verified or built the artifact."""
    entry = mode_entry(artifact, matching_mode)
    result, selection = entry["result"], copy.deepcopy(entry.get("selection", {}))
    config = artifact.get("config", {})
    candidate = select_overview_candidate(result, config)
    points = artifact.get("projection", {}).get("points", [])
    groups = _overview_fragments(points, config, candidate, selection, matching_mode)
    valid_ids = {str(row["rid"]) for row in points if usable(row, config)}
    displayed_ids = {point[2] for group in groups for point in group["points"]}
    return {
        "id": summary["id"],
        "vehicle": vehicle,
        "vehicle_label": str(vehicle_label),
        "direction_label": summary.get(
            "direction_label", summary.get("direction", "UNKNOWN")
        ),
        "status": result.get("status", "UNMATCHED"),
        "start": summary.get("source_start", summary.get("start")),
        "end": summary.get("source_end", summary.get("end")),
        "matching_mode": matching_mode,
        "decision_selection": selection,
        "fragments": groups,
        "apc_intervals": _overview_apc(
            artifact.get("apc_evidence", []), selection, matching_mode, candidate
        ),
        "candidate": candidate,
        "candidate_source": candidate["source"] if candidate else "none",
        "valid_primary_point_count": len(valid_ids),
        "displayed_projection_point_count": len(displayed_ids),
        "omitted_valid_primary_point_count": len(valid_ids - displayed_ids),
        "raw_avl_count": len(points),
        "off_reference_count": sum(
            row.get("projection_status") == "OFF_REFERENCE" for row in points
        ),
        "reasons": copy.deepcopy(result.get("reasons", [])),
    }


def build_day_overview(
    case_records, canonical, *, run_id, day, matching_mode, available_matching_modes
):
    validate_matching_mode(matching_mode)
    cases = sorted(
        case_records,
        key=lambda row: (str(row["vehicle"]), row.get("start") or 0, row["id"]),
    )
    return {
        "run_id": run_id,
        "day": day,
        "route_scope": ["29"],
        "matching_mode": matching_mode,
        "available_matching_modes": list(available_matching_modes),
        "reference_facets": [
            {
                **{
                    key: reference.get(key)
                    for key in ("reference_id", "direction_label", "length_m")
                },
                "shape_id": reference.get(
                    "canonical_shape_id", reference.get("shape_id")
                ),
            }
            for reference in canonical.get("references", [])
        ],
        "cases": cases,
        "summary": {
            "case_count": len(cases),
            "vehicle_count": len({row["vehicle"] for row in cases}),
            "statuses": dict(Counter(row["status"] for row in cases)),
            "candidate_sources": dict(
                Counter(row["candidate_source"] for row in cases)
            ),
            "displayed_projection_point_count": sum(
                row["displayed_projection_point_count"] for row in cases
            ),
            "omitted_valid_primary_point_count": sum(
                row["omitted_valid_primary_point_count"] for row in cases
            ),
        },
        "matching_recomputed": False,
        "schedule_time_basis": "original_scheduled_wall_clock",
        "candidate_policy": (
            "lowest_cost_all_gates_pass_else_lowest_cost_genuine_gate_failure"
        ),
        "trip_identity_confirmed": False,
        "warning": (
            "全天总览仅展示一个候选；通过门槛不等于唯一或已确认的班次。"
            "虚线 fallback 未过门槛。不同 reference 分面显示，APC 仅为站点时间区间。"
        ),
    }
