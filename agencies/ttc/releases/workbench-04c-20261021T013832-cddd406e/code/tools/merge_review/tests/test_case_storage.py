"""Single-global transport is lossless, strict, append-only and old-reader shaped."""

import copy
import gzip
import json

import pytest

from tools.merge_review.gtfs_matching.case_storage import (
    STORAGE_DESCRIPTOR,
    STORAGE_KEY,
    decode_case_artifact,
    encode_case_artifact,
)
from tools.merge_review.gtfs_matching.runner import read_case, write_case


def artifact():
    result = {
        "status": "PATTERN_ONLY",
        "all_candidates": [
            {
                "trip_id": "synthetic-trip",
                "score": 0.25,
                "whole_alignment": {
                    "delay_process": [{"t": 10.0, "delay_s": -0.0, "rid": "obs"}]
                },
            }
        ],
    }
    return {
        "case_id": "synthetic-segment",
        "projection": {"points": [{"t": 10.0, "rid": "obs"}]},
        "result": result,
        "config": {"weight": 1.0},
        "matching_modes": {
            mode: {"result": copy.deepcopy(result), "selection": {"mode": mode}}
            for mode in ("global", "early", "middle", "late")
        },
    }


def test_exact_result_is_stored_only_at_canonical_path_without_input_mutation():
    original = artifact()
    before = copy.deepcopy(original)
    compact = encode_case_artifact(original)
    assert original == before
    assert "result" not in compact
    assert compact[STORAGE_KEY] == STORAGE_DESCRIPTOR
    assert compact["matching_modes"] == before["matching_modes"]
    assert decode_case_artifact(compact) == before
    assert STORAGE_KEY not in decode_case_artifact(compact)
    assert "result" not in compact


def test_decoder_restores_independent_legacy_result_objects():
    original = artifact()
    stored = json.loads(json.dumps(encode_case_artifact(original)))
    restored = decode_case_artifact(stored)
    restored["result"]["all_candidates"][0]["score"] = 99
    for mode in ("global", "early", "middle", "late"):
        assert (
            restored["matching_modes"][mode]["result"]["all_candidates"][0]["score"]
            == 0.25
        )
    assert stored["matching_modes"]["global"]["result"] == original["result"]


@pytest.mark.parametrize("value", [None, [], [1, 2], {}, {"metadata": "unchanged"}])
def test_generic_unmarked_metadata_passes_through(value):
    assert decode_case_artifact(value) is value


@pytest.mark.parametrize("modes", [None, {}, {"early": {"result": {}}}])
def test_legacy_global_only_artifact_stays_unmarked(modes):
    original = artifact()
    if modes is None:
        original.pop("matching_modes")
    else:
        original["matching_modes"] = modes
    stored = encode_case_artifact(original)
    assert stored == original
    assert stored is not original
    assert STORAGE_KEY not in stored


@pytest.mark.parametrize(
    "first,second", [(1, True), (1, 1.0), (0.0, -0.0), (0.25, 0.5)]
)
def test_different_json_values_cannot_be_silently_discarded(first, second):
    original = artifact()
    original["result"]["probe"] = first
    original["matching_modes"]["global"]["result"]["probe"] = second
    with pytest.raises(ValueError, match="copies differ"):
        encode_case_artifact(original)


def test_nested_dictionary_key_order_is_not_silently_changed():
    original = artifact()
    original["matching_modes"]["global"]["result"] = dict(
        reversed(list(original["result"].items()))
    )
    with pytest.raises(ValueError, match="copies differ"):
        encode_case_artifact(original)


@pytest.mark.parametrize(
    "descriptor",
    [
        None,
        "gtfs-case-single-global-v1",
        {},
        {"schema": "future-version"},
        STORAGE_DESCRIPTOR | {"global_result_path": "/tmp/other.json"},
        STORAGE_DESCRIPTOR | {"extra": "unexpected"},
    ],
)
def test_unknown_or_malformed_markers_fail_closed(descriptor):
    stored = encode_case_artifact(artifact())
    stored[STORAGE_KEY] = descriptor
    with pytest.raises(ValueError, match="descriptor"):
        decode_case_artifact(stored)


@pytest.mark.parametrize("value", [None, {}, {"status": "different"}])
def test_marked_top_level_result_is_rejected_even_when_empty(value):
    stored = encode_case_artifact(artifact())
    stored["result"] = value
    with pytest.raises(ValueError, match="top-level result"):
        decode_case_artifact(stored)


@pytest.mark.parametrize("value", [None, [], {}, {"global": {}}, {"global": None}])
def test_dangling_canonical_reference_fails_closed(value):
    stored = encode_case_artifact(artifact())
    stored["matching_modes"] = value
    with pytest.raises(ValueError, match="requires"):
        decode_case_artifact(stored)


def test_encoded_object_must_be_decoded_before_writing_again():
    with pytest.raises(ValueError, match="Decode"):
        encode_case_artifact(encode_case_artifact(artifact()))


def test_append_only_writer_preserves_logical_data_and_gzip_settings(tmp_path):
    original = artifact()
    before = copy.deepcopy(original)
    path = tmp_path / "case.json.gz"
    write_case(path, original)
    raw = gzip.decompress(path.read_bytes())
    assert json.loads(raw)[STORAGE_KEY] == STORAGE_DESCRIPTOR
    assert "result" not in json.loads(raw)
    assert read_case(path) == before == original
    assert path.read_bytes()[4:8] == bytes(4)  # Reproducible gzip mtime remains zero.
    original_bytes = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_case(path, original)
    assert path.read_bytes() == original_bytes


def test_invalid_numeric_data_does_not_publish_case(tmp_path):
    original = artifact()
    original["matching_modes"]["global"]["result"] = original["result"]
    original["result"]["score"] = float("nan")
    with pytest.raises(ValueError):
        write_case(tmp_path / "invalid.json.gz", original)
    assert not (tmp_path / "invalid.json.gz").exists()
