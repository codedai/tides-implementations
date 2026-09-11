"""Revocable anchor-and-align with EXACT optimum stability
(Spike 02R sections 15-17).

Differences from the frozen Spike-02 aligner:

- anchors are soft: every anchor segment keeps its FULL candidate set
  plus the unmatched state; deviating from the anchor's preferred key
  costs a break penalty, but nothing is hard-pinned, so "anchors and
  the DP never conflicted" can no longer be true by construction;
- candidates are COMPLETE keys (date|feed|route|trip|polarity), never
  bare trip ids;
- stability is exact: an assignment is reported stable only if
  forbidding it strictly worsens the optimum (mandatory in ALL optimal
  paths). Nothing is inferred from an enumerated subset of paths, and
  there is no enumeration cap to truncate;
- components exceeding the configured state bound fail CLOSED:
  ``optimum_analysis_incomplete`` and every assignment unresolved;
- the path itself must beat the null path (all unmatched) by an
  absolute bar and beat the second-best distinct path by a margin, or
  every non-mandatory conclusion abstains.
"""

from __future__ import annotations

from dataclasses import dataclass

from .spike02r_contracts import (
    AlignmentAssignment,
    OptimumStability,
    Reason02R,
)

_BIG = 1 << 60


@dataclass(frozen=True)
class RevocableInput:
    component_id: str
    segment_ids: tuple[str, ...]  # ordered by start time
    key_order: tuple[str, ...]  # complete keys ordered by scheduled start
    candidate_costs: dict[str, dict[str, int]]  # seg -> key -> cost
    unmatched_cost: int
    anchor_prefs: dict[str, str]  # seg -> preferred complete key (soft)
    anchor_break_penalty: int
    allow_many_to_one: bool = False
    state_bound: int = 250_000
    path_absolute_bar_q: int = 100_000
    path_margin_bar_q: int = 10_000


def _adjusted_costs(inp: RevocableInput) -> list[list[tuple[int, int]]]:
    """Per segment: [(key_index, cost_with_break_penalty)]. Unmatched is
    handled separately (also break-penalized for anchor segments)."""
    key_index = {k: i for i, k in enumerate(inp.key_order)}
    out: list[list[tuple[int, int]]] = []
    for sid in inp.segment_ids:
        pref = inp.anchor_prefs.get(sid)
        pairs = []
        for key, cost in sorted(inp.candidate_costs.get(sid, {}).items()):
            if key not in key_index:
                continue
            c = cost
            if pref is not None and key != pref:
                c += inp.anchor_break_penalty
            pairs.append((key_index[key], c))
        pairs.sort()
        out.append(pairs)
    return out


def _unmatched(inp: RevocableInput, i: int) -> int:
    sid = inp.segment_ids[i]
    extra = inp.anchor_break_penalty if sid in inp.anchor_prefs else 0
    return inp.unmatched_cost + extra


def _best_cost(
    inp: RevocableInput,
    cand: list[list[tuple[int, int]]],
    *,
    forbid: tuple[int, int | None] | None = None,
    force: tuple[int, int | None] | None = None,
) -> int:
    """Exact DP best cost. ``forbid=(i, k)`` removes choice k (None =
    unmatched) at segment i; ``force=(i, k)`` requires it."""
    n = len(inp.segment_ids)
    m = len(inp.key_order)
    f = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row = f[i]
        nxt = f[i + 1]
        for j in range(m + 1):
            best = _BIG
            allow_un = not (forbid is not None and forbid == (i, None))
            must = force[1] if (force is not None and force[0] == i) else "any"
            if allow_un and (must == "any" or must is None):
                best = _unmatched(inp, i) + nxt[j]
            for k, c in cand[i]:
                if k < j:
                    continue
                if forbid is not None and forbid == (i, k):
                    continue
                if must != "any" and must != k:
                    continue
                nj = k if inp.allow_many_to_one else k + 1
                v = c + nxt[nj]
                if v < best:
                    best = v
            row[j] = best
    return f[0][0]


def _two_best_cost(
    inp: RevocableInput, cand: list[list[tuple[int, int]]]
) -> tuple[int, int | None]:
    n = len(inp.segment_ids)
    m = len(inp.key_order)

    def add(pair, c):
        b, s = pair
        return (b + c, None if s is None else s + c)

    def merge(a, b):
        vals = [a[0], b[0]] + [x for x in (a[1], b[1]) if x is not None]
        d = sorted(set(v for v in vals if v < _BIG))
        return (d[0] if d else _BIG, d[1] if len(d) > 1 else None)

    f = [[(0, None)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m + 1):
            acc = add(f[i + 1][j], _unmatched(inp, i))
            for k, c in cand[i]:
                if k < j:
                    continue
                nj = k if inp.allow_many_to_one else k + 1
                acc = merge(acc, add(f[i + 1][nj], c))
            f[i][j] = acc
    return f[0][0]


def solve_revocable(inp: RevocableInput) -> OptimumStability:
    n = len(inp.segment_ids)
    m = len(inp.key_order)
    n_states = (n + 1) * (m + 1)
    if n_states > inp.state_bound:
        return OptimumStability(
            component_id=inp.component_id,
            n_states=n_states,
            within_bound=False,
            optimum_analysis_incomplete=True,
            best_cost=None,
            assignments=tuple(
                AlignmentAssignment(
                    segment_id=sid,
                    status="unresolved_incomplete_optimum",
                    candidate_key=None,
                    mandatory_in_all_optima=None,
                    best_without_cost_delta_q=None,
                    reason_codes=(Reason02R.OPTIMUM_INCOMPLETE,),
                )
                for sid in inp.segment_ids
            ),
            reason_codes=(Reason02R.OPTIMUM_INCOMPLETE,),
        )

    cand = _adjusted_costs(inp)
    best, second = _two_best_cost(inp, cand)
    null_cost = sum(_unmatched(inp, i) for i in range(n))
    path_quality = null_cost - best
    path_margin = None if second is None else second - best

    path_reasons: list[str] = []
    path_ok = True
    if best >= null_cost:
        path_ok = False
        path_reasons.append(Reason02R.PATH_NULL_WINS)
    elif path_quality < inp.path_absolute_bar_q:
        path_ok = False
        path_reasons.append(Reason02R.PATH_LOW_QUALITY)

    assignments: list[AlignmentAssignment] = []
    for i, sid in enumerate(inp.segment_ids):
        choices: list[tuple[int | None, int]] = [(None, 0)]
        choices += [(k, 0) for k, _c in cand[i]]
        in_some: list[int | None] = []
        for k, _z in choices:
            forced = _best_cost(inp, cand, force=(i, k))
            if forced == best:
                in_some.append(k)
        pref = inp.anchor_prefs.get(sid)
        key_of = dict(enumerate(inp.key_order))
        if len(in_some) != 1:
            status = "tied"
            assignments.append(
                AlignmentAssignment(
                    segment_id=sid,
                    status=status,
                    candidate_key=None,
                    mandatory_in_all_optima=False,
                    best_without_cost_delta_q=0,
                    break_penalty_applied=pref is not None,
                    reason_codes=(Reason02R.OPTIMUM_NOT_MANDATORY,),
                )
            )
            continue
        choice = in_some[0]
        without = _best_cost(inp, cand, forbid=(i, choice))
        delta = None if without >= _BIG else without - best
        mandatory = without > best
        chosen_key = None if choice is None else key_of[choice]
        if not mandatory:
            status = "tied"
            reasons = (Reason02R.OPTIMUM_NOT_MANDATORY,)
            chosen_key = None
        elif not path_ok:
            status = "unresolved_incomplete_optimum"
            reasons = tuple(path_reasons)
            chosen_key = None
        elif choice is None:
            if pref is not None:
                status = "anchor_revoked"
                reasons = (Reason02R.ANCHOR_REVOKED,)
            else:
                status = "unmatched"
                reasons = ()
        elif pref is not None and chosen_key == pref:
            status = "anchor_retained"
            reasons = (Reason02R.ANCHOR_RETAINED,)
        elif pref is not None:
            status = "anchor_revoked"
            reasons = (Reason02R.ANCHOR_REVOKED,)
        else:
            status = "matched"
            reasons = ()
        if (
            status in ("matched", "anchor_retained")
            and path_margin is not None
            and path_margin < inp.path_margin_bar_q
        ):
            status = "tied"
            reasons = (Reason02R.PATH_LOW_MARGIN,)
            chosen_key = None
        assignments.append(
            AlignmentAssignment(
                segment_id=sid,
                status=status,
                candidate_key=chosen_key,
                mandatory_in_all_optima=mandatory,
                best_without_cost_delta_q=delta,
                break_penalty_applied=pref is not None,
                reason_codes=reasons,
            )
        )
    return OptimumStability(
        component_id=inp.component_id,
        n_states=n_states,
        within_bound=True,
        optimum_analysis_incomplete=False,
        best_cost=best,
        assignments=tuple(assignments),
        reason_codes=tuple(path_reasons),
    )
