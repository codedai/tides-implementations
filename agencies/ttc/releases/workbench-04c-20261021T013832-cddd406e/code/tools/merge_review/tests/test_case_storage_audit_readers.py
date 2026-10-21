"""Storage decoding must not weaken byte pins or independent audit checks."""

import copy
import gzip
import hashlib
import json

import pytest

from tools.merge_review.gtfs_matching import audit as persisted_audit
from tools.merge_review.gtfs_matching import partial_audit
from tools.merge_review.gtfs_matching.case_storage import encode_case_artifact
from tools.merge_review.gtfs_matching.tests.test_partial_audit import (
    fixture as partial_fixture,
)
from tools.merge_review.tests import test_audit_occurrence_run as occurrence_fixtures
from tools.merge_review.tests import test_gtfs_audit as persisted_fixtures
from tools.merge_review.tests.audit_occurrence_run import MODES, PinnedRun, audit


@pytest.fixture
def synthetic_runs(tmp_path):
    return occurrence_fixtures.runs.__wrapped__(tmp_path)


def logical_case():
    result = {"status": "PATTERN_ONLY", "all_candidates": [], "score": -0.0}
    return {
        "case_id": "synthetic-storage-case",
        "projection": {"points": [], "apc_evidence": []},
        "apc_evidence": [],
        "parent_boundary": {"synthetic": True},
        "config": {},
        "result": copy.deepcopy(result),
        "matching_modes": {
            mode: {"result": copy.deepcopy(result), "selection": {"mode": mode}}
            for mode in MODES
        },
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if path.name.endswith(".gz"):
        payload = gzip.compress(payload, compresslevel=1, mtime=0)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("reader", [persisted_audit._read, partial_audit._read])
@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
@pytest.mark.parametrize("compact", [False, True])
def test_audit_readers_return_same_logical_case_without_rewriting(
    tmp_path, reader, suffix, compact
):
    original = logical_case()
    stored = encode_case_artifact(original) if compact else original
    path = tmp_path / ("case" + suffix)
    digest = write_json(path, stored)
    before = path.read_bytes()

    restored = reader(path)

    assert restored == original
    assert "_case_storage" not in restored
    assert path.read_bytes() == before
    assert persisted_audit._sha(path) == digest
    assert partial_audit._sha(path) == digest
    restored["result"]["status"] = "only-the-legacy-alias-mutated"
    assert restored["matching_modes"]["global"]["result"]["status"] == "PATTERN_ONLY"


@pytest.mark.parametrize("reader", [persisted_audit._read, partial_audit._read])
@pytest.mark.parametrize("value", [{"manifest": "ordinary"}, [1, {"a": 2}], None])
def test_generic_unmarked_json_is_not_reinterpreted(tmp_path, reader, value):
    path = tmp_path / "metadata.json"
    write_json(path, value)
    assert reader(path) == value


@pytest.mark.parametrize("reader", [persisted_audit._read, partial_audit._read])
@pytest.mark.parametrize("damage", ["unknown-marker", "missing-global", "top-result"])
def test_audit_readers_reject_invalid_marked_case(tmp_path, reader, damage):
    stored = encode_case_artifact(logical_case())
    if damage == "unknown-marker":
        stored["_case_storage"]["schema"] = "unknown-storage-schema"
    elif damage == "missing-global":
        del stored["matching_modes"]["global"]
    else:
        stored["result"] = {"status": "fabricated"}
    path = tmp_path / "case.json.gz"
    write_json(path, stored)
    with pytest.raises(ValueError):
        reader(path)


def pinned_run(folder, relative, payload):
    digest = write_json(folder / relative, payload)
    write_json(folder / "output_sha256.json", {relative: digest})
    write_json(
        folder / "completion.json",
        {"validation_ok": True, "available_matching_modes": list(MODES)},
    )
    write_json(folder / "run.json", {"id": folder.name})
    return PinnedRun(folder), digest


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_occurrence_reader_hashes_stored_bytes_and_decodes_afterward(tmp_path, suffix):
    original = logical_case()
    relative = "cases/case" + suffix
    run, digest = pinned_run(tmp_path, relative, encode_case_artifact(original))
    original_bytes = (tmp_path / relative).read_bytes()

    restored = run.read_pinned(relative)

    assert restored == original
    assert run.hashes[relative] == digest == hashlib.sha256(original_bytes).hexdigest()
    assert (
        run.hashes[relative]
        != hashlib.sha256(
            json.dumps(restored, separators=(",", ":")).encode()
        ).hexdigest()
    )
    run.verify_unchanged()
    assert (tmp_path / relative).read_bytes() == original_bytes


@pytest.mark.parametrize("replacement", ["legacy-same-logical-case", "gzip-header"])
def test_same_logical_result_does_not_make_altered_file_pin_valid(
    tmp_path, replacement
):
    original = logical_case()
    relative = "cases/case.json.gz"
    run, _ = pinned_run(tmp_path, relative, encode_case_artifact(original))
    path = tmp_path / relative
    if replacement == "legacy-same-logical-case":
        write_json(path, original)
    else:
        payload = gzip.decompress(path.read_bytes())
        path.write_bytes(gzip.compress(payload, compresslevel=1, mtime=1))
    with pytest.raises(ValueError, match="Pinned artifact changed"):
        run.read_pinned(relative)


def test_occurrence_pin_is_checked_before_storage_marker_decoding(tmp_path):
    relative = "cases/case.json.gz"
    run, _ = pinned_run(tmp_path, relative, encode_case_artifact(logical_case()))
    write_json(tmp_path / relative, {"_case_storage": {"schema": "bad"}})
    with pytest.raises(ValueError, match="Pinned artifact changed"):
        run.read_pinned(relative)


def test_partial_audit_checks_same_evidence_after_storage_decode(tmp_path):
    original, _, config = partial_fixture()
    path = tmp_path / "case.json.gz"
    write_json(path, encode_case_artifact(original))
    baseline, compact = partial_audit.Report(), partial_audit.Report()
    partial_audit.audit_modes(original, config, baseline, "case")
    partial_audit.audit_modes(partial_audit._read(path), config, compact, "case")
    assert compact.output() == baseline.output()
    assert compact.output()["validation_ok"]


def compact_synthetic_run(folder):
    path = folder / "cases/synthetic.json.gz"
    logical = json.loads(gzip.decompress(path.read_bytes()))
    pins = json.loads((folder / "output_sha256.json").read_text())
    pins["cases/synthetic.json.gz"] = write_json(path, encode_case_artifact(logical))
    write_json(folder / "output_sha256.json", pins)


@pytest.mark.parametrize("compact_source", [False, True])
def test_full_occurrence_audit_accepts_mixed_or_both_compact_synthetic_runs(
    synthetic_runs, compact_source
):
    source, target = synthetic_runs
    if compact_source:
        compact_synthetic_run(source)
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["partial_source_sha256"] = {
            relative: hashlib.sha256((source / relative).read_bytes()).hexdigest()
            for relative in manifest["partial_source_sha256"]
        }
        pins = json.loads((target / "output_sha256.json").read_text())
        pins["manifest.json"] = write_json(manifest_path, manifest)
        write_json(target / "output_sha256.json", pins)
    compact_synthetic_run(target)

    report = audit(source, target)

    assert report["status"] == "PASS"
    assert report["counts"]["mode_results_audited"] == 4
    assert report["source_clocks_unchanged"]
    assert not report["accuracy_claimed"]


@pytest.mark.parametrize(
    "variant,accepted",
    [
        ("legacy", True),
        ("storage", True),
        ("missing-storage-codec", False),
        ("unexpected-module", False),
        ("malformed-digest", False),
        ("empty", False),
    ],
)
def test_replay_provenance_accepts_exact_legacy_or_storage_inventory(
    tmp_path, variant, accepted
):
    _, run, _ = persisted_fixtures.replayed(tmp_path)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    code = manifest["decision_code_sha256"]
    if variant != "legacy":
        code.update({"runner.py": "4" * 64, "case_storage.py": "5" * 64})
    if variant == "missing-storage-codec":
        del code["case_storage.py"]
    elif variant == "unexpected-module":
        code["unexpected.py"] = "6" * 64
    elif variant == "malformed-digest":
        code["case_storage.py"] = "not-a-sha256"
    elif variant == "empty":
        code.clear()
    write_json(path, manifest)
    persisted_fixtures._repin(run, "manifest.json")

    result = persisted_audit.audit_run(run)

    assert not result["violations"]
    assert result["validation_ok"] is accepted
    assert bool(result["schema_warnings"].get("unknown_decision_code_provenance")) is (
        not accepted
    )
