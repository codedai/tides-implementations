"""Whitelisted independent stop evidence; upstream schedule labels cannot leak."""

import math

from ..retrieval import for_reference

# No AvgOTP, delayed flags, vendor trip assignment, run assignment or schedule ID.
FEATURE_FIELDS = {
    "source",
    "rid",
    "source_rid",
    "t",
    "arrival",
    "departure",
    "interval_start",
    "interval_end",
    "stop",
    "stop_id",
    "s_m",
    "reference_id",
    "fragment_id",
    "boarding",
    "alighting",
    "confidence",
    "quality",
    "projection_status",
    "lat",
    "lon",
    "direction_label",
    "canonical_stop_intervals",
    "projection_alternatives",
    "interval_valid",
}


def normalize_apc(rows, reference_id):
    clean = [{k: v for k, v in row.items() if k in FEATURE_FIELDS} for row in rows]
    result = []
    for row in for_reference(clean, reference_id, include_unprojectable=True):
        left = row.get("interval_start", row.get("arrival", row.get("t")))
        right = row.get("interval_end", row.get("departure", left))
        reasons = []

        def valid(v):
            return isinstance(v, (int, float)) and math.isfinite(v)

        if not valid(left) or not valid(right) or right < left:
            reasons.append("INVALID_APC_TIME_INTERVAL")
        s = row.get("s_m")
        positions = [
            p
            for p in row.get("canonical_stop_intervals", [])
            if p.get("reference_id") == reference_id
            and not p.get("ambiguous")
            and valid(p.get("s_min_m"))
            and valid(p.get("s_max_m"))
        ]
        stop_ids = {
            str(p["stop_id"]) for p in positions if p.get("stop_id") is not None
        }
        stop_id = row.get("stop_id") or (
            next(iter(stop_ids)) if len(stop_ids) == 1 else None
        )
        if not valid(s):
            if len(positions) == 1:
                s = (positions[0]["s_min_m"] + positions[0]["s_max_m"]) / 2
            else:
                reasons.append("NO_UNAMBIGUOUS_CANONICAL_STOP")
        if row.get("projection_status") == "OFF_REFERENCE":
            reasons.append("APC_COORDINATE_OFF_REFERENCE")
        result.append(
            {
                "source": "apc",
                "source_id": "apc|" + str(row.get("source_rid", row.get("rid"))),
                "source_rid": row.get("source_rid", row.get("rid")),
                "canonical_stop_s": s,
                "interval_start": left,
                "interval_end": right,
                "stop_id": stop_id,
                "stop_code": row.get("stop"),
                "boarding": row.get("boarding"),
                "alighting": row.get("alighting"),
                "confidence": min(
                    1.0,
                    max(
                        0.0,
                        float(row.get("confidence", row.get("quality", 0.6)) or 0.0),
                    ),
                ),
                "status": "NO_EVIDENCE" if reasons else "EVIDENCE",
                "reasons": reasons,
                "trajectory_fabricated": False,
                "schedule_derived_features_used": False,
            }
        )
    return result
