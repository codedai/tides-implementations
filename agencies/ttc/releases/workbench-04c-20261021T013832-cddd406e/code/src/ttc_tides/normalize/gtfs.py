"""Typed normalization of GTFS landing tables.

This is the Python equivalent of the WMATA ``stg_gtfs_*`` dbt layer: safe,
per-field casts from the all-string landing representation into canonical
types, with three deliberate differences required by this prototype's
contracts:

- identifiers always stay strings, even when they look numeric;
- rows that fail a cast or a value rule are quarantined with stable reason
  codes instead of becoming silent nulls;
- GTFS times keep the original string next to the parsed
  seconds-since-noon-minus-12h value.

The conservation invariant ``landing == normalized + quarantined`` holds for
every member and is re-checked by the quality layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import polars as pl

from ..ingest.gtfs import (
    KNOWN_MEMBERS,
    LINEAGE_COLUMNS,
    FatalGtfsError,
    GtfsArchive,
    read_member_landing,
)

# Stable quarantine reason codes -------------------------------------------------

REASON_INVALID_DATE = "GTFS_INVALID_DATE"
REASON_INVALID_TIME = "GTFS_INVALID_TIME"
REASON_INVALID_INT = "GTFS_INVALID_INT"
REASON_INVALID_FLOAT = "GTFS_INVALID_FLOAT"
REASON_MISSING_REQUIRED_VALUE = "GTFS_MISSING_REQUIRED_VALUE"
REASON_VALUE_NOT_ALLOWED = "GTFS_VALUE_NOT_ALLOWED"
REASON_DUPLICATE_PRIMARY_KEY = "GTFS_DUPLICATE_PRIMARY_KEY"

_KIND_REASON = {
    "date": REASON_INVALID_DATE,
    "time": REASON_INVALID_TIME,
    "int": REASON_INVALID_INT,
    "float": REASON_INVALID_FLOAT,
}

#: GTFS HH:MM:SS with unpadded hours and hours >= 24 allowed.
TIME_PATTERN = r"^(?<h>\d{1,3}):(?<m>[0-5][0-9]):(?<s>[0-5][0-9])$"

QUARANTINE_REASONS_COLUMN = "_quarantine_reasons"
_FIELD_INVALID_COLUMN = "__field_invalid"
_DUPLICATE_KEY_COLUMN = "__duplicate_primary_key"
_KEY_HASH_COLUMN = "__primary_key_hash"
_ROW_INDEX_COLUMN = "__normalization_row_index"

Kind = Literal["str", "int", "float", "date", "time"]


@dataclass(frozen=True)
class FieldSpec:
    """Contract for one GTFS column.

    ``required`` means the value must be present on every row and therefore
    also requires the column. ``column_required`` covers conditionally
    required GTFS fields whose header must exist even though individual rows
    may be blank. ``allowed`` validates the stripped source string against a
    closed set before casting.
    """

    name: str
    kind: Kind = "str"
    required: bool = False
    column_required: bool = False
    allowed: tuple[str, ...] | None = None


MEMBER_CONTRACTS: dict[str, tuple[FieldSpec, ...]] = {
    "agency.txt": (
        FieldSpec("agency_id"),
        FieldSpec("agency_name", required=True),
        FieldSpec("agency_url", required=True),
        FieldSpec("agency_timezone", required=True),
        FieldSpec("agency_lang"),
        FieldSpec("agency_phone"),
        FieldSpec("agency_fare_url"),
    ),
    "calendar.txt": (
        FieldSpec("service_id", required=True),
        FieldSpec("monday", "int", required=True, allowed=("0", "1")),
        FieldSpec("tuesday", "int", required=True, allowed=("0", "1")),
        FieldSpec("wednesday", "int", required=True, allowed=("0", "1")),
        FieldSpec("thursday", "int", required=True, allowed=("0", "1")),
        FieldSpec("friday", "int", required=True, allowed=("0", "1")),
        FieldSpec("saturday", "int", required=True, allowed=("0", "1")),
        FieldSpec("sunday", "int", required=True, allowed=("0", "1")),
        FieldSpec("start_date", "date", required=True),
        FieldSpec("end_date", "date", required=True),
    ),
    "calendar_dates.txt": (
        FieldSpec("service_id", required=True),
        FieldSpec("date", "date", required=True),
        FieldSpec("exception_type", "int", required=True, allowed=("1", "2")),
    ),
    "routes.txt": (
        FieldSpec("route_id", required=True),
        FieldSpec("agency_id"),
        FieldSpec("route_short_name"),
        FieldSpec("route_long_name"),
        FieldSpec("route_desc"),
        FieldSpec("route_type", "int", required=True),
        FieldSpec("route_url"),
        FieldSpec("route_color"),
        FieldSpec("route_text_color"),
    ),
    "stops.txt": (
        FieldSpec("stop_id", required=True),
        FieldSpec("stop_code"),
        FieldSpec("stop_name"),
        FieldSpec("stop_desc"),
        FieldSpec("stop_lat", "float"),
        FieldSpec("stop_lon", "float"),
        FieldSpec("zone_id"),
        FieldSpec("stop_url"),
        FieldSpec("location_type", "int"),
        FieldSpec("parent_station"),
        FieldSpec("stop_timezone"),
        FieldSpec("wheelchair_boarding", "int"),
    ),
    "trips.txt": (
        FieldSpec("route_id", required=True),
        FieldSpec("service_id", required=True),
        FieldSpec("trip_id", required=True),
        FieldSpec("trip_headsign"),
        FieldSpec("trip_short_name"),
        FieldSpec("direction_id", "int"),
        FieldSpec("block_id"),
        FieldSpec("shape_id"),
        FieldSpec("wheelchair_accessible", "int"),
        FieldSpec("bikes_allowed", "int"),
    ),
    "stop_times.txt": (
        FieldSpec("trip_id", required=True),
        FieldSpec("arrival_time", "time", column_required=True),
        FieldSpec("departure_time", "time", column_required=True),
        FieldSpec("stop_id", required=True),
        FieldSpec("stop_sequence", "int", required=True),
        FieldSpec("stop_headsign"),
        FieldSpec("pickup_type", "int"),
        FieldSpec("drop_off_type", "int"),
        FieldSpec("shape_dist_traveled", "float"),
    ),
    "shapes.txt": (
        FieldSpec("shape_id", required=True),
        FieldSpec("shape_pt_lat", "float", required=True),
        FieldSpec("shape_pt_lon", "float", required=True),
        FieldSpec("shape_pt_sequence", "int", required=True),
        FieldSpec("shape_dist_traveled", "float"),
    ),
    "feed_info.txt": (
        FieldSpec("feed_publisher_name", required=True),
        FieldSpec("feed_publisher_url", required=True),
        FieldSpec("feed_lang", required=True),
        FieldSpec("feed_start_date", "date"),
        FieldSpec("feed_end_date", "date"),
        FieldSpec("feed_version"),
        FieldSpec("feed_contact_email"),
        FieldSpec("feed_contact_url"),
    ),
}

#: Primary keys used for deterministic duplicate quarantine.
MEMBER_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "calendar.txt": ("service_id",),
    "calendar_dates.txt": ("service_id", "date"),
    "routes.txt": ("route_id",),
    "stops.txt": ("stop_id",),
    "trips.txt": ("trip_id",),
    "stop_times.txt": ("trip_id", "stop_sequence"),
    "shapes.txt": ("shape_id", "shape_pt_sequence"),
}


@dataclass
class NormalizedMember:
    """Result of normalizing one landing member."""

    member: str
    landing_rows: int
    normalized: pl.DataFrame
    quarantined: pl.DataFrame

    @property
    def reason_counts(self) -> dict[str, int]:
        if self.quarantined.is_empty():
            return {}
        counts = (
            self.quarantined.select(
                pl.col(QUARANTINE_REASONS_COLUMN).explode(empty_as_null=True)
            )
            .to_series()
            .drop_nulls()
            .value_counts()
        )
        return {
            row[QUARANTINE_REASONS_COLUMN]: row["count"]
            for row in counts.sort(QUARANTINE_REASONS_COLUMN).iter_rows(named=True)
        }

    def check_conservation(self) -> None:
        if self.landing_rows != self.normalized.height + self.quarantined.height:
            raise FatalGtfsError(
                f"{self.member}: row loss detected "
                f"({self.landing_rows} landed, {self.normalized.height} normalized, "
                f"{self.quarantined.height} quarantined)"
            )


@dataclass
class NormalizedFeed:
    """All normalized members of one GTFS archive."""

    archive: GtfsArchive
    members: dict[str, NormalizedMember] = field(default_factory=dict)

    def table(self, member: str) -> pl.DataFrame:
        if member not in self.members:
            raise FatalGtfsError(
                f"{self.archive.source_file}: member {member} was not normalized"
            )
        return self.members[member].normalized


def _clean_expr(name: str) -> pl.Expr:
    """Strip surrounding whitespace and turn whitespace-only values to null."""
    stripped = pl.col(name).str.strip_chars()
    return pl.when(stripped == "").then(None).otherwise(stripped).alias(name)


def _tmp_name(spec: FieldSpec) -> str:
    """Temporary name for the parsed value while the source string is kept."""
    return f"{spec.name}_secs" if spec.kind == "time" else f"__parsed__{spec.name}"


def _parsed_expr(spec: FieldSpec) -> list[pl.Expr]:
    src = pl.col(spec.name)
    if spec.kind == "str":
        return []
    if spec.kind == "date":
        return [src.str.to_date("%Y%m%d", strict=False).alias(_tmp_name(spec))]
    if spec.kind == "int":
        return [src.cast(pl.Int64, strict=False).alias(_tmp_name(spec))]
    if spec.kind == "float":
        return [src.cast(pl.Float64, strict=False).alias(_tmp_name(spec))]
    if spec.kind == "time":
        groups = src.str.extract_groups(TIME_PATTERN)
        seconds = (
            groups.struct.field("h").cast(pl.Int64) * 3600
            + groups.struct.field("m").cast(pl.Int64) * 60
            + groups.struct.field("s").cast(pl.Int64)
        )
        # The original string is kept beside the parsed value.
        return [seconds.alias(_tmp_name(spec))]
    raise ValueError(f"unknown field kind {spec.kind!r}")


def _reason_rules(spec: FieldSpec) -> list[tuple[pl.Expr, str]]:
    """Return boolean failure conditions and their stable reason labels."""
    src = pl.col(spec.name)
    parsed = pl.col(_tmp_name(spec))
    rules: list[tuple[pl.Expr, str]] = []
    if spec.required:
        rules.append(
            (
                src.is_null(),
                f"{REASON_MISSING_REQUIRED_VALUE}:{spec.name}",
            )
        )
    if spec.allowed is not None:
        rules.append(
            (
                src.is_not_null() & ~src.is_in(list(spec.allowed)),
                f"{REASON_VALUE_NOT_ALLOWED}:{spec.name}",
            )
        )
    if spec.kind != "str":
        parse_failed = parsed.is_null()
        if spec.kind == "float":
            parse_failed = parse_failed | ~parsed.is_finite()
        rules.append(
            (
                src.is_not_null() & parse_failed,
                f"{_KIND_REASON[spec.kind]}:{spec.name}",
            )
        )
    return rules


def _columns_needing_cleaning(
    df: pl.DataFrame, contract: tuple[FieldSpec, ...]
) -> list[str]:
    """Find source columns with leading/trailing whitespace without copying.

    Stripping every clean string column eagerly can transiently duplicate most
    of a large GTFS member. A boolean regex scan lets the common clean case
    reuse the CSV buffers; only dirty columns are materialized again.
    """
    names = [spec.name for spec in contract]
    flags = df.select(
        [
            pl.col(name).str.contains(r"^\s|\s$").fill_null(False).any().alias(name)
            for name in names
        ]
    ).row(0, named=True)
    return [name for name in names if flags[name]]


def _duplicate_key_flags(df: pl.DataFrame, primary_key: tuple[str, ...]) -> pl.Series:
    """Return an exact duplicate-key bitmap with bounded common-case state.

    Hashing the key to one UInt64 column first avoids asking Polars to retain a
    multi-column string key in a 3-million-entry duplicate table. Hash matches
    are only a prefilter: candidate keys are grouped and compared exactly, so a
    hash collision cannot quarantine distinct records.
    """
    hashes = df.select(pl.struct(list(primary_key)).hash().alias(_KEY_HASH_COLUMN))
    duplicate_hashes = (
        hashes.filter(pl.col(_KEY_HASH_COLUMN).is_duplicated())
        .select(_KEY_HASH_COLUMN)
        .unique()
    )
    flags = pl.repeat(False, df.height, dtype=pl.Boolean, eager=True).rename(
        _DUPLICATE_KEY_COLUMN
    )
    if duplicate_hashes.is_empty():
        return flags

    candidates = (
        df.select(primary_key)
        .with_row_index(_ROW_INDEX_COLUMN)
        .with_columns(hashes.to_series())
        .join(duplicate_hashes, on=_KEY_HASH_COLUMN, how="semi")
        .filter(
            pl.all_horizontal([pl.col(column).is_not_null() for column in primary_key])
        )
    )
    duplicate_keys = (
        candidates.group_by(primary_key)
        .len()
        .filter(pl.col("len") > 1)
        .select(primary_key)
    )
    if duplicate_keys.is_empty():
        return flags
    duplicate_indices = candidates.join(
        duplicate_keys,
        on=primary_key,
        how="semi",
    )[_ROW_INDEX_COLUMN]
    return flags.scatter(duplicate_indices, True)


def normalize_member(landing: pl.DataFrame, member: str) -> NormalizedMember:
    """Normalize one landing member into typed rows plus quarantined rows."""
    if member not in MEMBER_CONTRACTS:
        raise FatalGtfsError(f"no normalization contract for member {member}")
    contract = MEMBER_CONTRACTS[member]

    missing_required_columns = sorted(
        spec.name
        for spec in contract
        if (spec.required or spec.column_required) and spec.name not in landing.columns
    )
    if missing_required_columns:
        raise FatalGtfsError(
            f"{member}: required column(s) missing: {missing_required_columns}"
        )

    landing_rows = landing.height
    landing_columns = landing.columns
    df = landing.with_columns(
        [
            pl.lit(None, dtype=pl.String).alias(spec.name)
            for spec in contract
            if spec.name not in landing.columns
        ]
    )
    columns_needing_cleaning = _columns_needing_cleaning(df, contract)
    if columns_needing_cleaning:
        df = df.with_columns([_clean_expr(name) for name in columns_needing_cleaning])

    parsed_exprs = [expr for spec in contract for expr in _parsed_expr(spec)]
    reason_rules = [rule for spec in contract for rule in _reason_rules(spec)]
    field_invalid = (
        pl.any_horizontal([condition for condition, _ in reason_rules])
        if reason_rules
        else pl.lit(False)
    )
    df = df.with_columns(parsed_exprs).with_columns(
        field_invalid.alias(_FIELD_INVALID_COLUMN)
    )

    # Duplicate keys are ambiguous. Quarantine every row whose complete source
    # key occurs more than once rather than allowing source order to choose a
    # winner.
    pk = MEMBER_PRIMARY_KEYS.get(member)
    if pk:
        specs_by_name = {spec.name: spec for spec in contract}
        normalized_keys = df.select(
            [
                (
                    pl.col(column)
                    if specs_by_name[column].kind == "str"
                    else pl.col(_tmp_name(specs_by_name[column])).alias(column)
                )
                for column in pk
            ]
        )
        duplicate = _duplicate_key_flags(normalized_keys, pk)
    else:
        duplicate = pl.repeat(
            False,
            df.height,
            dtype=pl.Boolean,
            eager=True,
        ).rename(_DUPLICATE_KEY_COLUMN)
    df = df.with_columns(duplicate)

    is_clean = ~pl.col(_FIELD_INVALID_COLUMN) & ~pl.col(_DUPLICATE_KEY_COLUMN)
    # Typed value for non-str kinds, source string for str kinds; time fields
    # additionally keep their original string next to the parsed seconds.
    value_columns: list[pl.Expr] = []
    for spec in contract:
        if spec.kind == "str":
            value_columns.append(pl.col(spec.name))
        elif spec.kind == "time":
            value_columns.append(pl.col(_tmp_name(spec)))
            value_columns.append(pl.col(spec.name))
        else:
            value_columns.append(pl.col(_tmp_name(spec)).alias(spec.name))
    normalized = df.filter(is_clean).select(
        value_columns + [pl.col(c) for c in LINEAGE_COLUMNS]
    )
    quarantined_rows = df.filter(~is_clean)
    quarantine_reason_exprs = [
        pl.when(condition).then(pl.lit(reason)).otherwise(None)
        for condition, reason in reason_rules
    ]
    quarantine_reason_exprs.append(
        pl.when(pl.col(_DUPLICATE_KEY_COLUMN))
        .then(pl.lit(REASON_DUPLICATE_PRIMARY_KEY))
        .otherwise(None)
    )
    quarantined = quarantined_rows.with_columns(
        pl.concat_list(quarantine_reason_exprs)
        .list.drop_nulls()
        .alias(QUARANTINE_REASONS_COLUMN)
    ).select(landing_columns + [QUARANTINE_REASONS_COLUMN])
    result = NormalizedMember(
        member=member,
        landing_rows=landing_rows,
        normalized=normalized,
        quarantined=quarantined,
    )
    result.check_conservation()
    return result


def normalize_archive(
    archive: GtfsArchive,
    *,
    members: tuple[str, ...] | None = None,
    ingest_run_id: str = "local",
) -> NormalizedFeed:
    """Land and normalize every known member of an archive.

    Raises :class:`FatalGtfsError` when a required member is missing. When
    ``members`` is given, only that subset is processed (required-member
    validation still applies to the whole archive).
    """
    missing = archive.missing_required_members()
    if missing:
        raise FatalGtfsError(
            f"{archive.source_file}: required member(s) missing: {list(missing)}"
        )

    selected = members if members is not None else KNOWN_MEMBERS
    unsupported = sorted(set(selected).difference(KNOWN_MEMBERS))
    if unsupported:
        raise FatalGtfsError(
            f"{archive.source_file}: unsupported normalization member(s): {unsupported}"
        )
    feed = NormalizedFeed(archive=archive)
    for member in selected:
        if member not in archive.member_names:
            continue
        landing = read_member_landing(archive, member, ingest_run_id=ingest_run_id)
        feed.members[member] = normalize_member(landing, member)
    return feed
