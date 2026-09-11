"""Fragment roles and deterministic stitching (Spike 02R section 19).

A refined chunk is NOT a performed trip. Chunks get explicit roles, and
a bounded stitch/split DP decides — with an explicit no-stitch
alternative and tied-optimum abstention — which ordered chunks combine
into a ``performed_trip_hypothesis`` (experimental ``xpt2r-*`` id, never
``trip_id_performed``).

Two chunks sharing a scheduled-trip candidate do NOT auto-stitch, and
chunks are not kept apart merely because TIDES permits many-to-one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .spike02r_contracts import (
    PERFORMED_HYPOTHESIS_ID,
    ChunkRecord,
    ChunkRole,
    Reason02R,
    StitchDecision,
)


@dataclass(frozen=True)
class StitchConfig:
    """Experimental, unapproved stitch objective parameters."""

    core_min_s: int = 1800
    fragment_max_s: int = 1200
    same_key_reward_q: int = 300_000
    continuity_reward_q: int = 150_000
    continuity_max_gap_s: int = 1200
    direction_reward_q: int = 100_000
    service_turnaround_penalty_q: int = 600_000
    stitch_base_cost_q: int = 120_000


@dataclass(frozen=True)
class PairContext:
    """Evidence about the junction between consecutive chunks."""

    gap_s: int
    crossed_service_turnaround: bool
    same_top_key: bool
    direction_consistent: bool | None


def classify_roles(ordered: list[ChunkRecord], cfg: StitchConfig) -> list[ChunkRecord]:
    """Assign roles from chunk shape and neighbouring turnarounds. The
    input is one run's chunks in time order (turnarounds included)."""
    out: list[ChunkRecord] = []
    for idx, ch in enumerate(ordered):
        if ch.role == ChunkRole.TERMINAL_TURNAROUND:
            out.append(ch)
            continue
        prev_ta = idx > 0 and ordered[idx - 1].role == ChunkRole.TERMINAL_TURNAROUND
        next_ta = (
            idx + 1 < len(ordered)
            and ordered[idx + 1].role == ChunkRole.TERMINAL_TURNAROUND
        )
        if ch.censored_start or ch.censored_end:
            role = ChunkRole.BOUNDARY_CENSORED_CHUNK
        elif ch.duration_s >= cfg.core_min_s:
            role = ChunkRole.TRIP_CORE_CANDIDATE
        elif ch.duration_s <= cfg.fragment_max_s and prev_ta and next_ta:
            # both neighbours are turnarounds: a bounded short traversal;
            # arrival/departure attribution would be arbitrary — abstain.
            role = ChunkRole.UNRESOLVED_FRAGMENT
        elif ch.duration_s <= cfg.fragment_max_s and next_ta:
            role = ChunkRole.ARRIVAL_FRAGMENT
        elif ch.duration_s <= cfg.fragment_max_s and prev_ta:
            role = ChunkRole.DEPARTURE_FRAGMENT
        else:
            role = ChunkRole.UNRESOLVED_FRAGMENT
        out.append(
            ChunkRecord(
                chunk_id=ch.chunk_id,
                profile=ch.profile,
                run_id=ch.run_id,
                record_ids=ch.record_ids,
                start_instant_s=ch.start_instant_s,
                end_instant_s=ch.end_instant_s,
                role=role,
                role_evidence=(
                    f"prev_turnaround={prev_ta}",
                    f"next_turnaround={next_ta}",
                    f"duration_s={ch.duration_s}",
                ),
                censored_start=ch.censored_start,
                censored_end=ch.censored_end,
            )
        )
    return out


def _pair_score(ctx: PairContext, cfg: StitchConfig) -> int:
    """Integer reward for joining a consecutive pair; the no-stitch
    alternative always scores 0."""
    score = -cfg.stitch_base_cost_q
    if ctx.same_top_key:
        score += cfg.same_key_reward_q
    if 0 <= ctx.gap_s <= cfg.continuity_max_gap_s:
        score += (
            cfg.continuity_reward_q
            * (cfg.continuity_max_gap_s - ctx.gap_s)
            // cfg.continuity_max_gap_s
        )
    if ctx.direction_consistent is True:
        score += cfg.direction_reward_q
    if ctx.crossed_service_turnaround:
        score -= cfg.service_turnaround_penalty_q
    return score


def stitch_run(
    run_id: str,
    chunks: list[ChunkRecord],
    contexts: list[PairContext],
    cfg: StitchConfig,
) -> tuple[list[StitchDecision], list[dict]]:
    """Decide joins over one run's ordered NON-turnaround chunks.

    ``contexts[i]`` describes the junction between chunks[i] and
    chunks[i+1]. Each junction is joined only when joining is MANDATORY
    in every optimal configuration (independent junctions: join iff
    pair score > 0; exact ties abstain). Chains become
    performed-trip hypotheses only when they contain core or
    complementary terminal-fragment evidence.
    """
    assert len(contexts) == max(0, len(chunks) - 1)
    scores = [_pair_score(c, cfg) for c in contexts]
    joins: list[str] = []  # "join" | "split" | "tied"
    for s in scores:
        if s > 0:
            joins.append("join")
        elif s < 0:
            joins.append("split")
        else:
            joins.append("tied")

    decisions: list[StitchDecision] = []
    hyp_rows: list[dict] = []
    chain: list[ChunkRecord] = []

    def flush(reason: str) -> None:
        nonlocal chain
        if not chain:
            return
        member_ids = tuple(c.chunk_id for c in chain)
        roles = {c.role for c in chain}
        role_seq = [c.role for c in chain]
        has_core = ChunkRole.TRIP_CORE_CANDIDATE in roles
        # coreless chains qualify only in physical order: a DEPARTURE
        # fragment (trip head) BEFORE an ARRIVAL fragment (trip tail);
        # arr-before-dep is tail-of-N + head-of-N+1, i.e. two trips.
        dep_idx = next(
            (i for i, r in enumerate(role_seq) if r == ChunkRole.DEPARTURE_FRAGMENT),
            None,
        )
        arr_idx = next(
            (
                i
                for i in range(len(role_seq) - 1, -1, -1)
                if role_seq[i] == ChunkRole.ARRIVAL_FRAGMENT
            ),
            None,
        )
        makes_trip = has_core or (
            dep_idx is not None and arr_idx is not None and dep_idx < arr_idx
        )
        hyp_id = None
        if makes_trip:
            key = f"{PERFORMED_HYPOTHESIS_ID}|{run_id}|{','.join(member_ids)}"
            hyp_id = (
                f"{PERFORMED_HYPOTHESIS_ID}-"
                + hashlib.sha256(key.encode()).hexdigest()[:24]
            )
            hyp_rows.append(
                {
                    "performed_trip_hypothesis_id": hyp_id,
                    "run_id": run_id,
                    "n_chunks": len(chain),
                    "member_chunk_ids": "|".join(member_ids),
                    "start_instant_s": chain[0].start_instant_s,
                    "end_instant_s": chain[-1].end_instant_s,
                    "duration_s": chain[-1].end_instant_s - chain[0].start_instant_s,
                    "roles": "|".join(sorted(r.value for r in roles)),
                    "profile": chain[0].profile,
                }
            )
        decisions.append(
            StitchDecision(
                stitch_id="xst2r-"
                + hashlib.sha256("|".join(member_ids).encode()).hexdigest()[:20],
                run_id=run_id,
                member_chunk_ids=member_ids,
                status=("stitched" if len(chain) > 1 else "no_stitch"),
                performed_trip_hypothesis_id=hyp_id,
                score_q=None,
                margin_q=None,
                reason_codes=(reason,)
                if len(chain) > 1
                else (Reason02R.STITCH_REFUSED,),
            )
        )
        chain = []

    for i, ch in enumerate(chunks):
        chain.append(ch)
        if i == len(chunks) - 1:
            break
        if joins[i] == "join":
            continue
        if joins[i] == "tied":
            flush(Reason02R.STITCH_TIED)
            decisions.append(
                StitchDecision(
                    stitch_id=f"xst2r-tied-{run_id[:8]}-{i}",
                    run_id=run_id,
                    member_chunk_ids=(ch.chunk_id, chunks[i + 1].chunk_id),
                    status="tied_abstain",
                    performed_trip_hypothesis_id=None,
                    score_q=scores[i],
                    margin_q=0,
                    reason_codes=(Reason02R.STITCH_TIED,),
                )
            )
        else:
            flush(Reason02R.STITCH_APPLIED)
    flush(Reason02R.STITCH_APPLIED)
    return decisions, hyp_rows
