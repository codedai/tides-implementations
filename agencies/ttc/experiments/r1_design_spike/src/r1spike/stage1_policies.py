"""Stage 1 APC date-consistency summary policies (spike §12).

The Stage 1 summary is a known design uncertainty (R1 blocker): how may
APC evidence support a segment's service-date/feed/route selection
without double-counting, without pre-deciding attachment, and without
scale effects from record density or segment duration?

Three strategies behind one interface. No policy is selected as final by
this spike; the run report measures decision changes across policies.

Invariants enforced by construction and verified by tests:

- one APC source record contributes total mass <= 1;
- multiple fold/stop hypotheses *divide* a record's mass, never
  multiply it;
- support is normalized by contributing-record count, so doubling APC
  density or segment duration does not raise it;
- passenger values and scheduled-trip scores never enter;
- insufficient support yields ``None`` -> the caller switches to the
  AVL-only regime (never weight renormalization).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .fixedpoint import round_half_even_fraction


@dataclass(frozen=True, slots=True)
class ApcHypothesisRef:
    """One APC candidate hypothesis, reduced to Stage-1-relevant fields."""

    apc_source_record_id: str
    candidate_segment_id: str
    service_date_hypothesis: str
    feed_id: str | None


def _record_hypotheses(
    hypotheses: list[ApcHypothesisRef],
) -> dict[str, list[ApcHypothesisRef]]:
    by_record: dict[str, list[ApcHypothesisRef]] = {}
    for h in sorted(
        hypotheses,
        key=lambda h: (
            h.apc_source_record_id,
            h.candidate_segment_id,
            h.service_date_hypothesis,
            h.feed_id or "",
        ),
    ):
        by_record.setdefault(h.apc_source_record_id, []).append(h)
    return by_record


def policy_a_support(
    segment_id: str,
    date_hypothesis: str,
    feed_id: str | None,
    hypotheses: list[ApcHypothesisRef],
) -> int | None:
    """Policy A: no APC Stage-1 support; Stage 1 is AVL-only."""
    return None


def policy_b_support(
    segment_id: str,
    date_hypothesis: str,
    feed_id: str | None,
    hypotheses: list[ApcHypothesisRef],
    *,
    min_support_records: int,
) -> int | None:
    """Policy B: unshared-source-record support, millis in 0..1000.

    - a record with candidates on more than one physical segment is
      *shared* and contributes nothing;
    - an eligible record's mass 1 is divided equally over its
      hypotheses on this segment; the mass on hypotheses matching
      ``(date_hypothesis, feed_id)`` is its contribution;
    - support = sum(contributions) / n_eligible_records — record count
      normalization, no density or duration multiplier.
    """
    by_record = _record_hypotheses(hypotheses)
    contributions: list[Fraction] = []
    for _record_id, hyps in sorted(by_record.items()):
        segments = {h.candidate_segment_id for h in hyps}
        if segments != {segment_id}:
            if segment_id not in segments:
                continue
            # candidates on more than one physical segment: shared ->
            # contributes no Stage 1 support (must not pre-decide
            # attachment)
            continue
        n = len(hyps)
        matching = sum(
            1
            for h in hyps
            if h.service_date_hypothesis == date_hypothesis and h.feed_id == feed_id
        )
        contributions.append(Fraction(matching, n))
    if len(contributions) < min_support_records:
        return None
    support = sum(contributions, Fraction(0)) / len(contributions)
    return round_half_even_fraction(support * 1000)


def policy_c_support(
    segment_id: str,
    date_hypothesis: str,
    feed_id: str | None,
    hypotheses: list[ApcHypothesisRef],
    *,
    min_support_records: int,
) -> int | None:
    """Policy C: marginalized shared support (uniform prior), millis.

    Shared records participate, but a record's mass 1 is divided over
    ALL of its hypotheses across every candidate segment; only the mass
    landing on this segment with the matching date/feed counts. The
    uniform division IS the prior — sensitivity to non-uniform priors is
    exactly what this spike cannot test without calibrated attachment
    scores, and that limitation is a finding.
    """
    by_record = _record_hypotheses(hypotheses)
    contributions: list[Fraction] = []
    for _record_id, hyps in sorted(by_record.items()):
        if not any(h.candidate_segment_id == segment_id for h in hyps):
            continue
        n_total = len(hyps)
        matching = sum(
            1
            for h in hyps
            if h.candidate_segment_id == segment_id
            and h.service_date_hypothesis == date_hypothesis
            and h.feed_id == feed_id
        )
        contributions.append(Fraction(matching, n_total))
    if len(contributions) < min_support_records:
        return None
    support = sum(contributions, Fraction(0)) / len(contributions)
    return round_half_even_fraction(support * 1000)


def compute_support(
    policy: str,
    segment_id: str,
    date_hypothesis: str,
    feed_id: str | None,
    hypotheses: list[ApcHypothesisRef],
    *,
    min_support_records: int,
) -> int | None:
    if policy == "policy_a":
        return policy_a_support(segment_id, date_hypothesis, feed_id, hypotheses)
    if policy == "policy_b":
        return policy_b_support(
            segment_id,
            date_hypothesis,
            feed_id,
            hypotheses,
            min_support_records=min_support_records,
        )
    if policy == "policy_c":
        return policy_c_support(
            segment_id,
            date_hypothesis,
            feed_id,
            hypotheses,
            min_support_records=min_support_records,
        )
    raise ValueError(f"unknown stage1 policy {policy!r}")
