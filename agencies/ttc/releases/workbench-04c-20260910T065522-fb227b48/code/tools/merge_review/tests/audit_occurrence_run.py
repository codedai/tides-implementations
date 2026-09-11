"""Read-only, one-case-at-a-time 04 -> occurrence-model full-run audit.

Hash each compressed case while reading it, then inspect all four modes in the
same pass. No matching, ingestion, labeling, model activation or source writes.
Only the new report is written. Descriptive changes are never accuracy claims.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import math
import statistics
import time
from itertools import groupby
from pathlib import Path, PurePosixPath

from tools.merge_review.gtfs_matching.continuous.apc import normalize_apc
from tools.merge_review.gtfs_matching.continuous.calibration import (
    occurrence_decision_guard,
)
from tools.merge_review.gtfs_matching.continuous.config import get_config
from tools.merge_review.gtfs_matching.continuous.evaluation import evaluate_shadow
from tools.merge_review.gtfs_matching.continuous.evidence import valid_projection
from tools.merge_review.gtfs_matching.continuous.retrieval import schedule_pool
from tools.merge_review.gtfs_matching.continuous.serialization import validate_result
from tools.merge_review.gtfs_matching.multimode import validate_modes
from tools.merge_review.gtfs_matching.partial import partition_projection
from tools.merge_review.gtfs_matching.retrieval import for_reference
from tools.merge_review.gtfs_matching.runner import delay_clock_violations
from tools.merge_review.gtfs_matching.windows import observation_key

MODES = ("global", "early", "middle", "late")
COMPONENTS = {
    "absolute_delay_level_cost",
    "conditioned_fit_cost",
    "delay_dynamics_cost",
    "APC_stop_time_cost",
    "projection_and_coverage_cost",
    "pattern_progress_cost",
    "vehicle_sequence_cost",
    "occurrence_displacement_cost",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def same_number(first, second):
    if first is None or second is None:
        return first is second
    return (
        finite(first) and finite(second) and math.isclose(first, second, abs_tol=1e-6)
    )


def distribution(values):
    ordered = sorted(value for value in values if finite(value))
    return {
        "count": len(ordered),
        "min": min(ordered, default=None),
        "median": statistics.median(ordered) if ordered else None,
        "p90": ordered[min(len(ordered) - 1, math.floor(len(ordered) * 0.9))]
        if ordered
        else None,
        "max": max(ordered, default=None),
    }


class PinnedRun:
    """Checked local targets, immutable-byte pins and end-of-audit stat checks."""

    def __init__(self, folder):
        self.folder = Path(folder).absolute()
        require(self.folder.is_dir(), f"Missing run: {folder}")
        self.stats, self.hashes, self.controls = {}, {}, {}
        for name in ("output_sha256.json", "completion.json", "run.json"):
            self.controls[name] = self.read(name)
        self.pins = self.controls["output_sha256.json"]
        require(isinstance(self.pins, dict) and self.pins, "Missing run output pins")
        require(
            self.controls["completion.json"].get("validation_ok") is True,
            "Run not completed and validated",
        )
        require(
            self.controls["completion.json"].get("available_matching_modes")
            == list(MODES),
            "Incomplete mode matrix",
        )
        self.verified = set()

    def path(self, name):
        relative = PurePosixPath(name)
        require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and relative.parts,
            "Escaping artifact path",
        )
        path = self.folder.joinpath(*relative.parts)
        require(
            all(not parent.is_symlink() for parent in (path, *path.parents)),
            "Symlink in audit input path",
        )
        return path

    @staticmethod
    def stat_key(path):
        stat = path.stat()
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def read(self, name, *, parse=True, expected=None):
        path = self.path(name)
        before = self.stat_key(path)
        if parse:
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
        else:
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        require(
            before == self.stat_key(path), f"Artifact changed while reading: {name}"
        )
        if expected is not None:
            require(
                digest == expected,
                f"Pinned artifact changed: {self.folder.name}/{name}",
            )
            self.verified.add(name)
        self.stats[name], self.hashes[name] = before, digest
        if not parse:
            return None
        return json.loads(gzip.decompress(payload) if name.endswith(".gz") else payload)

    def read_pinned(self, name, *, parse=True):
        require(name in self.pins, f"Unpinned required artifact: {name}")
        return self.read(name, parse=parse, expected=self.pins[name])

    def verify_rest(self):
        for name, digest in self.pins.items():
            if name not in self.verified:
                self.read(name, parse=False, expected=digest)

    def verify_unchanged(self):
        require(self.verified == set(self.pins), "Not every pinned output was audited")
        for name, previous in self.stats.items():
            require(
                previous == self.stat_key(self.path(name)),
                f"Input mutated during audit: {name}",
            )
        for name in self.controls:
            require(
                hashlib.sha256(self.path(name).read_bytes()).hexdigest()
                == self.hashes[name],
                f"Run control changed: {name}",
            )


def _context(pool, candidate, stored):
    return pool.confusability(
        pool.trips[candidate["candidate_id"]],
        stored["observed_s_range"],
        stored.get("anchor_s_m", stored.get("observed_s")),
        stored["observed_t"],
        observed_stop_ids=stored.get("observed_stop_ids", ()),
    )


def _check_headway_context(pool, candidate, stored, counts, projection, config):
    if not stored:
        return
    scoped = [
        row
        for row in for_reference(
            projection["points"], candidate["reference_id"], include_unprojectable=True
        )
        if valid_projection(row, config)
    ]
    lower_t, upper_t = min(row["t"] for row in scoped), max(row["t"] for row in scoped)
    lower_s, upper_s = stored["observed_s_range"]
    stops = sorted(
        (
            row
            for row in normalize_apc(
                projection.get("apc_evidence", []), candidate["reference_id"]
            )
            if row["status"] == "EVIDENCE"
            and row.get("stop_id") is not None
            and row["confidence"] > 0
            and row["confidence"] >= config["continuous_occurrence_apc_min_confidence"]
            and lower_s <= row["canonical_stop_s"] <= upper_s
            and row["interval_end"] >= lower_t
            and row["interval_start"] <= upper_t
        ),
        key=lambda row: row["interval_start"],
    )
    identities = [key for key, _ in groupby(str(row["stop_id"]) for row in stops)]
    fresh = _context(pool, candidate, stored | {"observed_stop_ids": identities})
    for key in ("class_id", "member_ids", "headway_source", "candidate_compatible"):
        require(
            stored.get(key) == fresh.get(key),
            f"Incorrect common-cohort provenance: {key}",
        )
    for key in (
        "local_headway_s",
        "headway_confidence",
        "previous_headway_s",
        "next_headway_s",
    ):
        require(
            same_number(stored.get(key), fresh.get(key)),
            f"Incorrect scheduled passage interval: {key}",
        )
    # Validate candidate-relative gaps independently from the persisted fields.
    index = pool.occurrence_index()
    table = index.passages(index.classes[fresh["class_id"]], fresh["anchor_s_m"])
    location = table["by_id"].get(candidate["candidate_id"])
    if location:
        position, own = location
        groups = table["groups"]
        expected = {
            "previous_headway_s": own["time"] - groups[position - 1]["time"]
            if position
            else None,
            "next_headway_s": groups[position + 1]["time"] - own["time"]
            if position + 1 < len(groups)
            else None,
        }
        for key, value in expected.items():
            require(
                same_number(stored.get(key), value),
                f"Gap is not between real GTFS passages: {key}",
            )
    counts["candidate_anchor_headways_verified"] += 1


def _check_retrieval(result, pool, config, counts):
    retrieval = result["retrieval"]
    union = set(retrieval["candidate_ids"])
    require(
        union == {row["candidate_id"] for row in result["all_candidates"]},
        "Scored candidate union differs from retrieval",
    )
    require(not retrieval.get("union_truncated"), "Retrieval union was truncated")
    emitted = set()
    for window in retrieval["windows"]:
        candidates = window.get("candidates", [])
        kept = {row["candidate_id"]: row for row in candidates}
        require(len(kept) == len(candidates), "Duplicate retrieval candidate in window")
        emitted.update(kept)
        checked = set()
        for candidate in candidates:
            require(
                candidate.get("retrieval_reasons"),
                "Candidate missing retrieval justification",
            )
            require(
                same_number(
                    candidate["core_buffer_s"],
                    candidate["local_headway_s"] * config["continuous_core_headways"],
                ),
                "Core buffer does not use local scheduled headway",
            )
            require(
                candidate["rescue_buffer_s"] >= candidate["core_buffer_s"],
                "Rescue buffer smaller than core",
            )
            if candidate["confusability_class_id"] in checked:
                continue
            checked.add(candidate["confusability_class_id"])
            context = _context(pool, candidate, candidate)
            require(
                context["class_id"] == candidate["confusability_class_id"],
                "Window common-cohort identity changed",
            )
            require(
                same_number(context["local_headway_s"], candidate["local_headway_s"]),
                "Window local headway differs from GTFS cohort",
            )
            index = pool.occurrence_index()
            table = index.passages(
                index.classes[context["class_id"]], candidate["observed_s"]
            )
            groups = table["groups"]
            if not groups:
                continue
            timestamp = candidate["observed_t"]
            distances = [
                max(group["lower_t"] - timestamp, timestamp - group["upper_t"], 0.0)
                for group in groups
            ]
            closest = [
                i
                for i, value in enumerate(distances)
                if abs(value - min(distances)) <= config["continuous_occurrence_tie_s"]
            ]
            required = collections.defaultdict(set)
            neighbors = max(1, int(config["continuous_occurrence_rescue_neighbors"]))
            for position in closest:
                for i in range(
                    max(0, position - neighbors),
                    min(len(groups), position + neighbors + 1),
                ):
                    reason = (
                        "CORE_NEAREST_AND_ADJACENT"
                        if abs(i - position) <= 1
                        else "RESCUE_EXTRA_NEIGHBOR"
                    )
                    for member in groups[i]["members"]:
                        required[member["candidate_id"]].add(reason)
            for identity, reasons in required.items():
                require(
                    identity in kept,
                    "Protected nearest/tied/adjacent occurrence missing",
                )
                require(
                    kept[identity].get("protected_from_top_k") is True,
                    "Mandatory neighbor lost protection",
                )
                require(
                    reasons <= set(kept[identity]["retrieval_reasons"]),
                    "Mandatory-neighbor provenance missing",
                )
            counts["protected_occurrence_memberships_verified"] += len(required)
            counts["retrieval_cohorts_verified"] += 1
    require(emitted == union, "Candidate union is not the union of saved windows")


def _audit_candidate(candidate, config, common_context, counts):
    require(
        set(candidate["score_components"]) == COMPONENTS,
        "Expected eight occurrence score components",
    )
    require(
        candidate.get("operational_context") == common_context,
        "Prior context changes by candidate",
    )
    require(
        candidate.get("delay_prior_source") != "local_scheduled_headway",
        "Candidate headway used as operational delay prior",
    )
    require(
        candidate.get("probability") is None and candidate.get("calibrated") is False,
        "Unlabeled candidate published probability",
    )
    require(
        candidate.get("occurrence_decision_guard")
        == occurrence_decision_guard(candidate),
        "Incorrect occurrence identity guard",
    )
    fit = candidate.get("delay_fit", {})
    process = candidate["whole_alignment"]["delay_process"]
    if process:
        prior = fit.get("prior_provenance", {})
        require(
            prior.get("candidate_headway_used") is False,
            "Prior depends on candidate headway",
        )
        require(
            prior.get("calibrated") is False,
            "Unlabeled operational prior marked calibrated",
        )
        require(
            prior.get("source") == "uncalibrated_operational_defaults",
            "Unexpected real-history prior without independent labels",
        )
        for row in process:
            pieces = row["uncertainty_components"]
            require(
                all(finite(value) and value >= 0 for value in pieces.values()),
                "Invalid uncertainty source",
            )
            require(
                same_number(
                    row["uncertainty_s"],
                    math.sqrt(sum(value * value for value in pieces.values())),
                ),
                "Uncertainty quadrature mismatch",
            )
            require(
                row.get("occurrence_displacement") is not None,
                "Missing per-point occurrence evidence",
            )
            require(
                all(
                    finite(row[key]) and row[key] >= 0
                    for key in (
                        "conditioned_fit_cost",
                        "absolute_delay_level_cost",
                        "occurrence_displacement_cost",
                    )
                ),
                "Nonfinite point score component",
            )
            counts["source_clock_and_uncertainty_points_verified"] += 1
    counts["candidate_scores_audited"] += 1


def _episode(result, case_id, mode, manifest):
    candidates = result.get("all_candidates", [])
    best = candidates[0] if candidates else {}
    return {
        "case_id": case_id,
        "matching_mode": mode,
        "feed_version": best.get("feed_version"),
        "route_id": "29",
        "service_date": manifest["debug_date"],
        "status": result["status"],
        "all_candidates": [
            {
                key: row.get(key)
                for key in (
                    "candidate_id",
                    "trip_id",
                    "score",
                    "probability",
                    "calibrated",
                    "inferred_delay_regime",
                )
            }
            | {
                "occurrence_displacement": {
                    key: (row.get("occurrence_displacement") or {}).get(key)
                    for key in ("crossed_occurrences", "outside_cell_s")
                }
            }
            for row in candidates
        ],
        "candidates": result.get("candidates", []),
        "local_headway_s": (
            best.get("occurrence_displacement", {}).get("context") or {}
        ).get("local_headway_s"),
        "valid_projection_point_ratio": best.get("valid_projection_point_ratio"),
        "inferred_delay_regime": best.get("inferred_delay_regime"),
    }


def audit(source, target, *, progress=None):
    started = time.perf_counter()
    source, target = PinnedRun(source), PinnedRun(target)
    old_manifest, manifest = (
        source.read_pinned("manifest.json"),
        target.read_pinned("manifest.json"),
    )
    old_config, config = (
        source.read_pinned("config.json")["gtfs"],
        target.read_pinned("config.json")["gtfs"],
    )
    require(
        manifest["partial_source_run_id"] == source.folder.name,
        "Target was not replayed from supplied source",
    )
    require(
        manifest["debug_date"] == old_manifest["debug_date"] == "2023-11-06",
        "Experiment day changed",
    )
    require(
        manifest["route_scope"] == old_manifest["route_scope"] == ["29"],
        "Route scope changed",
    )
    require(
        config.get("continuous_occurrence_model_enabled") is True,
        "Target occurrence model not enabled",
    )
    require(
        not old_config.get("continuous_occurrence_model_enabled", False),
        "Source is not pre-occurrence Experiment04",
    )
    config = get_config(config)
    require(
        config.get("continuous_operational_prior_artifact") is None,
        "This audit requires the declared no-label experiment",
    )
    for key in (
        "continuous_absolute_delay_weight",
        "continuous_delay_dynamics_weight",
        "continuous_conditioned_fit_weight",
    ):
        require(
            config[key] == old_config[key],
            f"Experiment04 baseline weight changed: {key}",
        )
    cases = sorted(name for name in target.pins if name.startswith("cases/"))
    require(
        cases
        and set(cases) == {name for name in source.pins if name.startswith("cases/")},
        "Case membership changed",
    )
    conserved = [
        name
        for name in source.pins
        if name.startswith(("observations/", "scope/", "gtfs/"))
        or name == "selection.json"
    ]
    for name in conserved:
        require(
            source.pins[name] == target.pins.get(name),
            f"Upstream file not conserved: {name}",
        )
    canonical = target.read_pinned("gtfs/canonical.json")
    for reader in (source, target):
        for name, digest in reader.pins.items():
            if not name.startswith("cases/") and name not in reader.verified:
                reader.read(name, parse=False, expected=digest)
    counts, observations = collections.Counter(), collections.defaultdict(set)
    stats = {
        mode: {
            "counts": collections.Counter(
                {
                    "old_any_point_headway_ge_3500": 0,
                    "old_median_headway_ge_3500": 0,
                }
            ),
            "distributions": collections.defaultdict(list),
            "new_statuses": collections.Counter(),
            "old_statuses": collections.Counter(),
            "headway_sources": collections.Counter(),
            "guard_reasons": collections.Counter(),
            "high_old_headway_examples": [],
        }
        for mode in MODES
    }
    episodes = {mode: {"old": [], "new": []} for mode in MODES}
    for number, name in enumerate(cases, 1):
        previous, artifact = source.read_pinned(name), target.read_pinned(name)
        for field in ("case_id", "projection", "apc_evidence", "parent_boundary"):
            require(
                artifact.get(field) == previous.get(field),
                f"Upstream case evidence changed: {name}:{field}",
            )
        validate_modes(
            artifact["projection"],
            artifact["result"],
            artifact["matching_modes"],
            config,
        )
        points = artifact["projection"]["points"]
        scopes = partition_projection(artifact["projection"], config)
        for row in points + artifact["projection"].get("apc_evidence", []):
            observations[row.get("source", "avl")].add(observation_key(row))
        pool = schedule_pool(
            canonical["trips"], config, references=canonical["references"]
        )
        for mode in MODES:
            old_entry, entry = (
                previous["matching_modes"][mode],
                artifact["matching_modes"][mode],
            )
            require(
                old_entry["selection"] == entry["selection"],
                f"Partial decision evidence changed: {name}/{mode}",
            )
            old_result, result = old_entry["result"], entry["result"]
            validate_result(result)
            require(
                not delay_clock_violations(result, points),
                "Raw or corrected observation clock changed",
            )
            require(
                all(
                    not result["diagnostics"].get(key)
                    for key in (
                        "production_dtw_used",
                        "upstream_schedule_derived_features_used",
                        "apc_used_as_gps",
                        "raw_source_clock_modified",
                    )
                ),
                "Forbidden scoring feature or clock mutation",
            )
            require(
                result["status"] != "MATCHED" and result["candidates"] == [],
                "Uncalibrated run made scheduled-trip decision",
            )
            require(
                result["calibration"]["calibrated"] is False,
                "No-label calibrator claimed validation",
            )
            _check_retrieval(result, pool, config, counts)
            candidates = result["all_candidates"]
            old_candidates = old_result["all_candidates"]
            common_context = (
                candidates[0].get("operational_context") if candidates else None
            )
            for candidate in candidates:
                _audit_candidate(candidate, config, common_context, counts)
                _check_headway_context(
                    pool,
                    candidate,
                    candidate["occurrence_displacement"].get("context"),
                    counts,
                    scopes[mode]["projection"],
                    config,
                )
            row = stats[mode]
            row["old_statuses"][old_result["status"]] += 1
            row["new_statuses"][result["status"]] += 1
            old_best = old_candidates[0] if old_candidates else {}
            best = candidates[0] if candidates else {}
            old_id, new_id = old_best.get("candidate_id"), best.get("candidate_id")
            row["counts"]["paired_top1_cases"] += bool(old_id and new_id)
            row["counts"]["top1_changes"] += bool(
                old_id and new_id and old_id != new_id
            )
            row["counts"]["top1_availability_changes"] += bool(old_id) != bool(new_id)
            for key, value in (
                ("old_candidate_union", len(old_candidates)),
                ("new_candidate_union", len(candidates)),
                ("old_top1_score", old_best.get("score")),
                ("new_top1_score", best.get("score")),
                ("old_top1_delay_s", old_best.get("estimated_delay_s")),
                ("new_top1_delay_s", best.get("estimated_delay_s")),
            ):
                row["distributions"][key].append(value)
            old_by_id = {
                candidate["candidate_id"]: candidate for candidate in old_candidates
            }
            new_by_id = {
                candidate["candidate_id"]: candidate for candidate in candidates
            }
            common_ids = old_by_id.keys() & new_by_id.keys()
            row["counts"]["common_candidate_comparisons"] += len(common_ids)
            row["counts"]["source_only_candidates"] += len(
                old_by_id.keys() - new_by_id.keys()
            )
            row["counts"]["target_only_candidates"] += len(
                new_by_id.keys() - old_by_id.keys()
            )
            for candidate in candidates:
                context = candidate["occurrence_displacement"].get("context") or {}
                row["headway_sources"][
                    context.get("headway_source", "NO_EVIDENCE")
                ] += 1
                row["guard_reasons"][
                    candidate["occurrence_decision_guard"]["reason"]
                ] += 1
                for key in ("local_headway_s", "previous_headway_s", "next_headway_s"):
                    row["distributions"]["new_" + key].append(context.get(key))
            for identity in common_ids:
                old, new = old_by_id[identity], new_by_id[identity]
                headways = [
                    p["headway_s"]
                    for p in old["whole_alignment"]["delay_process"]
                    if finite(p.get("headway_s"))
                ]
                old_median = statistics.median(headways) if headways else None
                context = new["occurrence_displacement"].get("context") or {}
                row["counts"]["old_any_point_headway_ge_3500"] += any(
                    value >= 3500 for value in headways
                )
                if old_median is not None and old_median >= 3500:
                    row["counts"]["old_median_headway_ge_3500"] += 1
                    row["distributions"][
                        "new_local_headway_when_old_median_ge_3500"
                    ].append(context.get("local_headway_s"))
                    if len(row["high_old_headway_examples"]) < 12:
                        row["high_old_headway_examples"].append(
                            {
                                "case_id": artifact["case_id"],
                                "candidate_id": identity,
                                "old_median_headway_s": old_median,
                                "new_local_headway_s": context.get("local_headway_s"),
                                "new_headway_source": context.get("headway_source"),
                                "new_previous_headway_s": context.get(
                                    "previous_headway_s"
                                ),
                                "new_next_headway_s": context.get("next_headway_s"),
                            }
                        )
            episodes[mode]["old"].append(
                _episode(old_result, artifact["case_id"], mode, manifest)
            )
            episodes[mode]["new"].append(
                _episode(result, artifact["case_id"], mode, manifest)
            )
            counts["mode_results_audited"] += 1
        counts["cases_audited"] += 1
        if progress and (number == 1 or number % 10 == 0 or number == len(cases)):
            progress(
                {
                    "cases": number,
                    "total": len(cases),
                    "elapsed_s": time.perf_counter() - started,
                }
            )
    for name, digest in manifest["partial_source_sha256"].items():
        require(
            source.hashes.get(name) == digest,
            f"Target source provenance does not match supplied source: {name}",
        )
    source.verify_unchanged()
    target.verify_unchanged()
    return {
        "status": "PASS",
        "schema_version": "occurrence-full-run-audit-v1",
        "source_run_id": source.folder.name,
        "run_id": target.folder.name,
        "elapsed_s": time.perf_counter() - started,
        "counts": dict(counts),
        "source_pins_verified": len(source.pins),
        "target_pins_verified": len(target.pins),
        "source_controls_sha256": {key: source.hashes[key] for key in source.controls},
        "target_controls_sha256": {key: target.hashes[key] for key in target.controls},
        "conserved_upstream_files": len(conserved),
        "all_four_mode_selections_unchanged": True,
        "projection_apc_parent_boundaries_unchanged": True,
        "source_clocks_unchanged": True,
        "source_observation_counts": {
            key: len(values) for key, values in observations.items()
        },
        "canonical_references": len(canonical["references"]),
        "canonical_trips": len(canonical["trips"]),
        "mode_statistics_descriptive_only": {
            mode: {
                key: {name: distribution(items) for name, items in value.items()}
                if key == "distributions"
                else dict(value)
                if isinstance(value, collections.Counter)
                else value
                for key, value in values.items()
            }
            for mode, values in stats.items()
        },
        "heldout_evaluation": {
            mode: evaluate_shadow(
                values["old"], values["new"], labels=[], config=config
            )
            for mode, values in episodes.items()
        },
        "accuracy_claimed": False,
        "independent_labels_available": False,
        "high_headway_definition": (
            "Common source/target candidate identities; old median and any-point "
            ">=3500s counts reported separately, each candidate once per case/mode."
        ),
        "validation_scope": (
            "All score/clock/projection records; canonical cohort/headway "
            "recomputation at each candidate anchor and retained window cohort; "
            "all protected nearest, simultaneous and adjacent occurrences checked. "
            "No fitted delay was recomputed."
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    require(not args.output.exists(), "Audit report already exists")
    try:
        report = audit(
            args.source,
            args.run,
            progress=lambda value: print(json.dumps(value), flush=True),
        )
    except Exception as exc:
        report = {
            "status": "FAIL",
            "source_run_id": args.source.name,
            "run_id": args.run.name,
            "error": f"{type(exc).__name__}: {exc}",
            "accuracy_claimed": False,
        }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(args.output),
                "error": report.get("error"),
            }
        ),
        flush=True,
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
