"""Diagnostic Stage-3 sequence validation for the R1 design spike.

EXPERIMENTAL. Nothing here is a production contract. This kernel exists
to *expose* design questions, not to hide them behind tie-breaking
heuristics.

Model
-----
The schedule is a sequence of stop *occurrences*, not stop ids: a trip
visiting stop A twice has two distinct occurrences (``occ_index`` 0 and
1). An observed APC visit may be MATCHED to a scheduled occurrence with
the same ``stop_id`` at an occurrence index strictly greater than the
last matched occurrence index (monotone, each occurrence used at most
once), or DEMOTED (left unmatched).

Algorithm (deterministic DP)
----------------------------
Observed visits are processed in caller-provided input order (callers
sort by ``observed_time_s`` then ``visit_id``). ``f[i][j]`` is the
minimal cost of processing observed visits ``i..n-1`` when the next
matchable occurrence index is ``>= j``:

- match cost = ``min(time_residual_cap_s, |observed - scheduled|)
  * time_residual_cost_per_s``;
- demotion cost = ``skip_cost`` (the keyword name is historical; it is
  the cost per unmatched OBSERVED visit, aliased internally as the
  demotion cost);
- scheduled occurrences never matched cost 0 — APC does not observe
  every scheduled stop and that is normal.

All arithmetic is on Python ints; no float comparison decides anything.
Complexity is ``O(n_observed * n_scheduled^2)`` for the DP plus
``O(max_optima * n_observed * n_scheduled)`` for enumeration.

All optimal alignments are enumerated by DP backtracking in a fixed
order (match candidates in increasing ``occ_index`` before the demotion
branch). That order affects only serialization and which optima survive
truncation — it NEVER manufactures a winner: equal-cost alternatives are
all enumerated, and stable-set membership is order-independent whenever
enumeration is not truncated. Enumeration stops at ``max_optima``; if
more optima exist, ``truncated`` is set, ``n_optimal == max_optima`` is
then only a lower bound, and ``OPTIMA_TRUNCATED_REASON`` is added.

Conservative tied-optima rule (the experimental rule under test)
----------------------------------------------------------------
When multiple optima exist, only attachments present in EVERY enumerated
optimum are retained (``stable_kept``); every other visit is demoted.
Visits kept in some but not all optima are ``ambiguous_demotions``
(``Reason.SEQ_AMBIGUOUS_DEMOTION``); visits kept in none are
``certain_demotions`` (``Reason.SEQ_INCONSISTENT``). Ties are never
broken by ``visit_id`` or ``occ_index`` preference.

Exposed design questions
------------------------
1. Multiple optimal alignments are surfaced as-is
   (``Reason.SEQ_MULTIPLE_OPTIMA``), including multiple equal-cost
   demotion sets that cannot be separated without an arbitrary choice.
2. ``stable_kept`` vs ``stable_matches``: a visit matched in every
   optimum but to DIFFERENT occurrences is in ``stable_kept`` but NOT in
   ``stable_matches``. It stays kept and gets no reason code here, yet
   its scheduled-time attribution is undefined downstream — whether such
   a visit should instead be demoted or flagged is deliberately left
   open by this spike.
3. Fold rollback: ``fold_rollback_visit_ids`` lists demoted visits whose
   DST fold hypothesis was selected SOLELY by the now-demoted attachment
   (``fold_selected_by_attachment``). Ambiguous demotions are included
   (conservative): the caller returns those folds to unresolved.
4. Truncation caveat: when ``truncated`` is set, the stable sets are
   computed over the enumerated subset ONLY. ``stable_kept`` can only
   shrink further with more optima (conservative for keeps), but
   ``certain_demotions`` may OVERSTATE certainty — a visit kept only in
   optima beyond the cap is misclassified as certain.
5. ``progress_violations`` (strictly decreasing ``along_m`` between
   consecutive kept visits with non-null ``along_m``) is a diagnostic
   only; it does not affect cost or demotion in this spike. This is a
   deliberate scope limit — ``along_m`` is float evidence and must not
   silently decide integer-cost outcomes.

Degenerate inputs: empty ``observed`` yields the single trivial optimum
(cost 0, everything empty). Empty ``scheduled`` demotes every observed
visit (cost ``n * skip_cost``); that IS the single optimum, so all
visits are certain demotions with ``Reason.SEQ_INCONSISTENT``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

from .contracts import Reason

# Not in contracts.Reason: local to this module by design (spike-only).
OPTIMA_TRUNCATED_REASON = "XR1.SEQ.OPTIMA_TRUNCATED"


@dataclass(frozen=True, slots=True)
class ScheduledOccurrence:
    occ_index: int  # 0-based position in the scheduled sequence
    stop_id: str
    scheduled_time_s: int  # seconds; any consistent epoch


@dataclass(frozen=True, slots=True)
class ObservedVisit:
    visit_id: str  # e.g. APC source record id
    stop_id: str
    observed_time_s: int
    along_m: float | None  # optional along-route progress evidence
    fold_selected_by_attachment: bool = False
    # True when this visit's DST fold hypothesis was selected SOLELY by
    # its attachment decision (no independent evidence)


@dataclass(frozen=True)
class Alignment:
    """One optimal alignment."""

    matches: tuple[tuple[str, int], ...]  # (visit_id, occ_index), observed order
    demoted: frozenset[str]  # visit_ids left unmatched (the demotion set)
    cost: int


@dataclass(frozen=True)
class SequenceValidationResult:
    optimal_cost: int
    n_optimal: int  # distinct optima found; lower bound when truncated
    truncated: bool  # True if enumeration hit the cap
    alignments: tuple[Alignment, ...]  # enumerated optima (bounded)
    stable_matches: tuple[tuple[str, int], ...]  # same pair in EVERY optimum
    stable_kept: frozenset[str]  # matched (to any occurrence) in EVERY optimum
    demoted: frozenset[str]  # visit_ids NOT in stable_kept (conservative rule)
    ambiguous_demotions: frozenset[str]  # kept in some optima but not all
    certain_demotions: frozenset[str]  # demoted in every optimum
    fold_rollback_visit_ids: frozenset[str]  # demoted, fold selected by attachment
    reason_codes: tuple[str, ...]
    progress_violations: tuple[tuple[str, str], ...]  # along_m decreased pairs


def validate_sequence(
    scheduled: list[ScheduledOccurrence],
    observed: list[ObservedVisit],
    *,
    skip_cost: int = 1_000,
    time_residual_cost_per_s: int = 1,
    time_residual_cap_s: int = 3_600,
    max_optima: int = 64,
) -> SequenceValidationResult:
    """Validate observed visits against the scheduled occurrence sequence.

    See the module docstring for the algorithm, the conservative
    tied-optima rule, and the exposed design questions. ``skip_cost`` is
    the cost per unmatched observed visit (demotion); skipped scheduled
    occurrences are free.
    """
    if max_optima < 1:
        raise ValueError("max_optima must be >= 1")
    if skip_cost < 0 or time_residual_cost_per_s < 0 or time_residual_cap_s < 0:
        raise ValueError("costs must be nonnegative integers")
    input_ids = [v.visit_id for v in observed]
    if len(set(input_ids)) != len(input_ids):
        raise ValueError("observed visit_id values must be unique")

    demotion_cost = skip_cost  # keyword name kept for API stability
    n = len(observed)
    m = len(scheduled)

    # Match candidates (same stop_id) and integer match costs per visit.
    cand: list[list[int]] = []
    mcost: list[dict[int, int]] = []
    for v in observed:
        ks = [k for k in range(m) if scheduled[k].stop_id == v.stop_id]
        costs: dict[int, int] = {}
        for k in ks:
            dt = abs(v.observed_time_s - scheduled[k].scheduled_time_s)
            costs[k] = min(time_residual_cap_s, dt) * time_residual_cost_per_s
        cand.append(ks)
        mcost.append(costs)

    # f[i][j]: minimal cost for observed[i:] with next occurrence >= j.
    f = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row = f[i]
        nxt = f[i + 1]
        for j in range(m + 1):
            best = demotion_cost + nxt[j]
            for k in cand[i]:
                if k >= j:
                    c = mcost[i][k] + nxt[k + 1]
                    if c < best:
                        best = c
            row[j] = best
    optimal_cost = f[0][0]

    def paths(i: int, j: int) -> Iterator[tuple[tuple[str, int], ...]]:
        """Yield every optimal match sequence for observed[i:], next occ >= j.

        Deterministic order: match candidates in increasing occ_index,
        then the demotion branch. Every branch taken satisfies the DP
        optimality equation, so each visited node lies on at least one
        optimal path — enumeration work is bounded by the number of
        optima actually consumed, never exponential.
        """
        if i == n:
            yield ()
            return
        target = f[i][j]
        vid = observed[i].visit_id
        for k in cand[i]:
            if k >= j and mcost[i][k] + f[i + 1][k + 1] == target:
                for rest in paths(i + 1, k + 1):
                    yield ((vid, k), *rest)
        if demotion_cost + f[i + 1][j] == target:
            yield from paths(i + 1, j)

    # Single pass: pull one item beyond the cap purely to detect truncation.
    enumerated = list(islice(paths(0, 0), max_optima + 1))
    truncated = len(enumerated) > max_optima
    if truncated:
        enumerated = enumerated[:max_optima]

    all_ids = frozenset(input_ids)
    alignments = tuple(
        Alignment(
            matches=match_seq,
            demoted=all_ids - {vid for vid, _ in match_seq},
            cost=optimal_cost,
        )
        for match_seq in enumerated
    )
    n_optimal = len(alignments)

    kept_sets = [frozenset(vid for vid, _ in a.matches) for a in alignments]
    stable_kept = frozenset.intersection(*kept_sets)
    union_kept = frozenset().union(*kept_sets)
    pair_sets = [set(a.matches) for a in alignments]
    stable_pairs = set.intersection(*pair_sets)
    input_order = {vid: idx for idx, vid in enumerate(input_ids)}
    stable_matches = tuple(sorted(stable_pairs, key=lambda pair: input_order[pair[0]]))

    demoted = all_ids - stable_kept
    ambiguous_demotions = union_kept - stable_kept
    certain_demotions = all_ids - union_kept
    fold_rollback_visit_ids = frozenset(
        v.visit_id
        for v in observed
        if v.visit_id in demoted and v.fold_selected_by_attachment
    )

    kept_with_progress = [
        v for v in observed if v.visit_id in stable_kept and v.along_m is not None
    ]
    progress_violations = tuple(
        (earlier.visit_id, later.visit_id)
        for earlier, later in zip(
            kept_with_progress, kept_with_progress[1:], strict=False
        )
        if later.along_m < earlier.along_m  # type: ignore[operator]
    )

    reasons: list[str] = []
    if certain_demotions:
        reasons.append(Reason.SEQ_INCONSISTENT)
    if ambiguous_demotions:
        reasons.append(Reason.SEQ_AMBIGUOUS_DEMOTION)
    if n_optimal > 1:
        reasons.append(Reason.SEQ_MULTIPLE_OPTIMA)
    if truncated:
        reasons.append(OPTIMA_TRUNCATED_REASON)

    return SequenceValidationResult(
        optimal_cost=optimal_cost,
        n_optimal=n_optimal,
        truncated=truncated,
        alignments=alignments,
        stable_matches=stable_matches,
        stable_kept=stable_kept,
        demoted=demoted,
        ambiguous_demotions=ambiguous_demotions,
        certain_demotions=certain_demotions,
        fold_rollback_visit_ids=fold_rollback_visit_ids,
        reason_codes=tuple(reasons),
        progress_violations=progress_violations,
    )
