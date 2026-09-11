"""Persist three synthetic negative findings without changing segmentation.

Run with ``python -m experiments.apc_avl_coverage_study.segmentation_counterexamples``
from agencies/ttc. This diagnostic intentionally records robustness failures; it
is not a passing robustness test, a fix, or an estimate of real-data accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .segmentation import ALGORITHM_VERSION, DEFAULT_CONFIG, segment_vehicle

BASE = datetime(2024, 1, 1, 13)
ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[2] / "artifacts/apc-avl-coverage-study-20260908"
)


def ping(index: int, **fields) -> dict:
    return {
        "rid": f"synthetic-avl-{index:04}",
        "vehicle": "SYNTHETIC_VEHICLE",
        "time": BASE + timedelta(minutes=index),
        "lat": 43.7,
        "lon": -79.4 + index * 0.001,
        "route": "SYNTHETIC_ROUTE",
        "run": "SYNTHETIC_RUN",
        "destination": "E",
        "source_trip": "SYNTHETIC_SOURCE_LABEL",
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        **fields,
    }


def apc(index: int, **fields) -> dict:
    arrival = BASE + timedelta(minutes=index * 2)
    return {
        "rid": f"synthetic-apc-{index:04}",
        "vehicle": "SYNTHETIC_VEHICLE",
        "arrival": arrival,
        "departure": arrival + timedelta(seconds=10),
        "route": "SYNTHETIC_ROUTE",
        "lat": 43.7,
        "lon": -79.4 + index * 0.002,
        "direction": "E" if index < 4 else "W",
        "stop": f"synthetic-stop-{index}",
        "grain_uncertain": False,
        "invalid": False,
        "time_ambiguous": False,
        "time_nonexistent": False,
        **fields,
    }


def normalized_memberships(result: dict, origins: dict[str, str]) -> list[list[str]]:
    """Compare original evidence partitions, not IDs containing added lineage."""
    return sorted(
        sorted({origins.get(rid, rid) for rid in segment["ping_ids"]})
        for segment in result["segments"]
    )


def compare(
    name: str,
    method: str,
    base_avl: list[dict],
    changed_avl: list[dict],
    base_apc: list[dict],
    changed_apc: list[dict],
    origins: dict[str, str],
    explanation: str,
    negative_status: str,
) -> dict:
    baseline = segment_vehicle(base_avl, base_apc, method)
    changed = segment_vehicle(changed_avl, changed_apc, method)
    base_members = normalized_memberships(baseline, {})
    changed_members = normalized_memberships(changed, origins)
    differs = base_members != changed_members
    return {
        "name": name,
        "method": method,
        "status": negative_status if differs else "negative_finding_not_reproduced",
        "robustness_fail": differs if negative_status == "robustness_fail" else None,
        "terminal_role_representation_gap": (
            differs if negative_status == "terminal_role_representation_gap" else None
        ),
        "interpretation": explanation,
        "baseline_chunk_count": len(baseline["segments"]),
        "perturbed_chunk_count": len(changed["segments"]),
        "baseline_original_rid_memberships": base_members,
        "perturbed_original_rid_memberships": changed_members,
        "baseline_boundary_reasons": [x["reasons"] for x in baseline["boundaries"]],
        "perturbed_boundary_reasons": [x["reasons"] for x in changed["boundaries"]],
        "duplicate_to_original_rids": origins,
        "synthetic_input_fixtures": {
            "baseline_avl": base_avl,
            "perturbed_avl": changed_avl,
            "baseline_apc": base_apc,
            "perturbed_apc": changed_apc,
        },
    }


def findings() -> list[dict]:
    context = [ping(i) for i in range(7)] + [
        ping(7, route="SYNTHETIC_ALT"),
        ping(8, time=BASE + timedelta(seconds=450), route="SYNTHETIC_ALT"),
    ]
    copy = dict(context[-2], rid="synthetic-context-duplicate")
    geometry = [
        ping(i, lon=-79.4 + (i if i < 9 else 18 - i) * 0.001) for i in range(19)
    ]
    duplicates = [dict(row, rid=row["rid"] + "-duplicate") for row in geometry]
    terminal_avl = [ping(i) for i in range(20)]
    terminal_apc = [apc(i) for i in range(8)]
    for index in (3, 4):
        terminal_apc[index] = dict(
            terminal_apc[index],
            stop="synthetic-shared-terminal",
            lat=43.7,
            lon=-79.394,
        )
    overlapping = [dict(row) for row in terminal_apc]
    overlapping[3]["departure"] = overlapping[4]["arrival"] + timedelta(seconds=60)
    return [
        compare(
            "duplicate_manufactures_context_persistence",
            "avl_context",
            context,
            context + [copy],
            [],
            [],
            {copy["rid"]: context[-2]["rid"]},
            "One repeated measurement with new lineage raises raw point count "
            "above the persistence minimum without adding a new measurement time.",
            "robustness_fail",
        ),
        compare(
            "duplicates_suppress_geometric_reversal",
            "avl_context",
            geometry,
            geometry + duplicates,
            [],
            [],
            {
                copy["rid"]: original["rid"]
                for copy, original in zip(duplicates, geometry, strict=True)
            },
            "Repeated measurements shorten the physical span of fixed raw-row "
            "reversal windows and suppress a previously detected reversal.",
            "robustness_fail",
        ),
        compare(
            "overlapping_terminal_roles_suppress_direction_boundary",
            "apc_assisted",
            terminal_avl,
            terminal_avl,
            terminal_apc,
            overlapping,
            {},
            "A longer previous terminal-role interval makes earliest_boundary "
            "later than latest_boundary. The direction candidate is discarded. "
            "This is a representation limitation, not proof every overlap is "
            "a real physical trip boundary.",
            "terminal_role_representation_gap",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ARTIFACT_ROOT / "segmentation_counterexamples.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ARTIFACT_ROOT.resolve()):
        raise ValueError("Output must remain inside the isolated study artifact root")
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing diagnostic")
    os.umask(0o077)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cases = findings()
    report = {
        "synthetic_only": True,
        "raw_data_used": False,
        "does_not_change_core_algorithm": True,
        "not_an_accuracy_estimate": True,
        "algorithm_version": ALGORITHM_VERSION,
        "nominal_segmentation_config": DEFAULT_CONFIG,
        "core_sha256": {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in ("segmentation.py", "merge.py", "raw_audit.py", "run_study.py")
        },
        "diagnostic_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": cases,
    }
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str, allow_nan=False)
        handle.write("\n")
    output.chmod(0o600)
    print(
        json.dumps(
            [
                {
                    key: case[key]
                    for key in (
                        "name",
                        "status",
                        "baseline_chunk_count",
                        "perturbed_chunk_count",
                    )
                }
                for case in cases
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
