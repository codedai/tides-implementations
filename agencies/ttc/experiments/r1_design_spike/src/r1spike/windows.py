"""Window ownership and execution-order experiment (spike §14).

Segmentation is global per vehicle; windows batch candidate/resolution
work only. Ownership is deterministic:

- a segment is owned by the window containing its first resolved
  instant; a time-unresolved segment by the window containing its
  earliest candidate instant (both are ``Segment.start_instant_s``);
- an APC record is owned by the owner window of its earliest-starting
  candidate segment; with no segment candidates, by the window
  containing its earliest valid time hypothesis; with no valid time
  hypothesis, by a deterministic source-calendar rule (the window
  containing the local midnight of its source calendar date, clamped).

Only owner windows emit decisions; the resolution engine receives the
batching as an execution schedule and must produce identical logical
outputs for any schedule — divergence is a design finding.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from .candidate_generation import ApcRecord
from .contracts import ApcCandidate
from .segmentation_baselines import Segment


@dataclass(frozen=True, slots=True)
class Window:
    index: int
    start_s: int
    end_s: int  # exclusive


def build_windows(span_start_s: int, span_end_s: int, *, hours: int) -> list[Window]:
    if hours <= 0:
        raise ValueError("window hours must be positive")
    if span_end_s < span_start_s:
        raise ValueError("window span end precedes start")
    width = hours * 3600
    windows = []
    start = span_start_s
    index = 0
    while start <= span_end_s:
        windows.append(Window(index=index, start_s=start, end_s=start + width))
        start += width
        index += 1
    return windows


def _containing(windows: list[Window], instant_s: int) -> int:
    for w in windows:
        if w.start_s <= instant_s < w.end_s:
            return w.index
    return windows[0].index if instant_s < windows[0].start_s else windows[-1].index


def segment_owner(segment: Segment, windows: list[Window]) -> int:
    return _containing(windows, segment.start_instant_s)


def apc_owner(
    record: ApcRecord,
    candidates: list[ApcCandidate],
    segments_by_id: dict[str, Segment],
    windows: list[Window],
) -> int:
    candidate_segments = sorted(
        {
            c.candidate_segment_id
            for c in candidates
            if not c.is_null_alternative() and c.candidate_segment_id in segments_by_id
        },
        key=lambda sid: (segments_by_id[sid].start_instant_s, sid),
    )
    if candidate_segments:
        return segment_owner(segments_by_id[candidate_segments[0]], windows)
    valid_instants = [e for _f, e in record.arrival_candidates]
    if valid_instants:
        return _containing(windows, min(valid_instants))
    midnight = int(
        dt.datetime.fromisoformat(record.local_date + " 00:00:00")
        .replace(tzinfo=dt.UTC)
        .timestamp()
    )
    return _containing(windows, midnight)


def window_order(n: int, order: str, *, seed: int) -> list[int]:
    indices = list(range(n))
    if order == "forward":
        return indices
    if order == "reverse":
        return list(reversed(indices))
    if order == "shuffled":
        rng = random.Random(seed)
        rng.shuffle(indices)
        return indices
    raise ValueError(f"unknown window order {order!r}")
