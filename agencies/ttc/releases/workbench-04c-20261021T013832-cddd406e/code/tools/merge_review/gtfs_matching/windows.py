"""Disjoint atomic ownership and explicitly normalized multiscale evidence."""

from __future__ import annotations

import collections
import math

DEFAULTS = {
    "window_atomic_s": 60,
    "window_context_s": [300, 600],
    "window_event_windows": [],
    "window_adaptive_enabled": False,
    "window_event_radius_s": 90,
    "window_min_points": 3,
    "window_min_duration_s": 20,
    "window_min_progress_m": 50,
    "window_min_quality": 0.2,
    "window_min_point_ratio": 0.25,
    "window_min_duration_ratio": 0.1,
    "window_max_gap_s": 90,
}


def get_config(config=None):
    return DEFAULTS | (config or {})


def observation_key(row):
    return f"{row.get('source', 'avl')}|{row.get('source_rid', row.get('rid'))}"


def usable(row, config=None):
    c = get_config(config)
    return (
        row.get("source", "avl") == "avl"
        and row.get("projection_status") == "ON_REFERENCE"
        and all(
            isinstance(row.get(k), (int, float)) and math.isfinite(row[k])
            for k in ("t", "s_m")
        )
        and float(row.get("quality", 1.0)) >= c["window_min_quality"]
    )


def quality(rows, config=None):
    c = get_config(config)
    selected = [r for r in rows if usable(r, c)]
    groups = collections.defaultdict(list)
    for row in selected:
        groups[row.get("reference_id"), row.get("fragment_id")].append(row)
    duration = progress = 0.0
    for group in groups.values():
        group.sort(key=lambda r: r["t"])
        for first, second in zip(group, group[1:], strict=False):
            gap = second["t"] - first["t"]
            if 0 < gap <= c["window_max_gap_s"]:
                duration += gap
                progress += max(0.0, second["s_m"] - first["s_m"])
    reasons = []
    for value, threshold, name in (
        (len(selected), c["window_min_points"], "INSUFFICIENT_POINTS"),
        (duration, c["window_min_duration_s"], "INSUFFICIENT_OBSERVED_DURATION"),
        (progress, c["window_min_progress_m"], "INSUFFICIENT_OBSERVED_PROGRESS"),
    ):
        if value < threshold:
            reasons.append(name)
    return {
        "status": "NO_EVIDENCE" if reasons else "EVIDENCE",
        "reasons": reasons,
        "point_count": len(selected),
        "observed_duration_s": duration,
        "observed_progress_m": progress,
    }


def build_windows(observations, config=None):
    c = get_config(config)
    if c["window_atomic_s"] != 60:
        raise ValueError("Atomic windows must be nonoverlapping one-minute windows")
    rows = list(observations)
    ids = [observation_key(r) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source identity in window input")
    timed = [
        r["t"]
        for r in rows
        if isinstance(r.get("t"), (int, float)) and math.isfinite(r["t"])
    ]
    if not timed:
        return {
            "atomic": [],
            "contexts": [],
            "event_windows": [],
            "windows": [],
            "observation_weights": {},
            "untimed_ids": ids,
        }
    beginning = math.floor(min(timed) / 60) * 60
    end = (math.floor(max(timed) / 60) + 1) * 60
    definitions = []
    for start in range(int(beginning), int(end), 60):
        definitions.append(("atomic", float(start), float(start + 60)))
        for duration in c["window_context_s"]:
            if duration <= 60:
                raise ValueError("Context windows must exceed an atomic minute")
            definitions.append(
                (f"context_{duration}", float(start), float(start + duration))
            )
    for event in c["window_event_windows"]:
        if not isinstance(event, dict) or not event["start"] < event["end"]:
            raise ValueError("Event windows require increasing explicit start/end")
        definitions.append(("event", float(event["start"]), float(event["end"])))
    by_id = dict(zip(ids, rows, strict=True))
    counts = collections.Counter()
    windows = []
    for ordinal, (kind, start, finish) in enumerate(definitions):
        members = [
            key
            for key, row in by_id.items()
            if row.get("t") is not None and start <= row["t"] < finish
        ]
        if not members and kind != "atomic":
            continue
        counts.update(members)
        selected = [by_id[key] for key in members]
        times = sorted(r["t"] for r in selected if r.get("source", "avl") == "avl")
        windows.append(
            {
                "id": f"{kind}:{ordinal}",
                "kind": kind,
                "start": start,
                "end": finish,
                "anchor_t": (start + finish) / 2,
                "observation_ids": members,
                "quality": quality(selected, c),
                "has_gap": any(
                    b - a > c["window_max_gap_s"]
                    for a, b in zip(times, times[1:], strict=False)
                )
                or any(r.get("scope_separator_before") for r in selected)
                or len(
                    {
                        r.get("fragment_id")
                        for r in selected
                        if r.get("source", "avl") == "avl"
                        and r.get("fragment_id") is not None
                    }
                )
                > 1,
            }
        )
    for window in windows:
        window["weights"] = {
            key: 1.0 / counts[key] for key in window["observation_ids"]
        }
        window["weight"] = sum(window["weights"].values())
        window["avl_weight"] = sum(
            value
            for key, value in window["weights"].items()
            if by_id[key].get("source", "avl") == "avl"
        )
    normalized = {key: sum(w["weights"].get(key, 0.0) for w in windows) for key in ids}
    return {
        "atomic": [w for w in windows if w["kind"] == "atomic"],
        "contexts": [w for w in windows if w["kind"].startswith("context")],
        "event_windows": [w for w in windows if w["kind"] == "event"],
        "windows": windows,
        "observation_weights": normalized,
        "untimed_ids": [key for key, row in by_id.items() if row.get("t") is None],
        "weight_definition": "Each timed original source identity sums to one "
        "across every containing scale; APC is not a GPS point.",
    }
