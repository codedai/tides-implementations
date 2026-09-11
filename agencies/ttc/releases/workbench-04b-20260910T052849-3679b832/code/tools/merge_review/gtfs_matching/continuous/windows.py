"""Atomic ownership plus context/event diagnostics, never independent votes."""

from ..windows import build_windows, usable


def construct_windows(points, apc, config):
    events = []
    radius = config["continuous_event_radius_s"]
    if config["continuous_event_windows_enabled"]:
        for row in apc:
            t = row.get("interval_start", row.get("arrival", row.get("t")))
            if isinstance(t, (int, float)):
                events.append((t, "APC_STOP"))
        previous = None
        for row in sorted(
            (p for p in points if p.get("t") is not None), key=lambda p: p["t"]
        ):
            if previous:
                reason = (
                    "GPS_GAP"
                    if row["t"] - previous["t"] > config.get("window_max_gap_s", 90)
                    else "OFF_REFERENCE_BOUNDARY"
                    if usable(row, config) != usable(previous, config)
                    else "REFERENCE_OR_BRANCH_CHANGE"
                    if row.get("reference_id") != previous.get("reference_id")
                    else "DIRECTION_CHANGE"
                    if row.get("direction_label") != previous.get("direction_label")
                    else None
                )
                if reason:
                    events.append((row["t"], reason))
            if row.get("scope_separator_before"):
                events.append((row["t"], "UPSTREAM_BOUNDARY"))
            if (
                row.get("s_m") is not None
                and row["s_m"] <= config["continuous_terminal_radius_m"]
            ):
                events.append((row["t"], "TERMINAL_PROXIMITY"))
            previous = row
    # Collapse nearby equivalent event anchors into atomic ownership bins.
    event_groups = {}
    for t, reason in events:
        event_groups.setdefault(int(t // 60) * 60, set()).add(reason)
    c = dict(
        config,
        window_event_windows=[
            {"start": t - radius, "end": t + radius} for t in sorted(event_groups)
        ],
    )
    bundle = build_windows([*points, *apc], c)
    for row in bundle["event_windows"]:
        row["event_reasons"] = sorted(event_groups.get(int(row["start"] + radius), []))
    bundle["scoring_role"] = (
        "retrieval_and_local_diagnostics; whole score uses each observation's "
        "temporal support once"
    )
    return bundle
