"""Read-only full-run audit; write one new report outside immutable run inputs."""

import argparse
import collections
import json
import statistics
from pathlib import Path

from tools.merge_review.gtfs_matching.continuous.serialization import validate_result
from tools.merge_review.gtfs_matching.feed import file_sha256
from tools.merge_review.gtfs_matching.runner import delay_clock_violations, read_case


def audit(source, target, *, reweight_only=False):
    source_pins = json.loads((source / "output_sha256.json").read_text())
    target_pins = json.loads((target / "output_sha256.json").read_text())
    for folder, pins in ((source, source_pins), (target, target_pins)):
        for name, digest in pins.items():
            if file_sha256(folder / name) != digest:
                raise ValueError(f"Pinned artifact changed: {folder.name}/{name}")
    modes = ("global", "early", "middle", "late")
    stats = {
        mode: {
            "statuses": collections.Counter(),
            "evidence": collections.Counter(),
            "diagnostic_candidate_counts": [],
            "eligible_counts": [],
            "top_projection_point_ratio": [],
            "top_schedule_point_ratio": [],
        }
        for mode in modes
    }
    counts = collections.Counter()
    reweight_stats = {
        mode: {
            "top1_changes": 0,
            "source_top1_cost_gate_pass": 0,
            "new_top1_cost_gate_pass": 0,
            "source_top1_scores": [],
            "new_top1_scores": [],
        }
        for mode in modes
    }
    source_config = json.loads((source / "config.json").read_text())["gtfs"]
    target_config = json.loads((target / "config.json").read_text())["gtfs"]
    sources = collections.defaultdict(set)
    files = sorted(name for name in target_pins if name.startswith("cases/"))
    if set(files) != {name for name in source_pins if name.startswith("cases/")}:
        raise ValueError("Case membership changed")
    for name in files:
        previous, artifact = read_case(source / name), read_case(target / name)
        for field in ("projection", "apc_evidence", "parent_boundary"):
            if artifact.get(field) != previous.get(field):
                raise ValueError(f"Upstream input mutated: {name}:{field}")
        for row in artifact["projection"]["points"] + artifact["projection"].get(
            "apc_evidence", []
        ):
            sources[row.get("source", "avl")].add(row.get("source_rid", row.get("rid")))
        for mode in modes:
            result = artifact["matching_modes"][mode]["result"]
            validate_result(result)
            if delay_clock_violations(result, artifact["projection"]["points"]):
                raise ValueError("Invalid corrected clock")
            if (
                result["diagnostics"]["production_dtw_used"]
                or result["diagnostics"]["upstream_schedule_derived_features_used"]
            ):
                raise ValueError("Forbidden production feature used")
            candidates = result["all_candidates"]
            if reweight_only:
                old_mode = previous["matching_modes"][mode]
                new_mode = artifact["matching_modes"][mode]
                if old_mode["selection"] != new_mode["selection"]:
                    raise ValueError("Decision scope changed during reweighting")
                old_candidates = {
                    c["candidate_id"]: c for c in old_mode["result"]["all_candidates"]
                }
                if set(old_candidates) != {c["candidate_id"] for c in candidates}:
                    raise ValueError("Retrieval membership changed during reweighting")
                for candidate in candidates:
                    old = old_candidates[candidate["candidate_id"]]
                    for field in (
                        "coverage_metrics",
                        "delay_fit",
                        "estimated_delay_s",
                        "delay_mad_s",
                        "delay_uncertainty_s",
                        "apc_support",
                        "projection_or_passage_rejections",
                        "scheduled_trajectory",
                    ):
                        if old[field] != candidate[field]:
                            raise ValueError(
                                f"Reweighting changed {field}: {name}/{mode}"
                            )
                    if (
                        old["whole_alignment"]["delay_process"]
                        != candidate["whole_alignment"]["delay_process"]
                    ):
                        raise ValueError("Reweighting changed fitted delay states")
                    # A no-fit sentinel depends on the whole-score gate; actual
                    # fitted residuals and all other raw components must not.
                    for key, value in old["score_components_unweighted"].items():
                        if (
                            key == "conditioned_fit_cost"
                            and not old["whole_alignment"]["delay_process"]
                        ):
                            if (
                                value != source_config["continuous_match_cost_max"] + 1
                                or candidate["score_components_unweighted"][key]
                                != target_config["continuous_match_cost_max"] + 1
                            ):
                                raise ValueError(
                                    "Invalid no-fit conditioned cost sentinel"
                                )
                            continue
                        if value != candidate["score_components_unweighted"][key]:
                            raise ValueError(f"Reweighting changed raw component {key}")
                    counts["unchanged_candidate_fits_verified"] += 1
                if candidates:
                    old_best = old_mode["result"]["all_candidates"][0]
                    best = candidates[0]
                    change = reweight_stats[mode]
                    change["top1_changes"] += (
                        old_best["candidate_id"] != best["candidate_id"]
                    )
                    change["source_top1_scores"].append(old_best["score"])
                    change["new_top1_scores"].append(best["score"])
                    change["source_top1_cost_gate_pass"] += (
                        old_best["score"] <= source_config["continuous_match_cost_max"]
                    )
                    change["new_top1_cost_gate_pass"] += (
                        best["score"] <= target_config["continuous_match_cost_max"]
                    )
            row = stats[mode]
            row["statuses"][result["status"]] += 1
            row["evidence"][result["evidence_status"]] += 1
            row["diagnostic_candidate_counts"].append(len(candidates))
            row["eligible_counts"].append(
                sum(c["quality_eligible"] for c in candidates)
            )
            counts["candidate_scores_audited"] += len(candidates)
            counts["mode_results_audited"] += 1
            if candidates:
                row["top_projection_point_ratio"].append(
                    candidates[0]["coverage_metrics"]["projection"]["point_ratio"]
                )
                row["top_schedule_point_ratio"].append(
                    candidates[0]["coverage_metrics"]["schedule"]["point_ratio"]
                )
    summaries = {}
    for mode, values in stats.items():
        summaries[mode] = {
            key: dict(value)
            if isinstance(value, collections.Counter)
            else {
                "count": len(value),
                "median": statistics.median(value) if value else None,
                "min": min(value, default=None),
                "max": max(value, default=None),
            }
            for key, value in values.items()
        }
    for change in reweight_stats.values():
        for key in ("source_top1_scores", "new_top1_scores"):
            values = sorted(change.pop(key))
            change[key] = {
                "count": len(values),
                "median": statistics.median(values) if values else None,
                "p90": values[min(len(values) - 1, int(len(values) * 0.9))]
                if values
                else None,
                "max": max(values, default=None),
            }
    return {
        "status": "PASS",
        "source_run_id": source.name,
        "run_id": target.name,
        "case_count": len(files),
        "counts": dict(counts),
        "source_observation_counts": {key: len(ids) for key, ids in sources.items()},
        "mode_statistics_descriptive_only": summaries,
        "source_pins_verified": len(source_pins),
        "new_pins_verified": len(target_pins),
        "upstream_fields_unchanged": True,
        "accuracy_claimed": False,
        **(
            {
                "reweight_invariance_verified": True,
                "reweight_comparison": reweight_stats,
            }
            if reweight_only
            else {}
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reweight-only", action="store_true")
    args = parser.parse_args()
    report = audit(args.source, args.run, reweight_only=args.reweight_only)
    with args.output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(report, ensure_ascii=False))
