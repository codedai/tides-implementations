"""Frozen-window robustness probes for the observation-level direct evidence layer.

No algorithm is treated as truth. Owner partitions are frozen before perturbation
to isolate merge behavior from the known duplicate sensitivity of v1 segmentation.
Every transformed source row is an in-memory copy; prior selections are reused,
and neither source files nor prior experiment outputs may be overwritten.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import copy
import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from experiments.apc_avl_coverage_study.merge import (
    DEFAULTS as V1_DEFAULTS,
)
from experiments.apc_avl_coverage_study.merge import (
    build_support as v1_support,
)
from experiments.apc_avl_coverage_study.merge import (
    direct_match as v1_match,
)
from experiments.apc_avl_coverage_study.probes import perturbations
from experiments.apc_avl_coverage_study.segmentation import segment_vehicle

DIRECT_STATUSES = {"direct"}
LOCALIZED_STATUSES = {"direct", "partial"}
DATE_FIELDS = {
    "window_start",
    "window_end",
    "avl_first",
    "avl_last",
    "apc_first",
    "apc_last",
    "apc_prefix_cutoff",
    "avl_suffix_cutoff",
}


def read_frozen_selections(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    selected, sources = [], []
    seen = set()
    for path in paths:
        contents = path.read_bytes()
        rows = json.loads(contents)
        sources.append(
            {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "windows": len(rows),
            }
        )
        for raw in rows:
            row = dict(raw)
            for field in DATE_FIELDS:
                if isinstance(row.get(field), str):
                    row[field] = datetime.fromisoformat(row[field])
            key = (row["vehicle"], row["window_start"], row["window_end"])
            if key in seen:
                continue
            seen.add(key)
            row.setdefault("selection_stratum", "general")
            row.setdefault(
                "window_case", row["vehicle_case"] + "__" + row["window_hash"]
            )
            selected.append(row)
    return selected, sources


def project_frozen_segmentation(
    original: dict,
    avl: list[dict],
    origins: dict[str, str] | None = None,
) -> dict:
    """Keep pre-perturbation owners but recompute their observed support envelope.

    A duplicate inherits its original row's owner. A removed row disappears from
    the support universe. This does not rerun a trip/segment boundary algorithm.
    """
    origins = origins or {}
    result = copy.deepcopy(original)
    mapping = original["ping_segment"]
    by_owner = collections.defaultdict(list)
    for row in avl:
        owner = mapping.get(origins.get(row["rid"], row["rid"]))
        if owner is not None:
            by_owner[owner].append(row)
    result["segments"] = []
    result["ping_segment"] = {}
    for segment in original["segments"]:
        members = sorted(
            by_owner[segment["segment_id"]], key=lambda row: (row["time"], row["rid"])
        )
        if not members:
            continue
        projected = dict(segment)
        projected.update(
            ping_ids=[r["rid"] for r in members],
            start=members[0]["time"],
            end=members[-1]["time"],
        )
        result["segments"].append(projected)
        result["ping_segment"].update(
            dict.fromkeys(projected["ping_ids"], segment["segment_id"])
        )
    surviving = {r["rid"] for r in avl}
    for field in ("excluded_ping_ids", "unlocated_invalid_ping_ids"):
        result[field] = [rid for rid in original.get(field, []) if rid in surviving]
    result["probe_owner_partition_frozen"] = True
    return result


def synthetic_cases() -> dict[str, dict]:
    """Constructed stresses describe intended distinctions, not field accuracy."""
    base = datetime(2024, 1, 1, 13)

    def ping(rid, second, offset=0):
        return {
            "rid": rid,
            "vehicle": "synthetic",
            "time": base + timedelta(seconds=second),
            "lat": 43.7,
            "lon": -79.4 + offset,
            "route": "10",
            "run": "R",
            "destination": "E",
            "invalid": False,
            "time_ambiguous": False,
            "time_nonexistent": False,
        }

    def event(rid, arrival, departure=None, **extra):
        return {
            "rid": rid,
            "vehicle": "synthetic",
            "arrival": base + timedelta(seconds=arrival),
            "departure": base
            + timedelta(seconds=arrival if departure is None else departure),
            "lat": 43.7,
            "lon": -79.4,
            "route": "10",
            "direction": "E",
            "branch": "A",
            "stop": "shared-stop",
            "invalid": False,
            "time_ambiguous": False,
            "time_nonexistent": False,
            "grain_uncertain": False,
            **extra,
        }

    cases = {
        "long_dwell_single_accidental_near_ping": {
            "avl": [
                ping(f"p{i}", second, 0 if second == 300 else 0.01)
                for i, second in enumerate(range(0, 601, 30))
            ],
            "apc": [event("long", 0, 600)],
            "question": "Does one near point hide a contradictory long interval?",
        },
        "long_dwell_continuously_near": {
            "avl": [
                ping(f"p{i}", second) for i, second in enumerate(range(0, 601, 30))
            ],
            "apc": [event("long", 0, 600)],
            "question": "Is duration alone confused with spatial contradiction?",
        },
        "same_chunk_repeated_near_stop_episodes": {
            "avl": [
                ping(f"p{i}", second, 0 if second <= 60 or second >= 540 else 0.01)
                for i, second in enumerate(range(0, 601, 30))
            ],
            "apc": [event("spanning-visits", 0, 600)],
            "question": "Are competing visits retained inside one chunk owner?",
        },
        "same_stop_opposite_direction_overlap": {
            "avl": [
                ping(f"p{i}", second) for i, second in enumerate(range(0, 241, 20))
            ],
            "apc": [
                event("inbound", 30, 150, direction="E"),
                event("outbound", 90, 210, direction="W"),
            ],
            "question": "Are local co-location and unresolved visit roles separate?",
        },
        "short_interpolation_without_point_inside_radius": {
            "avl": [ping("left", 0, -0.002), ping("right", 60, 0.002)],
            "apc": [event("between", 30)],
            "question": "Is interpolation distinguished from a measured near ping?",
        },
        "internal_missing_gps_not_a_fabricated_observation": {
            "avl": [ping("before", 0), ping("after", 600)],
            "apc": [event("unsupported", 300)],
            "question": "Does a ten-minute gap abstain from direct evidence?",
        },
    }
    for row in cases.values():
        row["segmentation"] = segment_vehicle(row["avl"], row["apc"], "gap_only")
    return cases


def normalized_decisions(
    links: list[dict], origins: dict[str, str] | None = None
) -> list[dict]:
    """Decision invariance excludes witness IDs/counts that encode multiplicity."""
    origins = origins or {}
    fields = (
        "status",
        "reason",
        "segment_id",
        "support_kind",
        "support_level",
        "visit_ambiguous",
        "distance_m",
        "gap_s",
    )
    return [
        {"apc_rid": row["apc_rid"]}
        | {field: row.get(field) for field in fields}
        | {
            "ping_ids": sorted(
                {origins.get(rid, rid) for rid in row.get("ping_ids", [])}
            )
        }
        for row in sorted(links, key=lambda r: r["apc_rid"])
    ]


def compare_links(reference: list[dict], changed: list[dict]) -> dict:
    previous = {row["apc_rid"]: row for row in reference}
    counts, transitions, tiers = (collections.Counter() for _ in range(3))
    for row in changed:
        old = previous.get(row["apc_rid"])
        if old is None:
            counts["new_apc"] += 1
            continue
        counts["retained_apc"] += 1
        old_direct = old["status"] in DIRECT_STATUSES
        new_direct = row["status"] in DIRECT_STATUSES
        counts["reference_direct"] += old_direct
        counts["changed_direct"] += new_direct
        counts["direct_to_not_direct"] += old_direct and not new_direct
        counts["direct_to_abstention"] += (
            old_direct and row["status"] not in LOCALIZED_STATUSES
        )
        counts["abstention_to_direct"] += (
            old["status"] not in LOCALIZED_STATUSES and new_direct
        )
        counts["both_direct_owner_switch"] += (
            old_direct and new_direct and old.get("segment_id") != row.get("segment_id")
        )
        transitions[f"{old['status']}->{row['status']}"] += 1
        old_local = old["status"] in LOCALIZED_STATUSES
        new_local = row["status"] in LOCALIZED_STATUSES
        counts["reference_localized"] += old_local
        counts["changed_localized"] += new_local
        counts["direct_to_partial"] += old_direct and row["status"] == "partial"
        counts["localized_to_abstention"] += old_local and not new_local
        tiers[f"{old.get('support_level')}->{row.get('support_level')}"] += 1
    return dict(counts) | {
        "status_transitions": dict(transitions),
        "support_level_transitions": dict(tiers),
        "independent_accuracy": None,
    }


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n")
    path.chmod(0o600)


def _save_links(path: Path, links: list[dict]) -> None:
    # JSON retains nested witnesses without inference from mixed/null parquet schemas.
    _dump(path, links)


def _seconds(value) -> float:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return (value - datetime(1970, 1, 1)).total_seconds()
    return float(value)


def _haversine(lat0, lon0, lat1, lon1) -> float:
    """Independent spherical replay; not imported from either matching method."""
    rad = math.pi / 180
    delta_lat = (lat1 - lat0) * rad
    delta_lon = (lon1 - lon0) * rad
    hav = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat0 * rad) * math.cos(lat1 * rad) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * 6371008.8 * math.asin(min(1, math.sqrt(hav)))


def replay_witnesses(
    avl: list[dict], apc: list[dict], links: list[dict], config: dict | None = None
) -> dict:
    """Recompute accepted v2 geometry/time from source refs without matcher helpers.

    This is a scoped integrity check, not a validator of real stop-visit identity,
    trip boundaries, sensor calibration, vendor grain, or accuracy.
    """
    cfg = {"time_tolerance_s": 60, "radius_m": 100, "interpolation_gap_s": 90}
    cfg.update(config or {})
    source = {row["rid"]: row for row in avl}
    events = {row["rid"]: row for row in apc}
    instants = collections.defaultdict(list)
    for row in avl:
        if row.get("time") is not None:
            instants[(row.get("vehicle"), _seconds(row["time"]))].append(row)
    instant_axes = collections.defaultdict(list)
    bad_instants = set()
    for key, rows in instants.items():
        instant_axes[key[0]].append(key[1])
        if len({(r.get("lat"), r.get("lon")) for r in rows}) > 1 or any(
            row.get("invalid")
            or row.get("time_ambiguous")
            or row.get("time_nonexistent")
            for row in rows
        ):
            bad_instants.add(key)
    for axis in instant_axes.values():
        axis.sort()
    failures, counts = [], collections.Counter()
    for link in links:
        if link["status"] not in LOCALIZED_STATUSES:
            continue
        counts["localized_records"] += 1
        event = events.get(link["apc_rid"])
        if event is None:
            failures.append({"apc_rid": link["apc_rid"], "check": "event_ref"})
            continue
        witnesses = link.get("witnesses", [])
        if not witnesses:
            failures.append({"apc_rid": link["apc_rid"], "check": "has_witness"})
        for witness in witnesses:
            counts["witnesses"] += 1
            errors = []
            refs = witness.get("ping_ids", [])
            rows = [source[rid] for rid in refs if rid in source]
            if not refs or len(rows) != len(refs):
                errors.append("real_source_refs")
            if any(row.get("vehicle") != event.get("vehicle") for row in rows):
                errors.append("same_vehicle")
            if any(
                (row.get("vehicle"), _seconds(row["time"])) in bad_instants
                for row in rows
                if row.get("time") is not None
            ):
                errors.append("source_instant_not_conflicted_or_quarantined")
            if any(
                row.get("invalid")
                or row.get("time_ambiguous")
                or row.get("time_nonexistent")
                or row.get("time") is None
                for row in rows
            ):
                errors.append("usable_source_time")
            if (
                event.get("invalid")
                or event.get("time_ambiguous")
                or event.get("time_nonexistent")
            ):
                errors.append("usable_event_time")
            if errors:
                failures.extend(
                    {"apc_rid": link["apc_rid"], "check": error} for error in errors
                )
                continue
            instant = _seconds(witness["support_time"])
            if witness["kind"] == "point":
                counts["point_witnesses"] += 1
                if len(rows) != 1:
                    errors.append("point_one_source")
                else:
                    row = rows[0]
                    lat, lon = row["lat"], row["lon"]
                    if abs(instant - _seconds(row["time"])) > 1e-6:
                        errors.append("point_observed_instant")
            elif witness["kind"] == "interpolation":
                counts["interpolation_witnesses"] += 1
                if len(rows) != 2:
                    errors.append("interpolation_two_sources")
                else:
                    left, right = sorted(rows, key=lambda row: row["time"])
                    low, high = _seconds(left["time"]), _seconds(right["time"])
                    span = high - low
                    if not (
                        0 < span <= cfg["interpolation_gap_s"] and low < instant < high
                    ):
                        errors.append("bounded_interpolation_bracket")
                    else:
                        axis = instant_axes[left.get("vehicle")]
                        inside = axis[
                            bisect.bisect_right(axis, low) : bisect.bisect_left(
                                axis, high
                            )
                        ]
                        if inside:
                            errors.append("adjacent_observed_interpolation_bracket")
                        fraction = (instant - low) / span
                        lat = left["lat"] + fraction * (right["lat"] - left["lat"])
                        lon = left["lon"] + fraction * (right["lon"] - left["lon"])
                        speed = (
                            _haversine(
                                left["lat"], left["lon"], right["lat"], right["lon"]
                            )
                            / span
                            * 3.6
                        )
                        if speed > 130 + 1e-6:
                            errors.append("interpolation_speed")
                        if abs(span - witness["bracket_gap_s"]) > 1e-6:
                            errors.append("stored_bracket_gap")
            else:
                errors.append("known_witness_kind")
            if not errors:
                residual = _haversine(event["lat"], event["lon"], lat, lon)
                if residual > cfg["radius_m"] + 0.01:
                    errors.append("radius")
                if abs(residual - witness["distance_m"]) > 0.01:
                    errors.append("stored_distance")
                if abs(lat - witness["lat"]) > 1e-9 or abs(lon - witness["lon"]) > 1e-9:
                    errors.append("stored_position")
                target = _seconds(witness["target_time"])
                gap = abs(instant - target)
                if gap > cfg["time_tolerance_s"] + 1e-6:
                    errors.append("target_time_tolerance")
                if abs(gap - witness["gap_s"]) > 1e-6:
                    errors.append("stored_time_gap")
                low, high = _seconds(event["arrival"]), _seconds(event["departure"])
                if not low <= target <= high:
                    errors.append("target_inside_event")
                if witness["target"] == "arrival" and target != low:
                    errors.append("arrival_target")
                if witness["target"] == "departure" and target != high:
                    errors.append("departure_target")
            failures.extend(
                {"apc_rid": link["apc_rid"], "check": error} for error in errors
            )
    return dict(counts) | {
        "failures": failures,
        "passed": not failures,
        "scope": "Accepted witness source/time/geometry, not identity or accuracy.",
    }


def evaluate(
    avl: list[dict], apc: list[dict], segmentation: dict, v2_config: dict | None = None
) -> dict:
    # Import lazily so harness-only tests can run before the matcher is finalized.
    from .matcher import build_index, match_direct

    index = build_index(avl, v2_config)
    return {
        "v1": v1_match(
            apc, v1_support(avl, apc), segmentation, "spatiotemporal", V1_DEFAULTS
        ),
        "v2": match_direct(apc, index, segmentation, v2_config),
    }


def refine_segments(segmentation: dict, avl: list[dict]) -> dict:
    """Split owners at every observed instant, leaving the physical source intact."""
    result = copy.deepcopy(segmentation)
    result["segments"], result["ping_segment"] = [], {}
    by_instant = collections.defaultdict(list)
    for row in avl:
        if row["rid"] in segmentation["ping_segment"]:
            by_instant[(row["vehicle"], row["time"])].append(row)
    for key, rows in sorted(by_instant.items()):
        sid = "probe-refinement-" + hashlib.sha256(str(key).encode()).hexdigest()[:20]
        segment = {
            "segment_id": sid,
            "vehicle": key[0],
            "start": key[1],
            "end": key[1],
            "ping_ids": sorted(row["rid"] for row in rows),
            "chronology_unresolved": any(
                r.get("time_ambiguous") or r.get("time_nonexistent") for r in rows
            ),
            "position_uncertain": False,
        }
        result["segments"].append(segment)
        result["ping_segment"].update(dict.fromkeys(segment["ping_ids"], sid))
    return result


def aggregate(records: list[dict]) -> dict:
    grouped = collections.defaultdict(list)
    for row in records:
        grouped[(row["version"], row["probe"], row["selection_stratum"])].append(row)
    result = {}
    for key, rows in sorted(grouped.items()):
        counts, transitions, levels = (collections.Counter() for _ in range(3))
        for row in rows:
            for name, value in row.items():
                if isinstance(value, int):
                    counts[name] += value
            transitions.update(row["status_transitions"])
            levels.update(row["support_level_transitions"])
        result["__".join(key)] = dict(counts) | {
            "windows": len(rows),
            "status_transitions": dict(transitions),
            "support_level_transitions": dict(levels),
            "independent_accuracy": None,
        }
    return result


def run(args) -> None:
    os.umask(0o077)
    ttc = Path(__file__).resolve().parents[2]
    allowed = (ttc / "artifacts" / "apc-avl-direct-evidence-20260908").resolve()
    out = args.out.resolve()
    if not out.is_relative_to(allowed) or out == allowed:
        raise ValueError(
            "Output requires a new isolated direct-evidence study subdirectory"
        )
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite prior probe run: {out}")
    out.mkdir(parents=True, mode=0o700)
    started = time.perf_counter()
    selected, sources = read_frozen_selections(args.selection)
    _dump(out / "selection.json", selected)
    _dump(
        out / "manifest.json",
        {
            "source_selections": sources,
            "selected_windows": len(selected),
            "code_sha256": {
                str(path.relative_to(ttc)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for package in ("apc_avl_coverage_study", "apc_avl_direct_evidence")
                for path in sorted((ttc / "experiments" / package).glob("*.py"))
            },
            "selection_unchanged_from_previous_raw_feature_study": True,
            "merge_only_partition_frozen": True,
            "segmentation_method": "avl_context",
            "source_times_shifted_in_memory_only": True,
            "time_shifts_are_perturbations_not_clock_estimation": True,
            "gtfs_used": False,
            "human_labels_used": False,
            "independent_ground_truth": False,
            "accuracy_claimed": False,
            "publishable": False,
        },
    )
    summaries, checks, replays = [], [], []
    for number, selection in enumerate(selected, 1):
        frames = []
        for source, field in (("avl", "time"), ("apc", "arrival")):
            frames.append(
                pl.scan_parquet(args.audit / f"{source}.parquet")
                .filter(
                    (pl.col("vehicle") == selection["vehicle"])
                    & (pl.col(field) >= selection["window_start"])
                    & (pl.col(field) < selection["window_end"])
                )
                .sort(field, "rid")
                .collect()
                .to_dicts()
            )
        avl, apc = frames
        case = selection["window_case"]
        segmentation = segment_vehicle(avl, apc, "avl_context")
        original = evaluate(avl, apc, segmentation)
        variants = {"original": (avl, apc)} | perturbations(avl, apc, selection)
        aa, pp = list(avl), list(apc)
        rng = random.Random(int(selection["window_hash"], 16))
        rng.shuffle(aa)
        rng.shuffle(pp)
        variants["input_permutation"] = (aa, pp)
        variants["owner_refinement"] = (avl, apc)
        for probe, (changed_avl, changed_apc) in variants.items():
            origins = (
                {row["rid"] + ":probe_duplicate": row["rid"] for row in avl}
                if probe == "exact_avl_duplication"
                else {}
            )
            projected = project_frozen_segmentation(segmentation, changed_avl, origins)
            if probe == "owner_refinement":
                projected = refine_segments(segmentation, changed_avl)
            changed = (
                original
                if probe == "original"
                else evaluate(changed_avl, changed_apc, projected)
            )
            for version, links in changed.items():
                _save_links(out / "decisions" / case / probe / f"{version}.json", links)
                checks.append(
                    {
                        "window_case": case,
                        "version": version,
                        "check": probe + "_one_decision_per_source_apc",
                        "passed": sorted(row["apc_rid"] for row in links)
                        == sorted(row["rid"] for row in changed_apc),
                        "hard_v2_check": version == "v2",
                    }
                )
                summaries.append(
                    compare_links(original[version], links)
                    | {
                        "window_case": case,
                        "selection_stratum": selection["selection_stratum"],
                        "probe": probe,
                        "version": version,
                        "avl_rows": len(changed_avl),
                        "apc_rows": len(changed_apc),
                    }
                )
                if probe in ("input_permutation", "exact_avl_duplication"):
                    checks.append(
                        {
                            "window_case": case,
                            "version": version,
                            "check": probe + "_normalized_decision",
                            "passed": normalized_decisions(original[version])
                            == normalized_decisions(links, origins),
                            "hard_v2_check": version == "v2",
                        }
                    )
                if probe == "no_overlap_45m_separation":
                    checks.append(
                        {
                            "window_case": case,
                            "version": version,
                            "check": "disjoint_coverage_no_local_assignment",
                            "passed": all(
                                row["status"] not in LOCALIZED_STATUSES for row in links
                            ),
                            "hard_v2_check": version == "v2",
                        }
                    )
            replay = replay_witnesses(changed_avl, changed_apc, changed["v2"])
            replays.append(replay | {"window_case": case, "probe": probe})
        _dump(out / "summary_progress.json", summaries)
        _dump(out / "checks_progress.json", checks)
        print(
            json.dumps(
                {
                    "completed_windows": number,
                    "total_windows": len(selected),
                    "elapsed_s": round(time.perf_counter() - started, 2),
                }
            ),
            flush=True,
        )
    synthetic_results = {}
    for name, case in synthetic_cases().items():
        outputs = evaluate(case["avl"], case["apc"], case["segmentation"])
        synthetic_results[name] = {
            "question": case["question"],
            "constructed_not_accuracy_labels": True,
            "source_avl": case["avl"],
            "source_apc": case["apc"],
            "v1": outputs["v1"],
            "v2": outputs["v2"],
            "replay": replay_witnesses(case["avl"], case["apc"], outputs["v2"]),
        }
    _dump(out / "synthetic_cases.json", synthetic_results)
    _dump(out / "per_window_summary.json", summaries)
    _dump(out / "probe_summary.json", aggregate(summaries))
    _dump(out / "checks.json", checks)
    _dump(out / "witness_replays.json", replays)
    _dump(
        out / "completion.json",
        {
            "vehicle_windows": len(selected),
            "method_probe_windows": len(summaries),
            "synthetic_cases": len(synthetic_results),
            "elapsed_seconds": time.perf_counter() - started,
            "checks": len(checks),
            "failed_checks": [row for row in checks if not row["passed"]],
            "replay_runs": len(replays),
            "replay_failures": sum(len(row["failures"]) for row in replays),
            "synthetic_replay_failures": sum(
                len(row["replay"]["failures"]) for row in synthetic_results.values()
            ),
            "independent_accuracy": None,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--selection", type=Path, nargs="+", required=True)
    run(parser.parse_args())
