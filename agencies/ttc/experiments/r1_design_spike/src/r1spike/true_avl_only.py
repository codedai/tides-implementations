"""V1R: a genuinely APC-free AVL-only comparator (Spike 02R section 6),
plus the profile-separated chunk builders for V2R variants.

Structural guarantees for V1R:

- the entry point accepts NO APC object of any kind — there is no
  parameter through which normalized APC, APC signals, APC alignment
  edges, APC episodes, or APC-derived artifacts can reach it;
- terminal clusters passed in must be GTFS-only (no experimental
  APC-observed stop-code hypotheses; asserted);
- episodes are detected with an empty edge set and an empty signal map,
  so every APC-dependent evidence family and service anchor is
  structurally impossible;
- boundary candidates therefore carry empty dependency-record sets,
  which the provenance layer asserts.

The metamorphic requirement (remove/permute/perturb APC leaves V1R
logically identical) holds by construction and is verified by test.

Profile identities are deliberately distinct and non-interchangeable:
``xsg-avl-*`` vs ``xsg-apc-*`` (spec section 7); a crosswalk artifact
records lineage between them.
"""

from __future__ import annotations

import hashlib

from .boundary_v2 import (
    BoundaryV2Config,
    applied_cuts,
    candidates_from_episodes,
    decide_boundaries_v2,
)
from .provisional_runs import ordered_run_pings
from .segmentation_baselines import SegPing
from .spike02_contracts import (
    AlignmentEdge,
    ApcBoundarySignal,
    TerminalCluster,
    VehicleActivityRun,
)
from .spike02r_contracts import (
    ALGORITHM_VERSION02R,
    CHUNK_ID_APC,
    CHUNK_ID_AVL,
    ChunkRecord,
    ChunkRole,
)
from .terminal_episodes import EpisodeConfig, detect_episodes

PROFILE_AVL = "avl"
PROFILE_APC_STRUCTURAL = "apc-structural"
PROFILE_APC_ACTIVITY = "apc-activity"


def _chunk_id(profile: str, run_id: str, record_ids: tuple[str, ...]) -> str:
    ns = CHUNK_ID_AVL if profile == PROFILE_AVL else CHUNK_ID_APC
    key = f"{ns}|{ALGORITHM_VERSION02R}|{profile}|{run_id}|{','.join(record_ids)}"
    return f"{ns}-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def assert_gtfs_only_clusters(clusters: list[TerminalCluster]) -> None:
    for c in clusters:
        if "apc_observed_stop_code_hypothesis" in c.evidence_sources:
            raise AssertionError(
                "V1R received a cluster carrying an APC-observed stop-code "
                f"hypothesis: {c.cluster_id}"
            )


def chunks_from_cuts(
    profile: str,
    run: VehicleActivityRun,
    pings: list[SegPing],
    cuts,
) -> list[ChunkRecord]:
    """Partition the run's pings at the applied cut intervals. Pings
    inside a cut interval become terminal_turnaround chunks; everything
    else becomes ordered chunks (roles refined later)."""
    out: list[ChunkRecord] = []
    intervals = [(c.candidate_start_s, c.candidate_end_s) for c in cuts]

    def owner(instant: int) -> int | None:
        for k, (a, b) in enumerate(intervals):
            if a <= instant < b or (a == b and instant == a):
                return k
        return None

    # Cuts that own no ping (zero-width, or a nonzero interval falling
    # entirely between two pings) still split between those pings.
    owned = set()
    for p_ in pings:
        k = owner(p_.ordering_instant)
        if k is not None:
            owned.add(k)
    zero_cuts = sorted(
        a for idx, (a, b) in enumerate(intervals) if a == b or idx not in owned
    )
    current: list[SegPing] = []
    current_kind: int | None = None  # None=segment, k=turnaround index

    def flush(kind: int | None) -> None:
        nonlocal current
        if not current:
            return
        rids = tuple(p.record_id for p in current)
        role = (
            ChunkRole.TERMINAL_TURNAROUND
            if kind is not None
            else ChunkRole.UNRESOLVED_FRAGMENT
        )
        out.append(
            ChunkRecord(
                chunk_id=_chunk_id(profile, run.run_id, rids),
                profile=profile,
                run_id=run.run_id,
                record_ids=rids,
                start_instant_s=current[0].ordering_instant,
                end_instant_s=current[-1].ordering_instant,
                role=role,
            )
        )
        current = []

    prev_instant: int | None = None
    for p in pings:
        t = p.ordering_instant
        if prev_instant is not None and any(prev_instant < z <= t for z in zero_cuts):
            flush(current_kind)
            current_kind = None
        k = owner(t)
        if k != current_kind:
            flush(current_kind)
            current_kind = k
        current.append(p)
        prev_instant = t
    flush(current_kind)
    return out


def build_profile_chunks(
    runs: list[VehicleActivityRun],
    pings_by_id: dict[str, SegPing],
    clusters: list[TerminalCluster],
    *,
    profile: str,
    episode_cfg: EpisodeConfig,
    boundary_cfg: BoundaryV2Config,
    along_by_id: dict[str, float],
    edges: list[AlignmentEdge] | None = None,
    signals_by_id: dict[str, ApcBoundarySignal] | None = None,
) -> tuple[list[ChunkRecord], list, list, dict[str, list]]:
    """Boundary-v2 chunks for one profile.

    For ``profile == "avl"`` the APC parameters MUST be None; passing
    any APC evidence raises. Returns (chunks, candidates, decisions,
    episodes_by_run).
    """
    if profile == PROFILE_AVL:
        if edges or signals_by_id:
            raise AssertionError("V1R is AVL-only: APC edges/signals are not accepted")
        assert_gtfs_only_clusters(clusters)
        edges, signals_by_id = [], {}
        mode = "activity_signals"  # irrelevant: no APC evidence exists
    elif profile == PROFILE_APC_STRUCTURAL:
        mode = "structural"
    elif profile == PROFILE_APC_ACTIVITY:
        mode = "activity_signals"
    else:
        raise ValueError(f"unknown profile {profile!r}")

    all_chunks: list[ChunkRecord] = []
    all_cands: list = []
    all_decisions: list = []
    episodes_by_run: dict[str, list] = {}
    for run in sorted(runs, key=lambda r: r.run_id):
        pings = ordered_run_pings(run, pings_by_id)
        episodes = detect_episodes(
            run,
            pings,
            clusters,
            edges or [],
            signals_by_id or {},
            cfg=episode_cfg,
            along_by_id=along_by_id,
            service_anchor_mode=mode,
        )
        episodes_by_run[run.run_id] = episodes
        cands = candidates_from_episodes(
            run.run_id,
            episodes,
            profile=profile,
            cfg=boundary_cfg,
        )
        decisions = decide_boundaries_v2(run.run_id, cands, cfg=boundary_cfg)
        cuts = applied_cuts(cands, decisions)
        all_chunks.extend(chunks_from_cuts(profile, run, pings, cuts))
        all_cands.extend(cands)
        all_decisions.extend(decisions)
    return all_chunks, all_cands, all_decisions, episodes_by_run


def profile_crosswalk(
    avl_chunks: list[ChunkRecord], apc_chunks: list[ChunkRecord]
) -> list[dict]:
    """Lineage between the two experimental identities: for each AVL
    chunk, the APC chunks sharing source pings and the Jaccard overlap.
    The identities are never interchangeable (spec section 7)."""
    rows: list[dict] = []
    apc_by_run: dict[str, list[ChunkRecord]] = {}
    for c in apc_chunks:
        apc_by_run.setdefault(c.run_id, []).append(c)
    for a in avl_chunks:
        aset = set(a.record_ids)
        for b in apc_by_run.get(a.run_id, []):
            bset = set(b.record_ids)
            inter = len(aset & bset)
            if not inter:
                continue
            rows.append(
                {
                    "avl_chunk_id": a.chunk_id,
                    "apc_chunk_id": b.chunk_id,
                    "shared_pings": inter,
                    "jaccard_millis": inter * 1000 // len(aset | bset),
                    "avl_role": a.role.value,
                    "apc_role": b.role.value,
                }
            )
    return rows
