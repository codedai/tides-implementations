"""Evidence-dependency provenance (Spike 02R section 8).

Every boundary and anchor declares its source records; overlap between
the set that BUILT a boundary and the set that SCORES an anchor on the
resulting chunks is the self-reinforcement the review flagged. This
module materializes those edges and quantifies the overlap. The fact
that an APC record helped define a boundary is never counted as
independent confirmation of anything downstream.
"""

from __future__ import annotations

from .spike02_contracts import TerminalEpisode
from .spike02r_contracts import EvidenceEdge, EvidenceUse


def boundary_edges(
    episodes_by_run: dict[str, list[TerminalEpisode]],
    *,
    profile: str,
    folds: dict[str, str] | None = None,
) -> list[EvidenceEdge]:
    edges: list[EvidenceEdge] = []
    for run_id in sorted(episodes_by_run):
        for ep in episodes_by_run[run_id]:
            for rid in ep.apc_record_ids:
                edges.append(
                    EvidenceEdge(
                        derived_id=ep.episode_id,
                        derived_kind="boundary",
                        source_record_id=rid,
                        source_type="apc",
                        evidence_family="terminal_episode_window",
                        use=EvidenceUse.BOUNDARY_GENERATION.value,
                        fold=(folds or {}).get(rid),
                    )
                )
    return edges


def anchor_edges(
    visits_by_segment: dict[str, list[str]],
    boundary_sources_by_segment: dict[str, set[str]],
    *,
    folds: dict[str, str] | None = None,
) -> list[EvidenceEdge]:
    """visits_by_segment: segment -> APC record ids used for per-stop
    anchor evidence. boundary_sources_by_segment: segment -> APC record
    ids that participated in creating its boundaries."""
    edges: list[EvidenceEdge] = []
    for seg_id in sorted(visits_by_segment):
        bset = boundary_sources_by_segment.get(seg_id, set())
        for rid in visits_by_segment[seg_id]:
            overlap = rid in bset
            edges.append(
                EvidenceEdge(
                    derived_id=seg_id,
                    derived_kind="anchor",
                    source_record_id=rid,
                    source_type="apc",
                    evidence_family="per_stop_alignment",
                    use=EvidenceUse.ANCHOR_SCORING.value,
                    fold=(folds or {}).get(rid),
                    overlaps_boundary_evidence=overlap,
                    reuse_reason=(
                        "record also used in boundary generation" if overlap else None
                    ),
                )
            )
    return edges


def reuse_report(b_edges: list[EvidenceEdge], a_edges: list[EvidenceEdge]) -> dict:
    boundary_sources = {e.source_record_id for e in b_edges}
    anchor_sources = {e.source_record_id for e in a_edges}
    per_segment: dict[str, dict] = {}
    for e in a_edges:
        blk = per_segment.setdefault(
            e.derived_id, {"anchor_records": 0, "overlapping": 0}
        )
        blk["anchor_records"] += 1
        blk["overlapping"] += int(e.overlaps_boundary_evidence)
    segments_with_overlap = sum(1 for blk in per_segment.values() if blk["overlapping"])
    segments_fully_independent = sum(
        1
        for blk in per_segment.values()
        if blk["anchor_records"] and not blk["overlapping"]
    )
    total_anchor_edges = sum(b["anchor_records"] for b in per_segment.values())
    total_overlap_edges = sum(b["overlapping"] for b in per_segment.values())
    return {
        "boundary_source_records": len(boundary_sources),
        "anchor_source_records": len(anchor_sources),
        "shared_source_records": len(boundary_sources & anchor_sources),
        "anchor_edges": total_anchor_edges,
        "anchor_edges_overlapping_boundary": total_overlap_edges,
        "anchor_edge_overlap_millis": (
            total_overlap_edges * 1000 // max(1, total_anchor_edges)
        ),
        "segments_scored": len(per_segment),
        "segments_with_boundary_overlap": segments_with_overlap,
        "segments_fully_independent": segments_fully_independent,
        "per_segment": {k: per_segment[k] for k in sorted(per_segment)},
    }


def edge_rows(edges: list[EvidenceEdge]) -> list[dict]:
    return [
        {
            "derived_id": e.derived_id,
            "derived_kind": e.derived_kind,
            "source_record_id": e.source_record_id,
            "source_type": e.source_type,
            "evidence_family": e.evidence_family,
            "use": e.use,
            "fold": e.fold,
            "dependency_generation": e.dependency_generation,
            "independent_confirmation": e.independent_confirmation,
            "overlaps_boundary_evidence": e.overlaps_boundary_evidence,
            "reuse_reason": e.reuse_reason,
        }
        for e in edges
    ]


def assert_avl_only(edges: list[EvidenceEdge]) -> None:
    apc = [e for e in edges if e.source_type == "apc"]
    if apc:
        raise AssertionError(
            f"AVL-only path carries {len(apc)} APC evidence edges "
            f"(first: {apc[0].derived_id} <- {apc[0].source_record_id})"
        )
