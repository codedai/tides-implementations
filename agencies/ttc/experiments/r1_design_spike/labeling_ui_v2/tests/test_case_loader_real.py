"""Render harness: every pilot case dry-loads and every panel degrades.

Mirrors the v1 render harness for the v2 loader: all 18 parent cases must
resolve chunk, vehicle, and pings; the candidate universe must be complete
with the null option; every figure builder returns a figure or None and
never leaks model vocabulary.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from tests.conftest import AUDIT_DIR, requires_artifacts
from uiv2 import adapters
from uiv2.case_loader import build_bundle, load_candidate_detail, load_diagnostics
from uiv2.plots import (
    fig_boundary,
    fig_map,
    fig_stop_ladder,
    fig_time_progress,
    fig_time_tracks,
    fig_timeline,
)

pytestmark = requires_artifacts

MODEL_VOCAB = (
    "low_margin",
    "qscore",
    "unresolved_fragment",
    "terminal_turnaround",
    "trip_core_candidate",
    "survival_margin",
    "evidence_margin",
    "anchor status",
)


@pytest.fixture(scope="module")
def routing(ui_store) -> pl.DataFrame:
    return adapters.load_routing(ui_store)


@pytest.fixture(scope="module")
def bundles(ui_store, routing) -> list:
    return [
        build_bundle(ui_store, row["case_id"], routing_row=row)
        for row in routing.iter_rows(named=True)
    ]


def test_all_cases_resolve_chunk_vehicle_and_pings(bundles):
    assert len(bundles) == 18
    for bundle in bundles:
        assert bundle.chunk.start_epoch is not None, bundle.case_id
        assert bundle.vehicle_id is not None, bundle.case_id
        assert bundle.chunk.pings is not None, bundle.case_id
        recorded = bundle.chunk.n_pings_recorded
        if recorded:
            actual = bundle.chunk.pings.height
            assert abs(actual - recorded) <= max(2, int(0.02 * recorded)), (
                bundle.case_id
            )


def test_universe_complete_null_present_aliases_unique(bundles):
    for bundle in bundles:
        real = bundle.real_candidates
        assert 26 <= len(real) <= 57, bundle.case_id
        assert bundle.null_option is not None, bundle.case_id
        aliases = [c.alias for c in bundle.candidates]
        assert len(aliases) == len(set(aliases)), bundle.case_id
        orders = [c.display_order for c in bundle.candidates]
        assert orders == sorted(orders), bundle.case_id


def test_apc_events_belong_to_resolved_vehicle(bundles):
    for bundle in bundles:
        if bundle.apc_events is None:
            continue
        vehicles = set(
            bundle.apc_events["vehicle_id_raw"].drop_nulls().unique().to_list()
        )
        assert vehicles <= {bundle.vehicle_id}, bundle.case_id


def test_light_metrics_are_computable(bundles):
    for bundle in bundles:
        for candidate in bundle.real_candidates[:5]:
            candidate.overlap_s(bundle.chunk)
            candidate.start_residual_s(bundle.chunk)
            candidate.end_residual_s(bundle.chunk)


def test_every_panel_renders_or_returns_none(bundles):
    for bundle in bundles:
        for figure in (
            fig_map(bundle),
            fig_timeline(bundle),
            fig_time_progress(bundle, None),
            fig_time_tracks(bundle),
            fig_boundary(bundle, interval_rel_s=(0, 60)),
        ):
            assert figure is None or hasattr(figure, "to_json")


def test_figures_never_leak_model_vocabulary(bundles):
    for bundle in bundles[:6]:
        for figure in (
            fig_map(bundle),
            fig_timeline(bundle),
            fig_time_progress(bundle, None),
            fig_time_tracks(bundle),
        ):
            if figure is None:
                continue
            serialized = figure.to_json().lower()
            for word in MODEL_VOCAB:
                assert word not in serialized, (bundle.case_id, word)


def test_candidate_detail_and_stop_ladder(ui_store, bundles):
    bundle = next(b for b in bundles if b.case_id == "C-5555da7a")
    complete = [
        c for c in bundle.real_candidates if c.kind == "complete_scheduled_trip"
    ]
    detail = load_candidate_detail(ui_store, bundle, complete[0].alias)
    assert detail is not None
    assert detail.sched_events is not None
    assert detail.metrics is not None
    ladder = fig_stop_ladder(bundle, detail)
    assert ladder is None or hasattr(ladder, "to_json")
    progress = fig_time_progress(bundle, detail)
    assert progress is None or hasattr(progress, "to_json")


def test_child_bundle_attaches_child_row(ui_store, routing):
    children = adapters.load_children(ui_store)
    child = children.row(0, named=True)
    parent_rows = routing.filter(pl.col("case_id") == child["parent_case_id"])
    bundle = build_bundle(
        ui_store,
        child["child_case_id"],
        routing_row=parent_rows.row(0, named=True),
        child_row=child,
    )
    assert bundle.is_child
    assert bundle.parent_case_id == child["parent_case_id"]
    assert bundle.child_row["child_task_type"] == child["child_task_type"]
    assert bundle.candidates, "children inherit the parent universe"


def test_degraded_store_without_spike02r_still_builds(run01_dir, routing):
    degraded = adapters.ArtifactStoreV2(
        run01_dir=run01_dir,
        spike02r_dir=None,
        normalized_dir=None,
        gtfs_root=None,
        audit_dir=AUDIT_DIR,
    )
    row = routing.row(0, named=True)
    bundle = build_bundle(degraded, row["case_id"], routing_row=row)
    assert bundle.availability, "degradation must be surfaced, not silent"
    assert bundle.chunk.start_epoch is not None  # from the routing manifest
    assert fig_map(bundle) is None or hasattr(fig_map(bundle), "to_json")


def test_diagnostics_loader_is_isolated_from_bundle(ui_store, bundles):
    bundle = bundles[0]
    serialized = json.dumps(
        {
            "routing": {
                k: v
                for k, v in bundle.routing.items()
                if k != "audit_reason_codes"
                and k != "recommended_second_pass_questions"
                and k != "case_grain_suggestion"
            },
            "candidates": [c.candidate_key for c in bundle.candidates],
            "availability": bundle.availability,
        },
        default=str,
    ).lower()
    for word in ("qscore", "low_margin", "survival_margin", "best_key"):
        assert word not in serialized
    diagnostics = load_diagnostics(ui_store, bundle)
    assert isinstance(diagnostics, dict)
