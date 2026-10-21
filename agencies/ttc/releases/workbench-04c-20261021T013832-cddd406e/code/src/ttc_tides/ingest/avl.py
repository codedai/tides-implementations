"""AVL source landing (plan S1, provisional per ADR 0002)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from .sources import (
    SourceFile,
    inventory_source_file,
    read_header_columns,
    require_columns,
    scan_landing,
)

SOURCE_TYPE = "avl"
ID_PREFIX = "a1"

REQUIRED_COLUMNS = (
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


def inventory_avl_file(path: Path | str) -> SourceFile:
    source = inventory_source_file(path, source_type=SOURCE_TYPE, id_prefix=ID_PREFIX)
    require_columns(source, read_header_columns(source.path), REQUIRED_COLUMNS)
    return source


def scan_avl_landing(
    source: SourceFile, *, ingest_run_id: str = "local"
) -> pl.LazyFrame:
    return scan_landing(source, ingest_run_id=ingest_run_id)
