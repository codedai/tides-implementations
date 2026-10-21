"""APC source landing (plan S2, provisional per ADR 0002).

The unnamed first CSV column is the source export index; it is preserved as
lineage (``source_export_id``), never as domain identity.
"""

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

SOURCE_TYPE = "apc"
ID_PREFIX = "c1"

EXPORT_INDEX_COLUMN = "source_export_id"

REQUIRED_COLUMNS = (
    "DAYTYPE",
    "Route",
    "RouteName",
    "Branch",
    "VehicleID",
    "RouteDirection",
    "StopID",
    "ONSTREET",
    "ATSTREET",
    "Lat",
    "Lon",
    "PeriodID",
    "StopArrivalTime",
    "StopDepartureTime",
    "NoTrips",
    "AvgDwell",
    "AvgOTP",
    "AvgSample",
    "Arrload",
    "Boarding",
    "Alighting",
    "Depload",
)


def inventory_apc_file(path: Path | str) -> SourceFile:
    source = inventory_source_file(path, source_type=SOURCE_TYPE, id_prefix=ID_PREFIX)
    require_columns(source, read_header_columns(source.path), REQUIRED_COLUMNS)
    return source


def scan_apc_landing(
    source: SourceFile, *, ingest_run_id: str = "local"
) -> pl.LazyFrame:
    return scan_landing(
        source,
        rename_unnamed_first=EXPORT_INDEX_COLUMN,
        ingest_run_id=ingest_run_id,
    )
