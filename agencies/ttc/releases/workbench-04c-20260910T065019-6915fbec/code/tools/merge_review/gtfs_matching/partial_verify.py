"""Supplement an unchanged audit with fresh pins and separate code provenance.

Reuse is explicit: the expensive independent evidence checks are NOT repeated.
Every run output and upstream pin is rehashed; the original report stays intact.
Current-checkout reproduction is reported separately from stored-data integrity.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

from .partial_audit import _path, _read, _sha

VERSION = "four-mode-audit-provenance-supplement-v1"


def overview_format_proof(current_source, execution_sha256):
    """Reverse only the two documented wraps, then verify bytes AND Python AST."""
    policy = "lowest_cost_all_gates_pass_else_lowest_cost_genuine_gate_failure"
    first = "全天总览仅展示一个候选；通过门槛不等于唯一或已确认的班次。"
    second = "虚线 fallback 未过门槛。不同 reference 分面显示，APC 仅为站点时间区间。"
    substitutions = [
        (
            f'        "candidate_policy": (\n            "{policy}"\n        ),',
            f'        "candidate_policy": "{policy}",',
        ),
        (
            f'        "warning": (\n            "{first}"\n'
            f'            "{second}"\n        ),',
            f'        "warning": "{first}{second}",',
        ),
    ]
    reconstructed = current_source
    counts = []
    for wrapped, original in substitutions:
        counts.append(reconstructed.count(wrapped))
        reconstructed = reconstructed.replace(wrapped, original)
    digest = hashlib.sha256(reconstructed.encode()).hexdigest()
    try:
        equivalent = ast.dump(
            ast.parse(current_source), include_attributes=False
        ) == ast.dump(ast.parse(reconstructed), include_attributes=False)
    except SyntaxError:
        equivalent = False
    verified = counts == [1, 1] and digest == execution_sha256 and equivalent
    return {
        "mechanically_verified": verified,
        "method": (
            "reverse exactly two documented string wraps; execution-byte hash "
            "plus location-free AST equality"
        ),
        "replacement_counts": counts,
        "reconstructed_source_sha256": digest,
        "reconstructed_matches_execution": digest == execution_sha256,
        "ast_equivalent": equivalent,
        "production_file_modified": False,
    }


def verify_run(run, original_report_path, *, expected_report_sha256):
    root, report_path = Path(run).resolve(), Path(original_report_path).resolve()
    actual_report_sha = _sha(report_path)
    if actual_report_sha != expected_report_sha256:
        raise ValueError(
            "Original audit report does not match its independently recorded hash"
        )
    original = _read(report_path)
    if original.get("run_id") != root.name:
        raise ValueError("Original audit belongs to another run")
    if (
        original.get("production_partition_or_matcher_used") is not False
        or original.get("scores_recomputed") is not False
    ):
        raise ValueError("Unsupported audit provenance")
    manifest = _read(root / "manifest.json")
    source = Path(manifest["partial_source_path"]).resolve()
    if original.get("source_run_id") != source.name:
        raise ValueError("Original audit belongs to another source run")
    outputs = _read(root / "output_sha256.json")
    source_pins = manifest["partial_source_sha256"]
    mismatches, verified = [], {"new_output_hashes": 0, "source_pin_hashes": 0}
    for directory, pins, key in (
        (root, outputs, "new_output_hashes"),
        (source, source_pins, "source_pin_hashes"),
    ):
        for relative, expected in pins.items():
            path = _path(directory, relative)
            actual = _sha(path) if path.is_file() else None
            verified[key] += 1
            if actual != expected:
                mismatches.append(
                    {
                        "kind": key,
                        "path": str(path),
                        "expected_sha256": expected,
                        "actual_sha256": actual,
                    }
                )
    base_data_violations = {
        key: value
        for key, value in original.get("violations", {}).items()
        if key != "partial_code_hash_changed" and value
    }
    counts_match = original.get("examined", {}).get("new_output_hash") == len(
        outputs
    ) and original.get("examined", {}).get("source_pin_hash") == len(source_pins)
    code_mismatches = []
    for name, expected in manifest.get("partial_code_sha256", {}).items():
        path = Path(__file__).with_name(name + ".py")
        actual = _sha(path) if path.is_file() else None
        if actual == expected:
            continue
        mismatch = {
            "module": name,
            "execution_sha256": expected,
            "current_checkout_sha256": actual,
            "byte_identical": False,
        }
        if name == "overview" and path.is_file():
            mismatch["format_only_proof"] = overview_format_proof(
                path.read_text(), expected
            )
        code_mismatches.append(mismatch)
    auditor_path = Path(__file__).with_name("partial_audit.py")
    auditor_sha = _sha(auditor_path)
    auditor_unchanged = auditor_sha == original.get("audit_sha256")
    base_report_unchanged = _sha(report_path) == actual_report_sha
    data_ok = (
        not base_data_violations
        and not mismatches
        and counts_match
        and auditor_unchanged
        and base_report_unchanged
    )
    return {
        "verification_version": VERSION,
        "verification_sha256": _sha(__file__),
        "run_id": root.name,
        "source_run_id": source.name,
        "data_validation_ok": data_ok,
        "current_checkout_matches_execution": not code_mismatches,
        "current_checkout_mismatches": code_mismatches,
        "original_audit_path": str(report_path),
        "original_audit_sha256": actual_report_sha,
        "executed_auditor_sha256": original.get("audit_sha256"),
        "current_auditor_sha256": auditor_sha,
        "auditor_source_unchanged": auditor_unchanged,
        "original_audit_validation_ok": original.get("validation_ok"),
        "original_audit_violations": original.get("violations", {}),
        "base_data_violations": base_data_violations,
        "pin_mismatches": mismatches,
        "original_report_unchanged": base_report_unchanged,
        "pinned_file_counts_match_original_audit": counts_match,
        "reverified": verified,
        "output_pin_manifest_sha256": _sha(root / "output_sha256.json"),
        "run_manifest_sha256": _sha(root / "manifest.json"),
        "reuse_policy": (
            "Reuse independently audited evidence only after rechecking every "
            "pinned run/source file; no score or evidence rescan"
        ),
        "reused_evidence_checks": original.get("examined", {}),
        "full_evidence_rescanned": False,
        "matching_recomputed": False,
        "accuracy": None,
        "accuracy_assessed": False,
        "interpretation": (
            "Stored-data integrity and byte-for-byte current-checkout "
            "reproducibility are separate; original failures remain visible"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("original_report", type=Path)
    parser.add_argument("--expected-report-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve()
    if any((parent / "completion.json").is_file() for parent in target.parents):
        parser.error("Supplemental reports must be outside completed runs")
    result = verify_run(
        args.run,
        args.original_report,
        expected_report_sha256=args.expected_report_sha256,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(payload + "\n")
    print(payload)
    raise SystemExit(0 if result["data_validation_ok"] else 1)


if __name__ == "__main__":
    main()
