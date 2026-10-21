"""Explicit physical GTFS instants and ambiguity-aware observation conversion.

Legacy replay keeps its declared wall-clock axis; new ingestion can use these
functions. Never interpret naive wall-clock seconds as UTC instants.
https://gtfs.org/documentation/schedule/reference/ (Time field type).
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def gtfs_instant(service_date, extended_time, agency_timezone):
    day = date.fromisoformat(str(service_date))
    hours, minutes, seconds = map(int, extended_time.split(":"))
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Invalid extended GTFS time")
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(agency_timezone))
    return (
        noon.astimezone(UTC)
        - timedelta(hours=12)
        + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    )


def observation_instants(local_datetime, agency_timezone, fold=None):
    value = (
        datetime.fromisoformat(local_datetime)
        if isinstance(local_datetime, str)
        else local_datetime
    )
    if value.tzinfo is not None:
        return {
            "status": "EVIDENCE",
            "instants": [value.astimezone(UTC)],
            "uncertainty_s": 0,
        }
    zone = ZoneInfo(agency_timezone)
    instants = set()
    for choice in (0, 1) if fold is None else (fold,):
        if choice not in (0, 1):
            raise ValueError("fold must be 0 or 1")
        instant = value.replace(tzinfo=zone, fold=choice).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == value:
            instants.add(instant)
    instants = sorted(instants)
    return {
        "status": "NONEXISTENT_LOCAL_TIME"
        if not instants
        else "AMBIGUOUS_LOCAL_TIME"
        if len(instants) > 1
        else "EVIDENCE",
        "instants": instants,
        "uncertainty_s": (instants[-1] - instants[0]).total_seconds()
        if len(instants) > 1
        else 0,
    }
