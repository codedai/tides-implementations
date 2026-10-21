"""Shared machinery for AVL/APC typed normalization (plan S1/S2).

Terminology follows the plan's §10 outcomes: rows that cannot satisfy the
structural source contract are **rejected** (outcome 2) with stage-namespaced
reason codes; plausibility findings are non-destructive **flags** reported in
the source quality report. The conservation invariant
``landing == accepted + rejected`` holds per source file.

Exact payload duplicates are annotated, never dropped (plan S1): duplicate
detection uses a 64-bit hash prefilter over the raw payload followed by an
exact match on the candidate subset, so hash collisions cannot create false
duplicate annotations.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from ..ingest.sources import SOURCE_LINEAGE_COLUMNS, FatalSourceError, SourceFile

REJECTION_REASONS_COLUMN = "_rejection_reasons"


@dataclass
class NormalizedSource:
    """Result of normalizing one AVL or APC source file."""

    source: SourceFile
    landing_rows: int
    accepted: pl.DataFrame
    rejected: pl.DataFrame
    flag_counts: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def reason_counts(self) -> dict[str, int]:
        if self.rejected.is_empty():
            return {}
        counts = (
            self.rejected.select(
                pl.col(REJECTION_REASONS_COLUMN).explode(empty_as_null=True)
            )
            .to_series()
            .drop_nulls()
            .value_counts()
        )
        return {
            row[REJECTION_REASONS_COLUMN]: row["count"]
            for row in counts.sort(REJECTION_REASONS_COLUMN).iter_rows(named=True)
        }

    def check_conservation(self) -> None:
        if self.landing_rows != self.accepted.height + self.rejected.height:
            raise FatalSourceError(
                f"{self.source.source_file}: row loss detected "
                f"({self.landing_rows} landed, {self.accepted.height} accepted, "
                f"{self.rejected.height} rejected)"
            )

    def report(self) -> dict[str, Any]:
        return {
            "source_type": self.source.source_type,
            "source_file": self.source.source_file,
            "source_sha256": self.source.sha256,
            "landing_rows": self.landing_rows,
            "accepted_rows": self.accepted.height,
            "rejected_rows": self.rejected.height,
            "rejection_reasons": self.reason_counts,
            "flag_counts": self.flag_counts,
            "metrics": self.metrics,
        }


def clean_str(name: str) -> pl.Expr:
    """Trim surrounding whitespace; whitespace-only becomes null."""
    stripped = pl.col(name).str.strip_chars()
    return pl.when(stripped == "").then(None).otherwise(stripped).alias(name)


def reason_when(condition: pl.Expr, code: str) -> pl.Expr:
    return pl.when(condition).then(pl.lit(code)).otherwise(None)


def split_by_reasons(
    df: pl.DataFrame,
    reason_exprs: list[pl.Expr],
    *,
    landing_columns: list[str],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Split rows into (accepted, rejected) by the collected reason list.

    ``rejected`` keeps the original landing columns plus the reason list;
    ``accepted`` keeps the full working frame minus the reason column.
    """
    df = df.with_columns(
        pl.concat_list(reason_exprs).list.drop_nulls().alias(REJECTION_REASONS_COLUMN)
    )
    is_clean = pl.col(REJECTION_REASONS_COLUMN).list.len() == 0
    accepted = df.filter(is_clean).drop(REJECTION_REASONS_COLUMN)
    rejected = df.filter(~is_clean).select([*landing_columns, REJECTION_REASONS_COLUMN])
    return accepted, rejected


def annotate_payload_duplicates(
    df: pl.DataFrame, payload_columns: list[str], *, flag_name: str
) -> pl.DataFrame:
    """Mark every member of an exact-payload duplicate group (nothing dropped)."""
    payload = [pl.col(c).fill_null("\x00<null>").alias(c) for c in payload_columns]
    hashed = df.with_columns(pl.struct(payload).hash().alias("__payload_hash"))
    candidate_hashes = (
        hashed.group_by("__payload_hash")
        .len()
        .filter(pl.col("len") > 1)
        .select("__payload_hash")
    )
    candidates = hashed.join(candidate_hashes, on="__payload_hash", how="semi")
    if candidates.is_empty():
        return df.with_columns(pl.lit(False).alias(flag_name))
    exact_dup_ids = (
        candidates.with_columns((pl.len().over(payload_columns) > 1).alias("__is_dup"))
        .filter(pl.col("__is_dup"))
        .select("_record_id")
    )
    return df.join(
        exact_dup_ids.with_columns(pl.lit(True).alias(flag_name)),
        on="_record_id",
        how="left",
    ).with_columns(pl.col(flag_name).fill_null(False))


def stable_vehicle_bucket(
    df: pl.DataFrame, vehicle_column: str, *, buckets: int = 16
) -> pl.DataFrame:
    """Deterministic vehicle bucket, stable across runs and library versions."""
    values = df[vehicle_column].unique().drop_nulls()
    mapping = {
        v: int(hashlib.sha256(v.encode("utf-8")).hexdigest()[:8], 16) % buckets
        for v in values.to_list()
    }
    return df.with_columns(
        pl.col(vehicle_column)
        .replace_strict(mapping, default=0, return_dtype=pl.Int32)
        .alias("vehicle_bucket")
    )


def count_flags(df: pl.DataFrame, flag_columns: list[str]) -> dict[str, int]:
    if df.is_empty():
        return dict.fromkeys(flag_columns, 0)
    row = df.select(
        [pl.col(c).cast(pl.Int64).sum().alias(c) for c in flag_columns]
    ).row(0, named=True)
    return {k: int(v or 0) for k, v in row.items()}


def lineage_columns() -> list[str]:
    return list(SOURCE_LINEAGE_COLUMNS)
