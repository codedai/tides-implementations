"""Compact HTTP presentation only; persisted matching evidence is untouched."""

POINT_FIELDS = {
    "source",
    "rid",
    "source_rid",
    "t",
    "lat",
    "lon",
    "s",
    "s_m",
    "lateral_m",
    "projection_distance_m",
    "heading",
    "heading_error_deg",
    "quality",
    "status",
    "projection_status",
    "reference_id",
    "family_id",
    "fragment_id",
    "rejection_reason",
    "sequence_restart",
    "scope_separator_before",
    "projection_alternatives",
    "all_references_checked",
    "opposite_direction_checked",
    "direction_fallback",
    "direction_label",
    "path_cost",
    "unconstrained_projection",
    "gate_rescue_used",
}
APC_FIELDS = POINT_FIELDS | {
    "arrival",
    "departure",
    "interval_start",
    "interval_end",
    "interval_valid",
    "stop",
    "stop_id",
    "s_min_m",
    "s_max_m",
    "ambiguous",
    "boarding",
    "alighting",
    "confidence",
    "projection_basis",
    "trajectory_fabricated",
}


def select(row, fields):
    return {k: v for k, v in row.items() if k in fields}


def compact_gtfs_detail(detail):
    """Consume a fresh per-request detail, preserving raw rows and all windows.

    Wide local-score matrices and repeated window memberships remain in the
    hash-verified case artifact; they are not necessary to render comparisons.
    """
    gtfs = detail.get("gtfs")
    if not gtfs:
        return detail
    from .runner import artifact_key

    result = gtfs["result"]
    if "all_candidates" in result:
        from .diagnostic_candidates import build_diagnostic_candidates

        diagnostic = build_diagnostic_candidates(result, gtfs.get("config"))
        gtfs["diagnostic_candidates"] = diagnostic["candidates"]
        gtfs["diagnostic_candidate_summary"] = diagnostic["summary"]
    retrieval = result.pop("retrieval", {})
    result["retrieval_summary"] = {
        "candidate_union_count": len(retrieval.get("trips", [])),
        "window_count": len(retrieval.get("windows", [])),
    }
    result["retained_candidate_count"] = len(result.pop("all_candidates", []))
    result.pop("local_scores", None)
    result.pop("windows", None)
    bundle = gtfs.get("windows", {})
    windows = bundle if isinstance(bundle, list) else bundle.get("windows", [])
    gtfs["windows"] = {
        "windows": [
            {k: v for k, v in w.items() if k not in {"observation_ids", "weights"}}
            for w in windows
        ],
        "membership_evidence_in_artifact": True,
    }
    projection = gtfs.get("projection", {})
    projection["points"] = [
        select(p, POINT_FIELDS) for p in projection.get("points", [])
    ]
    projection["by_reference"] = {
        key: [select(p, POINT_FIELDS - {"projection_alternatives"}) for p in points]
        for key, points in projection.get("by_reference", {}).items()
    }
    projection.pop("apc_evidence", None)
    gtfs["apc_evidence"] = [select(p, APC_FIELDS) for p in gtfs.get("apc_evidence", [])]
    for reference in gtfs.get("references", []):
        reference.pop("points", None)  # geometry is the identical browser alias.
    for row in detail.get("avl", []):
        if "gtfs_projection" in row:
            row["gtfs_projection"] = select(row["gtfs_projection"], POINT_FIELDS)
    gtfs["lightweight_http_presentation"] = True
    gtfs.setdefault(
        "intermediate_artifact",
        (
            detail["provenance"]["run_path"]
            + "/cases/"
            + artifact_key(detail["case"]["id"])
            + ".json"
        ),
    )
    return detail
