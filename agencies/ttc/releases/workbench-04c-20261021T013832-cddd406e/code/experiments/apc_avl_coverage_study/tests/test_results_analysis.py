"""Regression checks for evidence accounting, independent of TTC raw files."""

import json

import polars as pl
import pytest

from experiments.apc_avl_coverage_study.analyze_results import (
    conservation_rule,
    invalid_ping_references,
    load_counterexamples,
)


def test_ping_identity_requires_exact_digest_namespace_and_decimal_ordinal():
    digest = "a" * 64
    prefix = f"avl:{digest}:"
    invalid = [
        prefix + "extra:1",
        prefix + "+1",
        prefix + "01",
        prefix + " 1",
        prefix + "0",
        prefix + "4",
        prefix + "9" * 50,
        "apc:" + digest + ":1",
        "avl:" + "b" * 64 + ":1",
    ]
    frame = pl.DataFrame({"rid": [prefix + "1", prefix + "3", *invalid]})
    assert invalid_ping_references(frame, {"sha256": digest, "row_count": 3}) == len(
        invalid
    )


@pytest.mark.parametrize(
    "violation", ["duplicate_source_memberships", "invalid_source_references"]
)
def test_source_conservation_rejects_extra_avl_members_even_without_missing_rows(
    violation,
):
    apc_checks = {
        "source_disposition_count_difference": 0,
        "duplicate_apc_dispositions": 0,
        "apc_references_missing_from_source": 0,
    }
    chunk = {
        "raw_avl_rows": 10,
        "source_memberships": 11,
        "raw_avl_rows_without_chunk": 0,
        "duplicate_source_memberships": 0,
        "invalid_source_references": 0,
    }
    chunk[violation] = 1
    result = conservation_rule(5, apc_checks, chunk)
    assert result["status"] == "fail"
    assert result["violations"] == 1
    assert result["evaluated"] == 15


@pytest.mark.parametrize("corruption", ["core_hash", "reported_status"])
def test_counterexample_claim_requires_matching_core_and_observed_negative_result(
    tmp_path, corruption
):
    hashes = {"merge.py": "a" * 64, "segmentation.py": "b" * 64}
    evidence = {
        "core_sha256": dict(hashes),
        "cases": [
            {
                "name": "duplicate_manufactures_context_persistence",
                "status": "robustness_fail",
                "baseline_chunk_count": 1,
                "perturbed_chunk_count": 2,
            },
            {
                "name": "duplicates_suppress_geometric_reversal",
                "status": "robustness_fail",
                "baseline_chunk_count": 2,
                "perturbed_chunk_count": 1,
            },
            {
                "name": "overlapping_terminal_roles_suppress_direction_boundary",
                "status": "terminal_role_representation_gap",
                "baseline_chunk_count": 2,
                "perturbed_chunk_count": 1,
            },
        ],
    }
    path = tmp_path / "counterexamples.json"
    path.write_text(json.dumps(evidence))
    assert load_counterexamples(path, {"code_sha256": hashes})[
        "analysis_verified_statuses_and_core_hashes"
    ]
    if corruption == "core_hash":
        evidence["core_sha256"]["merge.py"] = "c" * 64
    else:
        evidence["cases"][0]["status"] = "not_evaluated"
    path.write_text(json.dumps(evidence))
    with pytest.raises(RuntimeError):
        load_counterexamples(path, {"code_sha256": hashes})


def test_missing_counterexample_artifact_cannot_support_a_negative_claim(tmp_path):
    assert load_counterexamples(tmp_path / "missing.json", {}) == {}
