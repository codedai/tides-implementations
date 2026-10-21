"""Storage-only compaction across real matching, replay, and browser readers.

All artifacts are synthetic and tmp_path-owned. No historical result is opened,
rewritten, migrated, deleted or used as an accuracy label by these tests.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from tools.merge_review.gtfs_matching import adapter, config, multimode, partial, runner
from tools.merge_review.gtfs_matching.case_storage import (
    decode_case_artifact,
    encode_case_artifact,
)
from tools.merge_review.gtfs_matching.continuous.serialization import (
    scorer_fingerprint,
    validate_result,
)
from tools.merge_review.gtfs_matching.continuous.tests.test_continuous_pipeline import (
    apc,
    points,
    schedule,
)
from tools.merge_review.gtfs_matching.feed import file_sha256
from tools.merge_review.gtfs_matching.overview import build_day_overview, overview_case
from tools.merge_review.tests.test_gtfs_multimode_run import (  # noqa: F401
    snapshot,
    source_run,
)

MODES = ("global", "early", "middle", "late")
MARKER = {
    "schema": "gtfs-case-single-global-v1",
    "global_result_path": "matching_modes.global.result",
}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def raw_write(path, payload):
    """Simulate pre-codec files, including deliberately corrupt wire formats."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    if path.suffix == ".gz":
        with gzip.open(path, "wt") as stream:
            stream.write(data)
    else:
        path.write_text(data)


def raw_read(path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


@pytest.fixture
def logical_case():
    cfg = config.defaults() | {
        "continuous_matching_enabled": True,
        "matching_partial_modes_enabled": True,
    }
    trace = points(step=30)
    canonical = {
        "route_id": "route-A",
        "references": [
            {
                "reference_id": trace[0]["reference_id"],
                "family_id": "family-A",
                "direction_id": "northbound",
                "direction_label": "NORTH",
                "length_m": 3000,
                "canonical_shape_id": "synthetic-shape",
                "points": [],
            }
        ],
        "trips": [schedule(), schedule("previous", -600), schedule("next", 600)],
        "validations": [],
        "diagnostics": {},
    }
    projection = {"points": trace, "apc_evidence": [apc()]}
    modes = partial.evaluate_matching_modes(projection, canonical, cfg)
    assert set(modes) == set(MODES)
    assert all(entry["result"]["all_candidates"] for entry in modes.values())
    summary = {
        "id": "direction:synthetic:0001",
        "vehicle": "synthetic-vehicle",
        "vehicle_label": "synthetic",
        "direction": "NORTH",
        "start": trace[0]["t"],
        "end": trace[-1]["t"],
        "avl_count": len(trace),
        "apc_count": 1,
    }
    # Independent subtrees model an ordinary legacy json.load, not an in-memory
    # shortcut where both keys happen to point at the same Python dictionary.
    artifact = {
        "case_id": summary["id"],
        "config": cfg,
        "projection": projection,
        "apc_evidence": projection["apc_evidence"],
        "result": copy.deepcopy(modes["global"]["result"]),
        "matching_modes": modes,
        "parent_boundary": copy.deepcopy(summary),
        "precision_audit": {
            "uint64": 18446744073709551615,
            "float": 1.0,
            "negative_zero": -0.0,
            "boolean": True,
        },
    }
    return artifact, canonical, summary


def test_real_four_mode_writer_stores_global_once_and_reader_restores_values(
    logical_case, tmp_path
):
    artifact, _, _ = logical_case
    before, fingerprint = (
        canonical_json(artifact),
        scorer_fingerprint(artifact["config"]),
    )
    path = tmp_path / "case.json.gz"
    runner.write_case(path, artifact)
    disk = raw_read(path)
    assert disk["_case_storage"] == MARKER
    assert "result" not in disk
    assert disk["matching_modes"]["global"]["result"] == artifact["result"]
    assert canonical_json(artifact) == before
    loaded = runner.read_case(path)
    assert "_case_storage" not in loaded
    assert canonical_json(loaded) == before
    assert scorer_fingerprint(loaded["config"]) == fingerprint
    multimode.validate_modes(
        loaded["projection"],
        loaded["result"],
        loaded["matching_modes"],
        loaded["config"],
    )
    for mode in MODES:
        assert (
            validate_result(loaded["matching_modes"][mode]["result"])["status"]
            == "PASS"
        )
    assert type(loaded["precision_audit"]["uint64"]) is int
    assert type(loaded["precision_audit"]["float"]) is float
    assert json.dumps(loaded["precision_audit"]["negative_zero"]) == "-0.0"
    digest = file_sha256(path)
    with pytest.raises(FileExistsError):
        runner.write_case(path, artifact)
    assert file_sha256(path) == digest


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_browser_reads_legacy_and_compact_files_identically_in_all_modes(
    logical_case, tmp_path, suffix
):
    artifact, canonical, summary = logical_case
    original = canonical_json(artifact)
    legacy, compact = tmp_path / f"legacy{suffix}", tmp_path / f"compact{suffix}"
    raw_write(legacy, artifact)
    wire = encode_case_artifact(artifact)
    raw_write(compact, wire)
    old, new = adapter.read(legacy), adapter.read(compact)
    assert canonical_json(old) == canonical_json(new) == original
    for mode in MODES:
        assert adapter.public_gtfs(old, canonical, mode) == adapter.public_gtfs(
            new, canonical, mode
        )
        args = (summary, "synthetic-vehicle", "synthetic", canonical, mode)
        old_overview, new_overview = (
            overview_case(old, *args),
            overview_case(new, *args),
        )
        assert old_overview == new_overview
        kwargs = dict(
            run_id="same-logical-run",
            day="2023-11-06",
            matching_mode=mode,
            available_matching_modes=MODES,
        )
        assert build_day_overview(
            [old_overview], canonical, **kwargs
        ) == build_day_overview([new_overview], canonical, **kwargs)
    assert canonical_json(old) == canonical_json(new) == original
    assert canonical_json(artifact) == original
    assert "result" not in wire and wire["_case_storage"] == MARKER


def test_decoded_global_top_and_modes_preserve_independent_mutation_contract(
    logical_case,
):
    artifact, _, _ = logical_case
    wire = json.loads(canonical_json(encode_case_artifact(artifact)))
    before = canonical_json(wire)
    decoded = decode_case_artifact(wire)
    assert canonical_json(wire) == before
    assert decoded["result"] is not decoded["matching_modes"]["global"]["result"]
    assert (
        decoded["result"]["all_candidates"]
        is not decoded["matching_modes"]["global"]["result"]["all_candidates"]
    )
    saved_global = canonical_json(decoded["matching_modes"]["global"])
    saved_partials = canonical_json(
        {mode: decoded["matching_modes"][mode] for mode in MODES[1:]}
    )
    decoded["result"]["all_candidates"][0]["score"] = -999.0
    decoded["result"]["all_candidates"].append({"trip_id": "caller-only"})
    assert canonical_json(decoded["matching_modes"]["global"]) == saved_global
    assert (
        canonical_json({mode: decoded["matching_modes"][mode] for mode in MODES[1:]})
        == saved_partials
    )
    assert canonical_json(wire) == before


@pytest.mark.parametrize("with_modes", [False, True])
def test_legacy_unmarked_layout_is_readable_without_rewrite(
    logical_case, tmp_path, with_modes
):
    artifact, canonical, _ = logical_case
    if not with_modes:
        artifact.pop("matching_modes")
    path = tmp_path / "old.json.gz"
    raw_write(path, artifact)
    before = path.read_bytes()
    loaded = runner.read_case(path)
    assert canonical_json(loaded) == canonical_json(artifact)
    assert adapter.public_gtfs(loaded, canonical)["result"] == artifact["result"]
    assert path.read_bytes() == before
    assert "_case_storage" not in raw_read(path)
    if not with_modes:
        with pytest.raises(ValueError):
            adapter.public_gtfs(loaded, canonical, "early")
        target = tmp_path / "new-global-only.json.gz"
        runner.write_case(target, loaded)
        assert "result" in raw_read(target)
        assert "_case_storage" not in raw_read(target)


def test_pinned_adapter_verifies_physical_bytes_before_decoding(logical_case, tmp_path):
    artifact, _, _ = logical_case
    case_id = artifact["case_id"]
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    path = tmp_path / relative
    runner.write_case(path, artifact)
    reader = adapter.GTFSReviewData.__new__(adapter.GTFSReviewData)
    reader.run = tmp_path
    reader.manifest = {"case_artifact_suffix": ".json.gz"}
    reader._hash_cache = {}
    reader.output_hashes = {relative: file_sha256(path)}
    loaded, returned_relative = reader._case_artifact(case_id)
    assert canonical_json(loaded) == canonical_json(artifact)
    assert returned_relative == relative
    # Business-equivalent legacy bytes are NOT interchangeable under old pins.
    raw_write(path, artifact)
    with pytest.raises(ValueError, match="Pinned source or result changed"):
        reader._case_artifact(case_id)


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_schema",
        "missing_path",
        "extra_marker",
        "missing_global",
        "conflicting_top",
    ],
)
@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_browser_reader_fails_closed_on_corrupt_reserved_marker(
    logical_case, tmp_path, corruption, suffix
):
    artifact, _, _ = logical_case
    wire = json.loads(canonical_json(encode_case_artifact(artifact)))
    if corruption == "unknown_schema":
        wire["_case_storage"]["schema"] = "future-or-invalid-schema"
    elif corruption == "missing_path":
        wire["_case_storage"].pop("global_result_path")
    elif corruption == "extra_marker":
        wire["_case_storage"]["unrecognized"] = True
    elif corruption == "missing_global":
        wire["matching_modes"].pop("global")
    else:
        wire["result"] = {"status": "CONFLICTING_GLOBAL"}
    path = tmp_path / f"corrupt{suffix}"
    raw_write(path, wire)
    with pytest.raises(ValueError):
        adapter.read(path)


def test_append_only_multimode_replay_produces_compact_output_and_keeps_source(
    request, monkeypatch
):
    workspace, source, case_id = request.getfixturevalue("source_run")
    before = snapshot(source)
    monkeypatch.setattr(
        runner, "evaluate_segment", lambda *a, **k: pytest.fail("Projection rerun")
    )
    record = multimode.create_multimode_run(
        workspace, source.name, "storage integration", workers=1
    )
    target = Path(record["path"])
    assert snapshot(source) == before
    relative = f"cases/{runner.artifact_key(case_id)}.json.gz"
    disk = raw_read(target / relative)
    assert disk["_case_storage"] == MARKER and "result" not in disk
    old, new = runner.read_case(source / relative), runner.read_case(target / relative)
    for key in ("projection", "apc_evidence", "parent_boundary", "result"):
        assert canonical_json(new[key]) == canonical_json(old[key])
    for name, digest in json.loads((target / "output_sha256.json").read_text()).items():
        assert file_sha256(target / name) == digest
    assert workspace.get_run(record["run_id"]) == target
    # A second replay starts from the compact file, not a special legacy path.
    second_record = multimode.create_multimode_run(
        workspace, target.name, "second storage replay", workers=1
    )
    second = runner.read_case(Path(second_record["path"]) / relative)
    for key in ("projection", "apc_evidence", "parent_boundary", "result"):
        assert canonical_json(second[key]) == canonical_json(new[key])
    assert snapshot(source) == before


def test_storage_codec_preserves_existing_score_fingerprint_digest(logical_case):
    artifact, _, _ = logical_case
    before = hashlib.sha256(canonical_json(artifact["result"]).encode()).hexdigest()
    restored = decode_case_artifact(encode_case_artifact(artifact))
    assert (
        hashlib.sha256(canonical_json(restored["result"]).encode()).hexdigest()
        == before
    )
    assert (
        restored["result"]["scorer_fingerprint"]
        == artifact["result"]["scorer_fingerprint"]
    )
    assert scorer_fingerprint(restored["config"]) == scorer_fingerprint(
        artifact["config"]
    )
