"""Spike-02 diagnostics: metrics, checksums, baseline reproduction,
experiment matrix, and the edge-case packet.

All metrics are computed from in-memory results or the frozen ``r1c``
artifacts — never re-derived from numbers typed into a report.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl

from .adaptive_attachment import multiplicity_distribution
from .anchor_alignment import AlignmentPathResult
from .diagnostics import disposition_rates
from .gtfs_anchors import SegmentAnchorResult
from .resolution import ResolutionResult
from .spike02_contracts import (
    EpisodeStatus,
    RefinementResult,
    SignalQuality,
    TerminalEpisode,
    VehicleActivityRun,
)

EXPERIMENT_MATRIX = [
    {
        "hypothesis": "H1 boundary refinement reduces coarse segment duration",
        "variant": "V1/V2 vs V0 provisional runs",
        "required_input": "normal-day slice, terminal clusters, episodes",
        "expected_observation": "refined durations approach trip scale; "
        "turnarounds separated",
        "falsifying_observation": "refined segments remain multi-hour or "
        "splits are direction chatter",
        "metric": "variant_metrics.segmentation.duration_s_{median,p90,max}",
    },
    {
        "hypothesis": "H2 APC-assisted refinement adds information over AVL-only",
        "variant": "V2 vs V1",
        "required_input": "APC signals + alignment edges",
        "expected_observation": "boundary consistency/multiplicity/margins "
        "improve beyond V1",
        "falsifying_observation": "V1 == V2 or APC causes false fragmentation",
        "metric": "variant_metrics deltas v1 vs v2",
    },
    {
        "hypothesis": "H3 conditional APC precedence safer than unconditional",
        "variant": "V2 vs VX",
        "required_input": "same",
        "expected_observation": "V2 improves edge cases without VX fragmentation",
        "falsifying_observation": "Q3 no more reliable than weaker signals, or "
        "V2 == VX",
        "metric": "variant_metrics v2 vs vx (+edge-case packet)",
    },
    {
        "hypothesis": "H4 refined boundaries reduce APC candidate multiplicity",
        "variant": "V0 vs V2/V3 attachment",
        "required_input": "attachment battery",
        "expected_observation": "3-/4-way candidates decline; owner retention stable",
        "falsifying_observation": "multiplicity falls only because plausible "
        "owners were removed",
        "metric": "threshold_sensitivity.multiplicity + pruned_best_records",
    },
    {
        "hypothesis": "H5 refined boundaries reduce sequence demotions",
        "variant": "V0 vs V2 forced_diagnostic",
        "required_input": "Stage-3 results",
        "expected_observation": "demotion rate falls on comparable records",
        "falsifying_observation": "demotions become boundary ambiguity or "
        "no-candidate instead",
        "metric": "variant_metrics.sequence_demoted + error-transfer table",
    },
    {
        "hypothesis": "H6 per-stop/terminal anchors improve adjacent-trip "
        "discrimination",
        "variant": "V4 anchors vs Spike-01 margins",
        "required_input": "anchor candidates with per-stop features",
        "expected_observation": "top-2 margins grow materially vs the "
        "743-25,392 q band",
        "falsifying_observation": "margins stay in the Spike-01 band or only "
        "direction assumptions separate",
        "metric": "variant_metrics.anchor_margin_distribution",
    },
    {
        "hypothesis": "H7 smaller mid-route bounds have a safe region",
        "variant": "T_mid 20/30/45/60",
        "required_input": "V3 attachment grid",
        "expected_observation": "a range reduces multiplicity while retaining "
        "high-confidence candidates",
        "falsifying_observation": "all smaller bounds lose plausible owners or "
        "affect mainly terminal records",
        "metric": "threshold_sensitivity per T_mid",
    },
    {
        "hypothesis": "H8 anchor-and-align more stable than independent selection",
        "variant": "V4 alignment vs per-segment decisions",
        "required_input": "anchors + ordered segments",
        "expected_observation": "ordered path consistency; no forced matches",
        "falsifying_observation": "alignment propagates bad anchors or most "
        "intervals tie",
        "metric": "variant_metrics.alignment_paths",
    },
]


def _duration_stats(durations: list[int]) -> dict:
    if not durations:
        return {"count": 0}
    ordered = sorted(durations)
    n = len(ordered)
    mid = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {
        "count": n,
        "min": ordered[0],
        "median": mid,
        "p90": ordered[min(n - 1, (n * 9) // 10)],
        "max": ordered[-1],
    }


def refinement_metrics(
    runs: list[VehicleActivityRun],
    refinements: list[RefinementResult],
    episodes: list[TerminalEpisode],
) -> dict:
    segs = [s for r in refinements for s in r.refined_segments]
    turnarounds = [t for r in refinements for t in r.turnarounds]
    n_pings_states = sum(len(r.ping_states) for r in refinements)
    n_run_pings = sum(len(r.record_ids) for r in runs)
    seg_ping_ids = [rid for s in segs for rid in s.record_ids]
    return {
        "provisional_runs": len(runs),
        "provisional_duration_s": _duration_stats([r.duration_s for r in runs]),
        "refined_segments": len(segs),
        "refined_duration_s": _duration_stats([s.duration_s for s in segs]),
        "refined_pings": _duration_stats([len(s.record_ids) for s in segs]),
        "turnaround_intervals": len(turnarounds),
        "turnaround_duration_s": _duration_stats(
            [t.end_instant_s - t.start_instant_s for t in turnarounds]
        ),
        "boundaries_applied": sum(len(r.applied_boundary_ids) for r in refinements),
        "boundaries_ambiguous": sum(len(r.ambiguous_boundary_ids) for r in refinements),
        "unresolved_boundary_pings": sum(
            len(r.unresolved_record_ids) for r in refinements
        ),
        "runs_split": sum(1 for r in refinements if r.was_split),
        "runs_unsplit": sum(1 for r in refinements if not r.was_split),
        "fragmentation_segments_per_run": (round(len(segs) / max(1, len(runs)), 2)),
        "conservation_ok": (
            n_pings_states == n_run_pings
            and len(seg_ping_ids) == len(set(seg_ping_ids))
        ),
        "segment_type_counts": _counts(s.segment_type.value for s in segs),
        "short_turn_episodes": sum(
            1 for e in episodes if e.kind == "short_turn_candidate"
        ),
        "route_purity_millis_min": min(
            (_purity(s.route_label_counts) for s in segs if s.route_label_counts),
            default=None,
        ),
    }


def _purity(counts: dict[str, int]) -> int:
    total = sum(counts.values())
    return max(counts.values()) * 1000 // total


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def episode_metrics(episodes: list[TerminalEpisode]) -> dict:
    terminal = [e for e in episodes if e.kind == "terminal"]
    n_all_four = sum(
        1
        for e in terminal
        if None
        not in (
            e.physical_arrival_s,
            e.service_end_s,
            e.service_start_s,
            e.physical_departure_s,
        )
    )
    return {
        "terminal_episodes": len(terminal),
        "short_turn_candidates": len(episodes) - len(terminal),
        "status_counts": _counts(e.status.value for e in episodes),
        "all_four_timestamps": n_all_four,
        "partial_timestamps": sum(
            1
            for e in terminal
            if 0
            < sum(
                t is not None
                for t in (
                    e.physical_arrival_s,
                    e.service_end_s,
                    e.service_start_s,
                    e.physical_departure_s,
                )
            )
            < 4
        ),
        "ordering_violations": sum(1 for e in episodes if not e.ordering_valid),
        "apc_quality_counts": _counts(
            e.apc_quality.value for e in episodes if e.apc_quality is not None
        ),
        "episodes_with_q3": sum(
            1 for e in episodes if e.apc_quality == SignalQuality.Q3_STRONG
        ),
        "spanning_records": len(
            {rid for e in episodes for rid in e.spanning_apc_record_ids}
        ),
        "evidence_family_counts": _counts(
            f for e in episodes for f in e.evidence_families
        ),
        "selected": sum(1 for e in episodes if e.status == EpisodeStatus.SELECTED),
    }


def refinement_fingerprint(refinements: list[RefinementResult]) -> str:
    payload = {
        r.run_id: {
            "variant": r.variant,
            "segments": [
                (s.refined_segment_id, s.record_ids) for s in r.refined_segments
            ],
            "turnarounds": [(t.turnaround_id, t.record_ids) for t in r.turnarounds],
            "unresolved": r.unresolved_record_ids,
            "decisions": [
                (d.boundary_id, d.status.value) for d in r.boundary_decisions
            ],
        }
        for r in sorted(refinements, key=lambda r: r.run_id)
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def anchor_metrics(results: dict[str, SegmentAnchorResult]) -> dict:
    margins = [
        r.margin_q
        for r in results.values()
        if r.margin_q is not None and r.status in ("accepted", "low_margin")
    ]
    return {
        "segments": len(results),
        "status_counts": _counts(r.status for r in results.values()),
        "anchors": sum(1 for r in results.values() if r.is_anchor),
        "margin_q": _duration_stats(sorted(margins)),
        "margin_q_values": sorted(margins),
    }


def alignment_metrics(results: dict[str, AlignmentPathResult]) -> dict:
    per_vehicle = {}
    for vehicle, res in sorted(results.items()):
        per_vehicle[vehicle] = {
            "optimal_cost": res.optimal_cost,
            "path_margin": res.path_margin,
            "n_optimal": res.n_optimal,
            "truncated": res.truncated,
            "segment_status_counts": _counts(s.status for s in res.segments),
            "skipped_scheduled_trips": len(res.skipped_trip_ids),
        }
    return per_vehicle


def attachment_summary(
    resolution: ResolutionResult, multiplicity: dict[str, int]
) -> dict:
    return {
        "dispositions": disposition_rates(resolution),
        "multiplicity": multiplicity,
        "candidate_counts": resolution.candidate_counts,
        "sequence_demoted": sum(
            1
            for status in resolution.dispositions.values()
            if status == "sequence_demoted"
        ),
        "sequence_validated_segments": len(resolution.sequence_results),
    }


def reproduce_r1c_baseline(r1c_dir: Path) -> dict:
    """Recompute Spike-01 normal-day baseline numbers from the frozen
    artifacts and compare them against the run's own metrics.json.
    Comparison of any metric that fails to reproduce must STOP (the
    caller marks it blocked) rather than proceed on bad numbers."""
    out: dict[str, dict] = {}
    metrics = json.loads((r1c_dir / "metrics.json").read_text())
    dispositions = pl.read_parquet(r1c_dir / "x_record_dispositions.parquet")
    recomputed_disp = _counts(dispositions["disposition"].to_list())
    out["dispositions"] = {
        "recomputed": recomputed_disp,
        "reported": metrics["disposition_rates"],
        "match": recomputed_disp == metrics["disposition_rates"],
    }
    cands = pl.read_parquet(r1c_dir / "x_apc_candidates.parquet")
    non_null = cands.filter(pl.col("candidate_segment_id") != "XNULL:NO_ATTACHMENT")
    per_record_segments = non_null.group_by("apc_source_record_id").agg(
        pl.col("candidate_segment_id").n_unique().alias("n_segments")
    )
    all_records = dispositions.select(pl.col("apc_source_record_id")).join(
        per_record_segments, on="apc_source_record_id", how="left"
    )
    seg_mult = _counts(
        str(v if v is not None else 0) for v in all_records["n_segments"].to_list()
    )
    out["candidate_rows"] = {
        "recomputed": cands.height,
        "reported": metrics["candidates"]["apc_candidates"],
        "match": cands.height == metrics["candidates"]["apc_candidates"],
    }
    out["segment_multiplicity_from_artifacts"] = {"recomputed": seg_mult}
    segments = pl.read_parquet(r1c_dir / "x_segments.parquet")
    x4b = segments.filter(pl.col("variant") == "x4b")
    durations = sorted(x4b["duration_s"].to_list())
    out["x4b_segments"] = {
        "recomputed": {
            "count": len(durations),
            "median": durations[len(durations) // 2] if durations else None,
            "max": durations[-1] if durations else None,
        },
    }
    baseline = json.loads((r1c_dir / "baseline_report.json").read_text())
    out["b1_status_counts"] = {"reported": baseline["b1_status_counts"]}
    out["all_matched"] = all(
        block.get("match", True) for block in out.values() if isinstance(block, dict)
    )
    return out


def multiplicity_before_after(base_candidates: dict, filtered_candidates: dict) -> dict:
    return {
        "before": multiplicity_distribution(base_candidates),
        "after": multiplicity_distribution(filtered_candidates),
    }
