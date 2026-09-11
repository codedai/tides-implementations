"""Rebuild single-day audit inputs from pinned CSVs, without historical runs.

Selection is only a memory-saving read filter: canonical values still come from
the original raw-audit normalizer. Source row numbers always refer to the entire
CSV, and both routes remain present until the direction/GTFS stages scope them.
"""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from experiments.apc_avl_coverage_study.raw_audit import (
    clean,
    normalize_raw,
    write_json,
    write_parquet,
)
from tools.merge_review.avl_dedup import RAW_FIELDS, file_sha256

_SOURCE_ROW = "__bootstrap_original_source_row"
_DATE = "2023-11-06"


def _day_membership(source):
    if source == "avl":
        return clean("Message_DateTime").str.to_datetime(
            "%Y-%m-%d %H:%M:%S%.f", strict=False, time_unit="us"
        )
    return pl.coalesce(
        *(
            clean(field).str.to_datetime(
                "%Y-%m-%d %H:%M:%S", strict=False, time_unit="us"
            )
            for field in ("StopArrivalTime", "StopDepartureTime")
        )
    )


def _normalize_day(path, source, digest, start, end):
    query = pl.scan_csv(
        path,
        infer_schema=False,
        try_parse_dates=False,
        row_index_name=_SOURCE_ROW,
        row_index_offset=1,
    )
    selected = query.filter(
        _day_membership(source).is_between(start, end, closed="left")
    ).collect(engine="streaming")
    # The helper column must not enter normalize_raw's business-payload hash.
    original_rows = selected[_SOURCE_ROW].cast(pl.UInt32)
    canonical = normalize_raw(selected.drop(_SOURCE_ROW), source, digest)
    canonical = canonical.with_columns(original_rows.alias("source_row"))
    canonical = canonical.with_columns(
        pl.concat_str(pl.lit(f"{source}:{digest}:"), pl.col("source_row")).alias("rid")
    )
    membership = (
        pl.col("time") if source == "avl" else pl.coalesce("arrival", "departure")
    )
    if canonical.filter(
        ~membership.is_between(start, end, closed="left").fill_null(False)
    ).height:
        raise ValueError("CSV day filter disagrees with the original normalizer")
    if canonical["rid"].n_unique() != canonical.height:
        raise ValueError("Original CSV row identity is not unique")
    return canonical


def bootstrap_raw_day(
    output_dir,
    avl_path,
    apc_path,
    date=_DATE,
    expected_hashes=None,
):
    """Create the DebugWorkspace audit contract in a new, private directory.

    ``expected_hashes`` must contain the full original ``avl`` and ``apc`` CSV
    SHA-256 values. The result is ``{path, inventory, source_counts}``. Output
    inventory.json is written last; its existence denotes successful validation.
    Failed partial output is retained for diagnosis, never silently overwritten.
    """
    if date != _DATE:
        raise ValueError(f"This workbench currently supports only {_DATE}")
    if not isinstance(expected_hashes, dict) or set(expected_hashes) != {
        "avl",
        "apc",
    }:
        raise ValueError("Pinned SHA-256 values are required for both raw sources")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in expected_hashes.values()
    ):
        raise ValueError("Raw source SHA-256 pins must be 64 lowercase hex digits")
    supplied = Path(output_dir)
    if supplied.exists() or supplied.is_symlink():
        raise ValueError("Raw bootstrap output must be a new directory")
    output = supplied.resolve()
    paths = {"avl": Path(avl_path), "apc": Path(apc_path)}
    sources = {}
    for source, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Raw {source} CSV must be an existing regular file")
        path = path.resolve()
        paths[source] = path
        if path.is_relative_to(output):
            raise ValueError("Raw CSV sources cannot be inside bootstrap output")
        digest = file_sha256(path)
        if digest != expected_hashes[source]:
            raise ValueError(f"Raw {source} CSV differs from its pinned SHA-256")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            columns = next(csv.reader(stream), [])
        if not columns or len(set(columns)) != len(columns):
            raise ValueError(f"Raw {source} CSV has absent or duplicate columns")
        if _SOURCE_ROW in columns:
            raise ValueError("Raw CSV contains the reserved bootstrap row column")
        if source == "avl" and set(columns) != set(RAW_FIELDS):
            raise ValueError("Exact AVL deduplication requires all 13 raw fields")
        if source == "apc" and "RouteName" not in columns:
            raise ValueError("Raw APC CSV requires its original RouteName column")
        sources[source] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": digest,
            "raw_columns": columns,
        }
    if paths["avl"] == paths["apc"]:
        raise ValueError("AVL and APC must use separate original CSVs")
    start = datetime.fromisoformat(date)
    end = start + timedelta(days=1)
    output.mkdir(mode=0o700, parents=True)
    counts = {}
    for source, path in paths.items():
        canonical = _normalize_day(path, source, expected_hashes[source], start, end)
        counts[source] = canonical.height
        sources[source]["selected_row_count"] = canonical.height
        sources[source]["canonical_dtypes"] = {
            name: str(dtype) for name, dtype in canonical.schema.items()
        }
        write_parquet(output / f"{source}.parquet", canonical)
    for source, path in paths.items():
        if file_sha256(path) != expected_hashes[source]:
            raise ValueError(f"Raw {source} CSV changed during bootstrap")
    inventory = {
        "audit_version": "single-day-bootstrap-v1",
        "created_utc": datetime.now(UTC).isoformat(),
        "debug_date": date,
        "scope": {"start": start.isoformat(), "end": end.isoformat()},
        "timezone_context": (
            "America/Toronto; naive wall time retained; no fold selected"
        ),
        "no_gtfs_used": True,
        "sources": sources,
        "source_counts": counts,
        "membership": (
            "AVL time and APC arrival in [00:00, next 00:00); "
            "only if parsed arrival is null use parsed departure."
        ),
        "normalization": {
            "module": "experiments.apc_avl_coverage_study.raw_audit.normalize_raw",
            "module_sha256": file_sha256(
                Path(__file__).resolve().parents[2]
                / "experiments/apc_avl_coverage_study/raw_audit.py"
            ),
            "polars_version": pl.__version__,
            "source_row_origin": 1,
            "source_row_scope": "entire original CSV, not day-filtered ordinal",
            "payload_hash_uses_original_business_columns_only": True,
        },
        "output_sha256": {
            f"{source}.parquet": file_sha256(output / f"{source}.parquet")
            for source in paths
        },
        "output_files": ["avl.parquet", "apc.parquet", "inventory.json"],
        "validation_ok": True,
        "limits": [
            "Only the declared calendar day is materialized; no adjacent-day GPS.",
            "All source routes remain present until the downstream route scope.",
            "Selected source fields and clocks are neither repaired nor deduplicated.",
            "This is normalization, not the historical full-month edge inventory.",
            "AvgOTP is retained as audit data only, never a production feature.",
        ],
    }
    write_json(output / "inventory.json", inventory)
    return {"path": str(output), "inventory": inventory, "source_counts": counts}
