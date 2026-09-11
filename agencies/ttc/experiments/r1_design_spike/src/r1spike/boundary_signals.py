"""APC source-level activity signals and boundary-quality tiers (Spike 02).

Semantics firewall: TTC APC count-field semantics are UNCONFIRMED (D-06
open). This module derives *diagnostic signals* from field positivity
only — raw values are preserved upstream and are never summed, rounded,
repaired, or reinterpreted here, and every derived signal carries
``XR2.SIGNAL.COUNT_SEMANTICS_UNCONFIRMED``.

The derived signals feed BOUNDARY evidence only. They are structurally
separate from identity/attachment scoring: Spike-01's score contract
firewall (``EXCLUDED_IDENTITY_FEATURES``) still bans passenger
quantities from every identity score, and nothing in this module emits a
score-contract feature.

Quality tiers (spike §6, experimental, not confirmed TTC semantics):

- Q0 unusable: invalid vehicle identity, invalid interval, missing
  time, negative counts, or a structurally invalid row.
- Q1 weak: ``AvgSample > 1``, a visit-key collision, weak/unresolved
  time or spatial evidence, or ambiguous direction/stop mapping.
- Q2 usable: exact vehicle hypothesis, valid interval, no unresolved
  collision, source fields readable.
- Q3 strong: Q2 plus terminal/turnback-cluster, direction/progress, and
  stop-occurrence context compatibility, with the source activity
  signal consistent with the episode state. Q3 is only assigned by the
  episode builder (context lives there), never here.

Q1 records may appear in diagnostics but must never create a boundary
alone; only Q3 (or an explicitly configured rule) may override a coarse
AVL terminal buffer.
"""

from __future__ import annotations

from dataclasses import replace

from .spike02_contracts import (
    ApcBoundarySignal,
    CountSignalState,
    Reason02,
    SignalQuality,
)

_QUALITY_ORDER = {
    SignalQuality.Q0_UNUSABLE: 0,
    SignalQuality.Q1_WEAK: 1,
    SignalQuality.Q2_USABLE: 2,
    SignalQuality.Q3_STRONG: 3,
}


def _state(
    boarding_positive: bool | None, alighting_positive: bool | None
) -> CountSignalState:
    if boarding_positive and alighting_positive:
        return CountSignalState.MIXED_COUNT_SIGNAL
    if boarding_positive:
        return CountSignalState.BOARDING_SIGNAL_ONLY
    if alighting_positive:
        return CountSignalState.ALIGHTING_SIGNAL_ONLY
    return CountSignalState.NO_COUNT_SIGNAL


def derive_signal(
    record_id: str,
    *,
    boarding: int | None,
    alighting: int | None,
    avg_sample: int | None,
    interval_inverted: bool,
    visit_key_collision: bool,
    negative_count: bool,
    vehicle_present: bool,
    arrival_time_present: bool,
    fold_resolved: bool,
) -> ApcBoundarySignal:
    """Derive the source-level activity signal for one APC record.

    Only positivity of the count fields is inspected. ``fold_resolved``
    is False when the record's wall time has more than one retained
    UTC hypothesis (DST fold) — that weakens, but does not invalidate,
    the signal.
    """
    reasons: list[str] = [Reason02.SIGNAL_SEMANTICS_UNCONFIRMED]
    boarding_positive = None if boarding is None else boarding > 0
    alighting_positive = None if alighting is None else alighting > 0
    state = _state(boarding_positive, alighting_positive)

    hard_reasons: list[str] = []
    if not vehicle_present:
        hard_reasons.append(Reason02.SIGNAL_VEHICLE_UNRESOLVED)
    if not arrival_time_present:
        hard_reasons.append(Reason02.SIGNAL_TIME_MISSING)
    if interval_inverted:
        hard_reasons.append(Reason02.SIGNAL_INTERVAL_INVERTED)
    if negative_count:
        hard_reasons.append(Reason02.SIGNAL_NEGATIVE_COUNT)
    if hard_reasons:
        return ApcBoundarySignal(
            record_id=record_id,
            boarding_field_positive=boarding_positive,
            alighting_field_positive=alighting_positive,
            state=state,
            avg_sample=avg_sample,
            quality=SignalQuality.Q0_UNUSABLE,
            reason_codes=tuple(reasons + hard_reasons),
        )

    weak_reasons: list[str] = []
    if avg_sample is not None and avg_sample > 1:
        weak_reasons.append(Reason02.SIGNAL_AVG_SAMPLE_MULTI)
    if visit_key_collision:
        weak_reasons.append(Reason02.SIGNAL_VISIT_KEY_COLLISION)
    if not fold_resolved:
        weak_reasons.append(Reason02.SIGNAL_TIME_UNRESOLVED_FOLD)
    quality = SignalQuality.Q1_WEAK if weak_reasons else SignalQuality.Q2_USABLE
    return ApcBoundarySignal(
        record_id=record_id,
        boarding_field_positive=boarding_positive,
        alighting_field_positive=alighting_positive,
        state=state,
        avg_sample=avg_sample,
        quality=quality,
        reason_codes=tuple(reasons + weak_reasons),
    )


def derate(
    signal: ApcBoundarySignal,
    *,
    to_quality: SignalQuality,
    reasons: tuple[str, ...],
) -> ApcBoundarySignal:
    """Lower a signal's quality with explicit reasons. Never raises
    quality: a derate to a higher tier is a programming error."""
    if _QUALITY_ORDER[to_quality] > _QUALITY_ORDER[signal.quality]:
        raise ValueError(
            f"derate cannot raise quality {signal.quality} -> {to_quality}"
        )
    if _QUALITY_ORDER[to_quality] == _QUALITY_ORDER[signal.quality]:
        return replace(
            signal, reason_codes=tuple(dict.fromkeys((*signal.reason_codes, *reasons)))
        )
    return replace(
        signal,
        quality=to_quality,
        reason_codes=tuple(dict.fromkeys((*signal.reason_codes, *reasons))),
    )


def upgrade_to_q3(
    signal: ApcBoundarySignal,
    *,
    cluster_compatible: bool,
    direction_progress_compatible: bool,
    stop_context_compatible: bool,
    state_consistent_with_episode: bool,
) -> ApcBoundarySignal:
    """Q2 -> Q3 promotion; every context check must pass explicitly.

    Only the episode builder calls this — the context (terminal cluster,
    direction/progress, stop occurrence, episode state) is unavailable
    at record level by design.
    """
    if signal.quality != SignalQuality.Q2_USABLE:
        return signal
    if not (
        cluster_compatible
        and direction_progress_compatible
        and stop_context_compatible
        and state_consistent_with_episode
    ):
        return signal
    return replace(signal, quality=SignalQuality.Q3_STRONG)
