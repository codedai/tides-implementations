"""Exact raw-string AVL equality, representative choice and complete lineage."""

import copy
import json
from datetime import datetime

import polars as pl
import pytest

from tools.merge_review import avl_dedup


def raw(rid, number, **changes):
    fields = dict.fromkeys(avl_dedup.RAW_FIELDS, "0")
    fields.update(
        Date_Key="20231106",
        Message_DateTime="2023-11-06 12:00:00",
        Vehicle="9027",
        Latitude="43.6500",
        Longitude="-79.4",
        Route="29",
    )
    return (
        fields | {"source_rid": rid, "source_row": number, "vehicle": "9027"} | changes
    )


def canonical(rows):
    return [
        {
            "rid": r["source_rid"],
            "source_row": r["source_row"],
            "vehicle": "9027",
            "time": datetime(2023, 11, 6, 12),
            "lat": float(r["Latitude"]),
            "lon": -79.4,
            "payload_hash": 123,
        }
        for r in rows
    ]


def test_every_original_maps_once_and_minimum_source_row_is_kept():
    rows = [raw("a-later", 30), raw("z-earliest", 10), raw("m-middle", 20)]
    source = canonical(rows)
    before = copy.deepcopy(source)
    kept, mapping = avl_dedup.deduplicate_avl(source, rows)
    assert [r["rid"] for r in kept] == ["z-earliest"]
    assert {r["representative_rid"] for r in mapping} == {"z-earliest"}
    assert {r["duplicate_group_size"] for r in mapping} == {3}
    assert sum(r["is_representative"] for r in mapping) == 1
    assert source == before
    assert not any(
        avl_dedup.validate_avl_deduplication(source, rows, kept, mapping)[
            "violations"
        ].values()
    )
    assert avl_dedup.deduplicate_avl(source[::-1], rows[::-1]) == (kept, mapping)


@pytest.mark.parametrize("field", avl_dedup.RAW_FIELDS)
def test_any_of_thirteen_raw_fields_can_prevent_deduplication(field):
    rows = [raw("one", 1), raw("two", 2)]
    rows[1][field] = rows[1][field] + " "
    kept, mapping = avl_dedup.deduplicate_avl(canonical(rows), rows)
    assert len(kept) == 2
    assert all(
        r["is_representative"] and r["duplicate_group_size"] == 1 for r in mapping
    )


def test_numeric_equivalence_and_hash_collision_are_not_exact_equality():
    rows = [raw("one", 1), raw("two", 2, Latitude="43.65")]
    source = canonical(rows)
    assert source[0]["lat"] == source[1]["lat"]
    assert source[0]["payload_hash"] == source[1]["payload_hash"]
    kept, _ = avl_dedup.deduplicate_avl(source, rows)
    assert len(kept) == 2


def test_null_and_empty_original_strings_are_not_collapsed():
    rows = [raw("one", 1, Destination=None), raw("two", 2, Destination="")]
    kept, _ = avl_dedup.deduplicate_avl(canonical(rows), rows)
    assert len(kept) == 2


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("representative_rid", "second", "wrong_representative"),
        ("representative_source_row", 20, "mapping_metadata_mismatch"),
        ("duplicate_group_size", 1, "group_size_mismatch"),
        ("is_representative", True, "mapping_metadata_mismatch"),
        ("source_row", 123, "mapping_metadata_mismatch"),
        ("rule", "vehicle_time_only", "mapping_metadata_mismatch"),
    ],
)
def test_independent_validation_rejects_mapping_corruption(field, value, rule):
    rows = [raw("first", 1), raw("second", 2)]
    source = canonical(rows)
    kept, mapping = avl_dedup.deduplicate_avl(source, rows)
    mapping[1][field] = value
    assert (
        avl_dedup.validate_avl_deduplication(source, rows, kept, mapping)["violations"][
            rule
        ]
        > 0
    )


def test_independent_validation_rejects_nonexact_member_and_missing_source():
    rows = [raw("first", 1), raw("second", 2, Route="929")]
    source = canonical(rows)
    kept, mapping = avl_dedup.deduplicate_avl(source, rows)
    mapping[1]["representative_rid"] = "first"
    check = avl_dedup.validate_avl_deduplication(source, rows, kept[:1], mapping)
    assert check["violations"]["nonexact_group_member"] == 1
    assert check["violations"]["retained_membership_mismatch"] == 1
    assert (
        avl_dedup.validate_avl_deduplication(source, rows, kept, mapping[:1])[
            "violations"
        ]["missing_mapping_source"]
        == 1
    )


def test_load_original_csv_by_source_row_and_hash(tmp_path):
    rows = [raw("first", 1), raw("second", 2, Latitude="43.65")]
    source = canonical(rows)
    csv = tmp_path / "avl.csv"
    pl.from_dicts(rows).select(avl_dedup.RAW_FIELDS).write_csv(csv)
    (tmp_path / "inventory.json").write_text(
        json.dumps(
            {
                "sources": {
                    "avl": {"path": str(csv), "sha256": avl_dedup.file_sha256(csv)}
                }
            }
        )
    )
    selected, metadata = avl_dedup.load_raw_avl_fields(tmp_path, pl.from_dicts(source))
    assert selected["Latitude"].to_list() == ["43.6500", "43.65"]
    assert metadata["fields"] == list(avl_dedup.RAW_FIELDS)
    assert not metadata["hash_is_equality_rule"]
    csv.write_text("changed\n")
    with pytest.raises(ValueError, match="SHA-256"):
        avl_dedup.load_raw_avl_fields(tmp_path, pl.from_dicts(source))


def test_empty_avl_group_has_empty_but_valid_mapping():
    assert avl_dedup.deduplicate_avl([], []) == ([], [])
    assert not any(
        avl_dedup.validate_avl_deduplication([], [], [], [])["violations"].values()
    )
