"""Pure parsing helpers: no I/O, no streamlit.

Vendored verbatim from the v1 UI (``tools/labeling_ui/labeling/parsing.py``)
for the v2 project; v1 stays frozen because the audit hashes it.

Conventions established by the r1-spike02r artifacts:

- Instants (``*_instant_s``, ``case_epoch_s``) are epoch seconds obtained by
  interpreting naive wall-clock timestamps as America/Toronto local time
  (the spike's "naive America/Toronto wall-time interpretation", D-16 open).
- Candidate keys are ``service_date|feed_id|route_id|trip_id|polarity``
  (polarity may be empty).
- Case summaries look like ``"chunk of 2312s, anchor status low_margin"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Toronto")

_DURATION_RE = re.compile(r"chunk of (\d+)\s*s")
_ANCHOR_STATUS_RE = re.compile(r"anchor status\s+(\w+)")


def parse_chunk_duration_s(summary: str | None) -> int | None:
    """Extract the chunk duration from a case summary, if present."""
    if not summary:
        return None
    m = _DURATION_RE.search(summary)
    return int(m.group(1)) if m else None


def parse_anchor_status(summary: str | None) -> str | None:
    if not summary:
        return None
    m = _ANCHOR_STATUS_RE.search(summary)
    return m.group(1) if m else None


def redact_summary(summary: str | None) -> str | None:
    """Mask model-verdict words so the neutral view does not anchor the
    reviewer (the raw summary stays available under diagnostics)."""
    if summary is None:
        return None
    return _ANCHOR_STATUS_RE.sub("anchor status ███ (hidden)", summary)


@dataclass(frozen=True)
class CandidateKey:
    raw: str
    service_date: date | None
    feed_id: str | None
    route_id: str | None
    trip_id: str | None
    polarity: str | None


def parse_candidate_key(key: str | None) -> CandidateKey | None:
    """Parse ``service_date|feed_id|route_id|trip_id|polarity``.

    Tolerates missing trailing fields and unparseable dates; returns None
    only for empty input.
    """
    if not key:
        return None
    parts = key.split("|")
    parts += [""] * (5 - len(parts))
    d: date | None = None
    try:
        d = date.fromisoformat(parts[0])
    except ValueError:
        pass
    return CandidateKey(
        raw=key,
        service_date=d,
        feed_id=parts[1] or None,
        route_id=parts[2] or None,
        trip_id=parts[3] or None,
        polarity=parts[4] or None,
    )


def epoch_to_local(epoch_s: int | float | None) -> datetime | None:
    """Epoch seconds -> aware America/Toronto datetime."""
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, LOCAL_TZ)


def local_naive_to_epoch(dt: datetime) -> int:
    """Naive wall-clock (assumed America/Toronto) -> epoch seconds.

    Matches the spike's convention. On DST fall-back the earlier (fold=0)
    instant is used; ambiguity is inherent to the source data (D-16).
    """
    if dt.tzinfo is not None:
        return int(dt.timestamp())
    return int(dt.replace(tzinfo=LOCAL_TZ, fold=0).timestamp())


def local_date_of_epoch(epoch_s: int | float | None) -> date | None:
    dt = epoch_to_local(epoch_s)
    return dt.date() if dt else None


def fmt_local(epoch_s: int | float | None, with_date: bool = True) -> str:
    """Human-readable local time for headers and plots."""
    dt = epoch_to_local(epoch_s)
    if dt is None:
        return "n/a"
    return dt.strftime("%Y-%m-%d %H:%M:%S") if with_date else dt.strftime("%H:%M:%S")


def fmt_duration_s(seconds: int | float | None) -> str:
    if seconds is None:
        return "n/a"
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 60:
        return f"{sign}{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{sign}{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{sign}{h}h{m:02d}m"


def dates_spanned(start_epoch_s: int, end_epoch_s: int) -> list[date]:
    """Local calendar dates touched by [start, end] (inclusive), padded by
    nothing — callers add their own padding before calling if needed."""
    d0 = local_date_of_epoch(start_epoch_s)
    d1 = local_date_of_epoch(end_epoch_s)
    if d0 is None or d1 is None:
        return []
    out = []
    d = d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def duration_bucket(duration_s: int | None) -> str:
    """Coarse buckets used by the sidebar filter."""
    if duration_s is None:
        return "unknown"
    if duration_s < 600:
        return "< 10 min"
    if duration_s < 1800:
        return "10–30 min"
    if duration_s < 3600:
        return "30–60 min"
    return "> 60 min"
