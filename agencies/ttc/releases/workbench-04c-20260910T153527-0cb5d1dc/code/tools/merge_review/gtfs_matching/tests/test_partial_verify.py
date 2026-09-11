"""Supplemental provenance cannot hide data failures or rewrite the base audit."""

import hashlib
import json
from pathlib import Path

import pytest

from tools.merge_review.gtfs_matching import multimode, partial_audit, partial_verify
from tools.merge_review.tests.test_gtfs_multimode_run import source_run  # noqa: F401


def test_format_proof_requires_exact_execution_bytes_and_identical_ast():
    current = Path(partial_verify.__file__).with_name("overview.py").read_text()
    first = partial_verify.overview_format_proof(current, "unknown")
    proof = partial_verify.overview_format_proof(
        current, first["reconstructed_source_sha256"]
    )
    assert proof["mechanically_verified"]
    assert proof["replacement_counts"] == [1, 1]
    assert proof["ast_equivalent"]
    assert not partial_verify.overview_format_proof(
        current + "\nchanged = True\n", first["reconstructed_source_sha256"]
    )["mechanically_verified"]
    assert not partial_verify.overview_format_proof("not valid python", "unknown")[
        "mechanically_verified"
    ]


def test_supplement_preserves_original_failures_and_rechecks_every_pin(
    source_run,  # noqa: F811
    tmp_path,
):
    workspace, source, _ = source_run
    target = Path(
        multimode.create_multimode_run(workspace, source.name, "supplement", workers=1)[
            "path"
        ]
    )
    base = partial_audit.audit_run(target)
    # The historical code-only failure remains visible while data checks pass.
    base.update(validation_ok=False, violations={"partial_code_hash_changed": 1})
    report_path = tmp_path / "original-audit.json"
    report_path.write_text(json.dumps(base))
    pinned = hashlib.sha256(report_path.read_bytes()).hexdigest()
    result = partial_verify.verify_run(
        target, report_path, expected_report_sha256=pinned
    )
    assert result["data_validation_ok"]
    assert result["original_audit_validation_ok"] is False
    assert result["original_audit_violations"] == {"partial_code_hash_changed": 1}
    assert not result["full_evidence_rescanned"]
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == pinned
    assert (
        result["reverified"]["new_output_hashes"] == base["examined"]["new_output_hash"]
    )
    assert (
        result["reverified"]["source_pin_hashes"] == base["examined"]["source_pin_hash"]
    )
    with (target / "observations/vehicle-test.json").open("ab") as stream:
        stream.write(b" ")
    result = partial_verify.verify_run(
        target, report_path, expected_report_sha256=pinned
    )
    assert not result["data_validation_ok"] and result["pin_mismatches"]


def test_prior_data_failure_cannot_be_reclassified_as_code_warning(
    source_run,  # noqa: F811
    tmp_path,
):
    workspace, source, _ = source_run
    target = Path(
        multimode.create_multimode_run(
            workspace, source.name, "data failure", workers=1
        )["path"]
    )
    base = partial_audit.audit_run(target)
    base.update(
        validation_ok=False,
        violations={"outside_mode_window_sources": 1, "partial_code_hash_changed": 1},
    )
    report_path = tmp_path / "original-audit.json"
    report_path.write_text(json.dumps(base))
    pinned = hashlib.sha256(report_path.read_bytes()).hexdigest()
    result = partial_verify.verify_run(
        target, report_path, expected_report_sha256=pinned
    )
    assert not result["data_validation_ok"]
    assert result["base_data_violations"] == {"outside_mode_window_sources": 1}
    with pytest.raises(ValueError, match="independently recorded hash"):
        partial_verify.verify_run(target, report_path, expected_report_sha256="wrong")
