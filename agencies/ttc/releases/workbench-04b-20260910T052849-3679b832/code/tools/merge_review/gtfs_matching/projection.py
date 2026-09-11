"""Per-reference sequence Viterbi projection with explicit rejected evidence."""

from __future__ import annotations

import collections
import copy
import math
from datetime import datetime

from .canonical import finite, make_index, project_candidates, valid_coordinate
from .off_reference import build_fragments, classify_off_reference

DEFAULTS = {
    "max_projection_distance_m": 100.0,
    "projection_candidate_search_m": 500.0,
    "projection_sigma_m": 25.0,
    "projection_heading_weight": 2.0,
    "projection_heading_min_speed_kph": 5.0,
    "projection_direction_prior": 4.0,
    "projection_max_speed_mps": 36.1111111111,
    "projection_speed_slack_m": 20.0,
    "projection_backtrack_m": 35.0,
    "projection_top_k": 6,
    "projection_beam_width": 12,
    "projection_skip_penalty": 50.0,
    "projection_restart_after_s": 60.0,
}
EPOCH = datetime(1970, 1, 1)


def wall_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("Projection requires naive local source clocks")
        return (value - EPOCH).total_seconds()
    return float(value) if finite(value) else None


def _references(value):
    refs = value["references"] if isinstance(value, dict) else value
    identities = {(r["feed_version"], r["route_id"]) for r in refs}
    if len(identities) > 1:
        raise ValueError("Projection must not mix feed versions or routes")
    if len({r["reference_id"] for r in refs}) != len(refs):
        raise ValueError("Duplicate reference IDs")
    return refs


def _config(config):
    cfg = DEFAULTS | (config or {})
    for key in DEFAULTS:
        if not finite(cfg[key]) or cfg[key] < 0:
            raise ValueError(f"Invalid {key}")
    for key in ("projection_top_k", "projection_beam_width"):
        if not isinstance(cfg[key], int) or cfg[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if cfg["projection_sigma_m"] <= 0:
        raise ValueError("Projection distance scale must be positive")
    return cfg


def _label(row):
    label = row.get("direction_label")
    if label not in {"NORTH", "SOUTH", "EAST", "WEST", "INBOUND", "OUTBOUND"}:
        text = str(row.get("destination") or "").strip().upper()
        token = text.split(maxsplit=1)[0] if text else ""
        label = (
            token
            if token in {"NORTH", "SOUTH", "EAST", "WEST", "INBOUND", "OUTBOUND"}
            else None
        )
    return label


def _emission(point, candidate, reference, cfg):
    cost = (candidate["lateral_m"] / cfg["projection_sigma_m"]) ** 2
    heading, speed = point.get("heading"), point.get("speed")
    error = None
    if (
        finite(heading)
        and 0 <= heading < 360
        and finite(speed)
        and speed >= cfg["projection_heading_min_speed_kph"]
    ):
        error = abs((heading - candidate["heading_deg"] + 180) % 360 - 180)
        cost += cfg["projection_heading_weight"] * (error / 90) ** 2
    direction = _label(point)
    fallback = direction is not None and reference.get("direction_label") not in {
        None,
        direction,
    }
    if fallback:
        cost += cfg["projection_direction_prior"]
    return dict(
        candidate,
        emission_cost=cost,
        heading_error_deg=error,
        direction_fallback=fallback,
        quality=math.exp(-min(cost, 100) / 8) * (1.0 if error is not None else 0.8),
    )


def _viterbi(points, layers, reference, cfg):
    """Beam Viterbi with skip states carrying the actual last accepted time.

    Skip states are essential: removing an isolated GPS spike must not shorten
    the elapsed time used by the next speed/progress constraint. Reinitializing
    is permitted only after a recorded long evidence gap, never at each point.
    """
    states = [dict(cost=0.0, last=None, tail=None, accepted=0)]
    for index, (point, candidates) in enumerate(zip(points, layers, strict=True)):
        next_states = []
        for state in states:
            next_states.append(
                dict(
                    state,
                    cost=state["cost"] + cfg["projection_skip_penalty"],
                    tail=(index, None, state["tail"]),
                )
            )
        for candidate in candidates:
            emitted = _emission(point, candidate, reference, cfg)
            choices = []
            for state in states:
                previous = state["last"]
                transition, restart = 0.0, False
                if previous is not None:
                    elapsed = point["t"] - previous["t"]
                    ds = candidate["s_m"] - previous["s_m"]
                    feasible = (
                        elapsed >= 0
                        and ds >= -cfg["projection_backtrack_m"]
                        and ds
                        <= cfg["projection_max_speed_mps"] * elapsed
                        + cfg["projection_speed_slack_m"]
                    )
                    if not feasible:
                        if elapsed < cfg["projection_restart_after_s"]:
                            continue
                        restart = True
                        transition += cfg["projection_skip_penalty"]
                    elif ds < 0:
                        transition += (-ds / max(1, cfg["projection_backtrack_m"])) ** 2
                cost = state["cost"] + emitted["emission_cost"] + transition
                choices.append((cost, state, restart))
            if choices:
                cost, parent, restart = min(choices, key=lambda item: item[0])
                selected = dict(emitted, sequence_restart=restart)
                next_states.append(
                    dict(
                        cost=cost,
                        last=dict(t=point["t"], s_m=candidate["s_m"]),
                        tail=(index, selected, parent["tail"]),
                        accepted=parent["accepted"] + 1,
                    )
                )
        # Keep distinct accepted positions/times, rather than many copies of
        # the same hypothesis with different bookkeeping histories.
        next_states.sort(key=lambda s: (s["cost"], -s["accepted"]))
        states, signatures = [], set()
        for state in next_states:
            signature = (
                None
                if state["last"] is None
                else (state["last"]["t"], round(state["last"]["s_m"], 3))
            )
            if signature not in signatures:
                signatures.add(signature)
                states.append(state)
                if len(states) >= cfg["projection_beam_width"]:
                    break
    best = min(states, key=lambda s: (s["cost"], -s["accepted"]))
    output, tail = [None] * len(points), best["tail"]
    while tail is not None:
        index, value, tail = tail
        output[index] = value
    return output, best["cost"] / max(1, len(points))


def _project_chunk(points, refs, cfg, indices):
    traces, costs, geometric, unconstrained = {}, {}, {}, {}
    for reference in refs:
        rid = reference["reference_id"]
        layers = []
        for point in points:
            valid = point["t"] is not None and not any(
                point.get(k) for k in ("invalid", "time_ambiguous", "time_nonexistent")
            )
            candidates = (
                project_candidates(
                    point.get("lat"),
                    point.get("lon"),
                    reference,
                    gate=cfg["projection_candidate_search_m"],
                    top_k=cfg["projection_top_k"],
                    index=indices[rid],
                )
                if valid
                else []
            )
            if valid and not candidates:
                candidates = project_candidates(
                    point.get("lat"),
                    point.get("lon"),
                    reference,
                    top_k=cfg["projection_top_k"],
                )
            layers.append(candidates)
        unconstrained[rid], costs[rid] = _viterbi(points, layers, reference, cfg)
        # The operational gate follows the unconstrained sequence projection.
        # A second constrained sequence search prevents a >gate winner from
        # hiding an alternative, sequence-consistent in-gate path.
        gated = [
            [c for c in layer if c["lateral_m"] <= cfg["max_projection_distance_m"]]
            for layer in layers
        ]
        rescue, rescue_cost = _viterbi(points, gated, reference, cfg)
        needs_rescue = any(
            original is not None
            and original["lateral_m"] > cfg["max_projection_distance_m"]
            or original is None
            and alternative is not None
            for original, alternative in zip(unconstrained[rid], rescue, strict=True)
        )
        # Select a complete valid trace. Splicing individual nodes from two
        # Viterbi paths could reintroduce impossible jumps around a loop.
        traces[rid] = rescue if needs_rescue else unconstrained[rid]
        if needs_rescue:
            costs[rid] = rescue_cost
        geometric[rid] = layers
    for index, point in enumerate(points):
        alternatives = []
        for reference in refs:
            rid = reference["reference_id"]
            selected = traces[rid][index]
            nearest = geometric[rid][index]
            if (
                selected is None
                and not nearest
                and valid_coordinate(point.get("lat"), point.get("lon"))
            ):
                nearest = project_candidates(
                    point["lat"], point["lon"], reference, top_k=1
                )
            invalid = (
                point["t"] is None
                or not valid_coordinate(point.get("lat"), point.get("lon"))
                or any(
                    point.get(k)
                    for k in ("invalid", "time_ambiguous", "time_nonexistent")
                )
            )
            in_gate = any(
                c["lateral_m"] <= cfg["max_projection_distance_m"]
                for c in geometric[rid][index]
            )
            status = (
                "ON_REFERENCE"
                if selected
                else "UNPROJECTABLE"
                if invalid
                else "OFF_REFERENCE"
            )
            rejected_reason = (
                None
                if selected
                else "invalid_source"
                if invalid
                else "no_sequence_consistent_in_gate_path"
                if in_gate
                else "distance_gate_failed"
            )
            ungated = unconstrained[rid][index]
            alternatives.append(
                dict(
                    reference_id=rid,
                    family_id=reference["family_id"],
                    direction_label=reference.get("direction_label"),
                    projection_status=status,
                    s_m=selected["s_m"] if selected else None,
                    lateral_m=selected["lateral_m"]
                    if selected
                    else nearest[0]["lateral_m"]
                    if nearest
                    else None,
                    quality=selected["quality"] if selected else 0.0,
                    heading_error_deg=selected["heading_error_deg"]
                    if selected
                    else None,
                    direction_fallback=selected["direction_fallback"]
                    if selected
                    else False,
                    path_cost=costs[rid],
                    sequence_restart=selected.get("sequence_restart", False)
                    if selected
                    else False,
                    rejection_reason=rejected_reason,
                    ungated_s_m=ungated["s_m"] if ungated else None,
                    ungated_lateral_m=ungated["lateral_m"] if ungated else None,
                    gate_applied_after_sequence=True,
                    segment_index=selected["segment_index"] if selected else None,
                    projected_lat=selected["projected_lat"] if selected else None,
                    projected_lon=selected["projected_lon"] if selected else None,
                )
            )
        accepted = [a for a in alternatives if a["projection_status"] == "ON_REFERENCE"]
        if accepted:
            best = min(
                accepted,
                key=lambda a: (
                    a["path_cost"] - math.log(max(a["quality"], 1e-8)) / 4,
                    a["reference_id"],
                ),
            )
            point.update(
                {
                    k: copy.deepcopy(v)
                    for k, v in best.items()
                    if k not in {"direction_label"}
                }
            )
        else:
            statuses = {a["projection_status"] for a in alternatives}
            status = (
                "UNPROJECTABLE"
                if not statuses or statuses == {"UNPROJECTABLE"}
                else "OFF_REFERENCE"
            )
            residuals = [
                a["lateral_m"] for a in alternatives if a["lateral_m"] is not None
            ]
            point.update(
                projection_status=status,
                s_m=None,
                lateral_m=min(residuals, default=None),
                reference_id=None,
                family_id=None,
                quality=0.0,
                direction_fallback=False,
                heading_error_deg=None,
                path_cost=None,
                sequence_restart=False,
                rejection_reason=";".join(
                    sorted(
                        {
                            a["rejection_reason"]
                            for a in alternatives
                            if a["rejection_reason"]
                        }
                    )
                ),
            )
        point["projection_alternatives"] = alternatives
        point["all_references_checked"] = True
        opposite = {
            "NORTH": "SOUTH",
            "SOUTH": "NORTH",
            "EAST": "WEST",
            "WEST": "EAST",
            "INBOUND": "OUTBOUND",
            "OUTBOUND": "INBOUND",
        }.get(_label(point))
        point["opposite_direction_checked"] = opposite is None or any(
            r.get("direction_label") == opposite for r in refs
        )


def project_apc_intervals(apc, refs, config=None):
    """Coordinate-gated APC evidence; missing GPS may supply a stop interval.

    APC stop values are looked up as GTFS stop_code, never assumed stop_id.
    Ambiguous stop codes/loop positions remain a list, not a chosen trajectory.
    """
    references, cfg = _references(refs), _config(config)
    output = []
    for original in apc:
        row = copy.deepcopy(original)
        arrival, departure = (
            wall_time(row.get("arrival")),
            wall_time(row.get("departure")),
        )
        instant = arrival if arrival is not None else departure
        row.update(
            source="apc",
            source_rid=row.get("source_rid", row["rid"]),
            t=instant,
            interval_start=arrival,
            interval_end=departure,
            interval_valid=arrival is not None
            and departure is not None
            and arrival <= departure,
            projection_basis="apc_coordinate_and_stop_code",
            trajectory_fabricated=False,
        )
        intervals, alternatives = [], []
        code = str(row.get("stop") or "")
        coordinates = valid_coordinate(row.get("lat"), row.get("lon"))
        for reference in references:
            positions = [
                s
                for s in reference["stop_positions"]
                if code and s.get("stop_code") == code
            ]
            unique = {(p["stop_id"], p["s_m"]) for p in positions}
            for p in positions:
                intervals.append(
                    dict(
                        reference_id=reference["reference_id"],
                        stop_id=p["stop_id"],
                        s_min_m=p["s_m"],
                        s_max_m=p["s_m"],
                        ambiguous=len(unique) > 1 or p["ambiguous"],
                    )
                )
            candidates = (
                project_candidates(
                    row.get("lat"),
                    row.get("lon"),
                    reference,
                    gate=cfg["max_projection_distance_m"],
                    top_k=cfg["projection_top_k"],
                )
                if coordinates
                else []
            )
            chosen = candidates[0] if candidates else None
            status = (
                "ON_REFERENCE"
                if chosen
                else "OFF_REFERENCE"
                if coordinates
                else "STOP_INTERVAL_ONLY"
                if positions
                else "UNPROJECTABLE"
            )
            # APC s is an observed-stop interval, not the GPS projection. The
            # coordinate projection is only a reference-quality gate.
            progress = (
                next(iter(unique))[1]
                if len(unique) == 1 and not any(p["ambiguous"] for p in positions)
                else None
            )
            if status == "OFF_REFERENCE":
                progress = None
            alternatives.append(
                dict(
                    reference_id=reference["reference_id"],
                    family_id=reference["family_id"],
                    s_m=progress,
                    lateral_m=chosen["lateral_m"] if chosen else None,
                    projection_status=status,
                    quality=0.6 if chosen else 0.3 if progress is not None else 0.0,
                    direction_fallback=False,
                    path_cost=None,
                    heading_error_deg=None,
                    coordinate_projection_s_m=chosen["s_m"] if chosen else None,
                    basis="coordinate"
                    if chosen
                    else "stop_code_interval"
                    if positions
                    else "no_evidence",
                )
            )
        accepted = [a for a in alternatives if a["projection_status"] == "ON_REFERENCE"]
        if accepted:
            best = min(accepted, key=lambda a: (a["lateral_m"], a["reference_id"]))
            row.update(best)
        else:
            usable = [
                a
                for a in alternatives
                if a["projection_status"] == "STOP_INTERVAL_ONLY"
            ]
            row.update(
                projection_status="OFF_REFERENCE"
                if coordinates
                else "STOP_INTERVAL_ONLY"
                if usable
                else "UNPROJECTABLE",
                reference_id=None,
                family_id=None,
                s_m=None,
                lateral_m=None,
                quality=0.3 if usable else 0.0,
                direction_fallback=False,
            )
        row.update(
            canonical_stop_intervals=intervals,
            projection_alternatives=alternatives,
            fragment_id=None,
            all_references_checked=True,
        )
        output.append(row)
    return output


def project_sequence(avl, refs, config=None, apc=None):
    """Project all AVL observations, retaining rejects and per-reference traces."""
    references, cfg = _references(refs), _config(config)
    points = []
    seen = set()
    for original in avl:
        if original["rid"] in seen:
            raise ValueError("Duplicate AVL source rid in projection input")
        seen.add(original["rid"])
        point = copy.deepcopy(original)
        value = original.get("t", original.get("time", original.get("event_time")))
        point.update(
            source="avl",
            source_rid=original.get("source_rid", original["rid"]),
            t=wall_time(value),
        )
        points.append(point)
    if len({p.get("vehicle") for p in points}) > 1:
        raise ValueError("A projection sequence requires one vehicle")
    points.sort(
        key=lambda p: (p["t"] is None, p["t"] or 0, p.get("source_row") or 0, p["rid"])
    )
    indices = {r["reference_id"]: make_index(r) for r in references}
    boundaries = (
        [0]
        + [i for i, p in enumerate(points) if i and p.get("scope_separator_before")]
        + [len(points)]
    )
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        if right > left:
            _project_chunk(points[left:right], references, cfg, indices)
    apc_evidence = project_apc_intervals(apc or [], references, cfg)
    runs = classify_off_reference(points, cfg, apc=apc_evidence, refs=references)
    fragments = build_fragments(points, runs, cfg)
    return dict(
        points=points,
        apc_evidence=apc_evidence,
        fragments=fragments,
        off_reference_runs=runs,
        diagnostics=dict(
            avl_source_count=len(avl),
            apc_source_count=len(apc or []),
            source_ids_preserved=True,
            status_counts=dict(
                collections.Counter(p["projection_status"] for p in points)
            ),
            direction_fallback_points=sum(p["direction_fallback"] for p in points),
            references_checked=len(references),
            isolated_points_never_deleted=True,
            algorithm="per-reference beam Viterbi with actual-dt skip states",
            global_sequence_optimum_guaranteed=False,
            search_scope="all references; top-k geometry and finite beam hypotheses",
            coordinates="s_m is reference-local metres; t is naive local epoch seconds",
        ),
    )
