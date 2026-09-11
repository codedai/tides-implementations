"""Exact raw-field AVL deduplication with complete reversible source lineage.

Equality uses all thirteen parsed original CSV strings, never canonical numeric
values, payload hashes, or vehicle/time alone. Only the minimum source_row is
displayed per group; each original row remains represented in the mapping.
"""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path

import polars as pl

from tools.merge_review.paths import project_root

RAW_FIELDS = (
    "Date_Key",
    "Message_DateTime",
    "Vehicle",
    "Latitude",
    "Longitude",
    "Heading",
    "Route",
    "Run",
    "Destination",
    "OffRoute",
    "Delayed",
    "KPH",
    "tatripId",
)
RULE = "raw_avl_all_13_fields_exact_min_source_row"
MAPPING_SCHEMA = {
    "source": pl.String,
    "source_rid": pl.String,
    "representative_rid": pl.String,
    "source_row": pl.UInt64,
    "representative_source_row": pl.UInt64,
    "is_representative": pl.Boolean,
    "duplicate_group_size": pl.UInt64,
    "group_id": pl.String,
    "rule": pl.String,
}


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_raw_avl_fields(audit: Path, avl: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Read only the selected source rows, binding strings to the original CSV."""
    inventory = Path(audit) / "inventory.json"
    if not inventory.is_file():
        raise ValueError("Exact AVL deduplication requires the source inventory")
    source = json.loads(inventory.read_text()).get("sources", {}).get("avl", {})
    if not source.get("path") or not source.get("sha256"):
        raise ValueError("Exact AVL deduplication requires a verified original CSV")
    raw = Path(source["path"])
    if not raw.is_absolute():
        raw = project_root() / raw
    if not raw.is_file() or file_sha256(raw) != source["sha256"]:
        raise ValueError("Original AVL CSV missing or its SHA-256 changed")
    query = pl.scan_csv(
        raw,
        infer_schema=False,
        row_index_name="source_row",
        row_index_offset=1,
        try_parse_dates=False,
    )
    if set(query.collect_schema().names()) != {*RAW_FIELDS, "source_row"}:
        raise ValueError("Original AVL CSV must contain exactly the thirteen fields")
    keys = avl.select("source_row", pl.col("rid").alias("source_rid"), "vehicle")
    if (
        keys["source_row"].null_count()
        or keys["source_row"].n_unique() != keys.height
        or keys["source_rid"].n_unique() != keys.height
    ):
        raise ValueError("Canonical AVL source_row and rid must each be unique")
    selected = query.join(
        keys.lazy(), on="source_row", how="inner", validate="1:1"
    ).collect()
    if selected.height != avl.height:
        raise ValueError("Original AVL CSV rows do not cover the entire day fixture")
    if selected.filter(pl.col("Vehicle").str.strip_chars() != pl.col("vehicle")).height:
        raise ValueError(
            "Original CSV vehicle disagrees with canonical source-row mapping"
        )
    if file_sha256(raw) != source["sha256"]:
        raise ValueError("Original AVL CSV changed while reading raw fields")
    return selected.sort("source_row"), {
        "rule": RULE,
        "fields": list(RAW_FIELDS),
        "keep": "minimum_source_row",
        "source_row_origin": 1,
        "raw_csv_path": str(raw.resolve()),
        "raw_csv_sha256": source["sha256"],
        "comparison": (
            "Parsed original CSV field strings, without whitespace stripping, "
            "numeric conversion, or timestamp normalization; missing fields stay null."
        ),
        "hash_is_equality_rule": False,
        "apc_deduplication": False,
    }


def deduplicate_avl(avl: list[dict], raw_rows: list[dict]):
    """Return representative original records and one mapping per original AVL."""
    canonical = {r["rid"]: r for r in avl}
    raw = {r["source_rid"]: r for r in raw_rows}
    if (
        len(canonical) != len(avl)
        or len(raw) != len(raw_rows)
        or canonical.keys() != raw.keys()
    ):
        raise ValueError("Raw fields must map each original AVL rid exactly once")
    if len({r["source_row"] for r in raw_rows}) != len(raw_rows):
        raise ValueError("Original source_row must be unique")
    groups = collections.defaultdict(list)
    for row in raw_rows:
        if any(
            field not in row
            or (row[field] is not None and not isinstance(row[field], str))
            for field in RAW_FIELDS
        ):
            raise ValueError("Exact duplicate fields must be original strings or null")
        original = canonical[row["source_rid"]]
        if row["source_row"] != original["source_row"]:
            raise ValueError("Raw fields refer to a different source_row")
        groups[tuple(row[field] for field in RAW_FIELDS)].append(row)
    retained, mapping = [], []
    for members in groups.values():
        representative = min(members, key=lambda r: r["source_row"])
        representative_rid = representative["source_rid"]
        retained.append(canonical[representative_rid])
        for row in members:
            mapping.append(
                {
                    "source": "avl",
                    "source_rid": row["source_rid"],
                    "representative_rid": representative_rid,
                    "source_row": row["source_row"],
                    "representative_source_row": representative["source_row"],
                    "is_representative": row["source_rid"] == representative_rid,
                    "duplicate_group_size": len(members),
                    "group_id": "avl-exact:" + representative_rid,
                    "rule": RULE,
                }
            )
    return sorted(retained, key=lambda r: r["source_row"]), sorted(
        mapping, key=lambda r: r["source_row"]
    )


def validate_avl_deduplication(avl, raw_rows, retained, mapping):
    """Independently rebuild exact-string groups and replay every mapping row."""
    violations = collections.Counter(
        {
            "raw_source_count_difference": abs(len(avl) - len(raw_rows)),
            "mapping_count_difference": abs(len(avl) - len(mapping)),
            "duplicate_mapping_source": len(mapping)
            - len({r.get("source_rid") for r in mapping}),
            "missing_mapping_source": 0,
            "extra_mapping_source": 0,
            "duplicate_raw_source": 0,
            "raw_source_identity_mismatch": 0,
            "invalid_raw_field": 0,
            "wrong_representative": 0,
            "nonexact_group_member": 0,
            "group_size_mismatch": 0,
            "mapping_metadata_mismatch": 0,
            "retained_membership_mismatch": 0,
            "duplicate_retained_source": len(retained)
            - len({r.get("rid") for r in retained}),
            "representative_original_changed": 0,
        }
    )
    originals = {r["rid"]: r for r in avl}
    raw_by_id = {r["source_rid"]: r for r in raw_rows}
    violations["duplicate_raw_source"] = len(raw_rows) - len(raw_by_id)
    maps = {r.get("source_rid"): r for r in mapping}
    violations["missing_mapping_source"] = len(originals.keys() - maps.keys())
    violations["extra_mapping_source"] = len(maps.keys() - originals.keys())
    violations["raw_source_identity_mismatch"] = len(
        originals.keys() ^ raw_by_id.keys()
    )
    expected_groups = collections.defaultdict(list)
    for row in raw_rows:
        bad = any(
            field not in row
            or (row[field] is not None and not isinstance(row[field], str))
            for field in RAW_FIELDS
        )
        violations["invalid_raw_field"] += bad
        if not bad:
            expected_groups[tuple(row[field] for field in RAW_FIELDS)].append(row)
    representatives = set()
    for members in expected_groups.values():
        representative = sorted(members, key=lambda r: r["source_row"])[0]
        keep = representative["source_rid"]
        representatives.add(keep)
        for row in members:
            original = originals.get(row["source_rid"])
            if original is None:
                continue
            violations["raw_source_identity_mismatch"] += (
                row["source_row"] != original["source_row"]
            )
            assigned = maps.get(row["source_rid"])
            if assigned is None:
                continue
            chosen = raw_by_id.get(assigned.get("representative_rid"))
            violations["wrong_representative"] += (
                assigned.get("representative_rid") != keep
            )
            violations["nonexact_group_member"] += chosen is None or any(
                row.get(field) != chosen.get(field) for field in RAW_FIELDS
            )
            violations["group_size_mismatch"] += assigned.get(
                "duplicate_group_size"
            ) != len(members)
            violations["mapping_metadata_mismatch"] += any(
                (
                    assigned.get("source") != "avl",
                    assigned.get("rule") != RULE,
                    assigned.get("source_row") != row["source_row"],
                    assigned.get("representative_source_row")
                    != representative["source_row"],
                    assigned.get("is_representative")
                    is not (row["source_rid"] == keep),
                    assigned.get("group_id") != "avl-exact:" + keep,
                )
            )
    retained_ids = {r.get("rid") for r in retained}
    violations["retained_membership_mismatch"] = len(retained_ids ^ representatives)
    for row in retained:
        original = originals.get(row.get("rid"))
        if original is None:
            continue
        left = json.dumps(original, sort_keys=True, default=str)
        right = json.dumps(
            {k: row.get(k) for k in original}, sort_keys=True, default=str
        )
        violations["representative_original_changed"] += left != right
    return {
        "violations": dict(violations),
        "examined": {
            "original_avl": len(avl),
            "retained_avl": len(retained),
            "mapping_rows": len(mapping),
        },
    }
