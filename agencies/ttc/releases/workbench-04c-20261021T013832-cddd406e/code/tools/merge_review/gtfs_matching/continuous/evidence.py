"""Density-aware evidence ownership and explicit three-stage coverage.

Point ratios describe the recorded sample; duration ratios describe adjacent
physical-time intervals. Failed observations remove only their incident edges,
never all evidence in an otherwise supported fragment.
"""

from __future__ import annotations

import math
from collections import defaultdict

DEFAULTS = {
    "continuous_coverage_max_gap_s": 90.0,
    "continuous_min_projection_quality": 0.2,
    "continuous_isolated_point_support_s": 1.0,
}


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def identity(row):
    return str(
        row.get(
            "source_id",
            f"{row.get('source', 'avl')}|{row.get('source_rid', row.get('rid'))}",
        )
    )


def _config(config=None):
    c = DEFAULTS | (config or {})
    for key in DEFAULTS:
        if not finite(c[key]) or c[key] < 0:
            raise ValueError(f"{key} must be finite and nonnegative")
    if c["continuous_coverage_max_gap_s"] <= 0:
        raise ValueError("Coverage maximum gap must be positive")
    if c["continuous_isolated_point_support_s"] <= 0:
        raise ValueError("Isolated-point temporal support must be positive")
    if c["continuous_min_projection_quality"] > 1:
        raise ValueError("Projection quality threshold must be between zero and one")
    return c


def _same_fragment(first, second):
    return (first.get("fragment_id"), first.get("reference_id")) == (
        second.get("fragment_id"),
        second.get("reference_id"),
    ) and not second.get("scope_separator_before")


def temporal_weights(rows, config=None):
    """Trapezoidal time support per source identity, without gap interpolation.

    Equal-time observations share support instead of duplicating it. The caller
    may multiply these seconds by quality; they are not independent votes.
    """
    c = _config(config)
    rows = list(rows)
    keys = [identity(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate source identity in continuous evidence")
    weights = dict.fromkeys(keys, 0.0)
    groups = defaultdict(list)
    for row in rows:
        if finite(row.get("t")):
            groups[row.get("fragment_id"), row.get("reference_id")].append(row)
    for group in groups.values():
        by_time = defaultdict(list)
        for row in group:
            by_time[float(row["t"])].append(row)
        nodes = sorted(by_time.items())
        node_weight = [0.0] * len(nodes)
        for index, ((first_t, first), (second_t, second)) in enumerate(
            zip(nodes, nodes[1:], strict=False)
        ):
            gap = second_t - first_t
            if gap <= c["continuous_coverage_max_gap_s"] and _same_fragment(
                first[-1], second[0]
            ):
                node_weight[index] += gap / 2
                node_weight[index + 1] += gap / 2
        for (_, tied), weight in zip(nodes, node_weight, strict=False):
            weight = weight or c["continuous_isolated_point_support_s"]
            for row in tied:
                weights[identity(row)] = weight / len(tied)
    return weights


def spatial(row):
    # APC without genuine coordinates remains stop-interval evidence elsewhere.
    if row.get("has_spatial_coordinates") is False:
        return False
    if "lat" in row or "lon" in row:
        return (
            finite(row.get("lat"))
            and finite(row.get("lon"))
            and -90 <= row["lat"] <= 90
            and -180 <= row["lon"] <= 180
        )
    if row.get("has_spatial_coordinates") is True:
        return True
    # A projected AVL fixture/serialized subset may omit its coordinate columns.
    return row.get("source", "avl").lower() == "avl" and "projection_status" in row


def valid_projection(row, config=None):
    c = _config(config)
    return (
        spatial(row)
        and row.get("projection_status") == "ON_REFERENCE"
        and finite(row.get("s_m"))
        and finite(row.get("t"))
        and finite(row.get("quality", 1.0))
        and row.get("quality", 1.0) >= c["continuous_min_projection_quality"]
    )


def coverage_metrics(all_rows, supported_ids, config=None):
    """Coverage denominators remain explicit and never shrink on score failure."""
    c = _config(config)
    rows = [row for row in all_rows if spatial(row)]
    keys = [identity(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate spatial evidence identity")
    support = {str(key) for key in supported_ids}
    projected = {identity(row) for row in rows if valid_projection(row, c)}
    supported = support & projected
    ordered = sorted(
        (row for row in rows if finite(row.get("t"))),
        key=lambda r: (r["t"], identity(r)),
    )
    all_edges = projected_edges = supported_edges = 0
    all_duration = projected_duration = supported_duration = gap_duration = 0.0
    longest = run = 0.0
    for first, second in zip(ordered, ordered[1:], strict=False):
        elapsed = second["t"] - first["t"]
        if elapsed <= 0:
            continue
        all_edges += 1
        all_duration += elapsed
        connected = elapsed <= c["continuous_coverage_max_gap_s"] and _same_fragment(
            first, second
        )
        if not connected:
            gap_duration += elapsed
        if connected and identity(first) in projected and identity(second) in projected:
            projected_edges += 1
            projected_duration += elapsed
            run += elapsed
            longest = max(longest, run)
            if identity(first) in supported and identity(second) in supported:
                supported_edges += 1
                supported_duration += elapsed
        else:
            run = 0.0

    def metric(
        points,
        point_denominator,
        edges,
        edge_denominator,
        duration,
        duration_denominator,
    ):
        return {
            "point_count": points,
            "point_denominator": point_denominator,
            "point_ratio": points / point_denominator if point_denominator else 0.0,
            "interval_count": edges,
            "interval_denominator": edge_denominator,
            "interval_ratio": edges / edge_denominator if edge_denominator else 0.0,
            "duration_s": duration,
            "duration_denominator_s": duration_denominator,
            "duration_ratio": duration / duration_denominator
            if duration_denominator
            else 0.0,
        }

    projection = metric(
        len(projected),
        len(rows),
        projected_edges,
        all_edges,
        projected_duration,
        all_duration,
    )
    schedule = metric(
        len(supported),
        len(projected),
        supported_edges,
        projected_edges,
        supported_duration,
        projected_duration,
    )
    end_to_end = metric(
        len(supported),
        len(rows),
        supported_edges,
        all_edges,
        supported_duration,
        all_duration,
    )
    return {
        "projection": projection,
        "schedule": schedule,
        "schedule_alignment": schedule,
        "end_to_end": end_to_end,
        "all_spatial_point_count": len(rows),
        "valid_projected_point_count": len(projected),
        "schedule_supported_point_count": len(supported),
        "projection_point_ratio": projection["point_ratio"],
        "projection_duration_ratio": projection["duration_ratio"],
        "schedule_point_ratio": schedule["point_ratio"],
        "schedule_duration_ratio": schedule["duration_ratio"],
        "end_to_end_point_ratio": end_to_end["point_ratio"],
        "end_to_end_duration_ratio": end_to_end["duration_ratio"],
        "unobserved_or_separated_duration_s": gap_duration,
        "longest_valid_contiguous_run_s": longest,
        "interval_definition": (
            "Both adjacent original spatial observations must be supported and in "
            "the same fragment; no edge crosses a separator or maximum gap."
        ),
    }
