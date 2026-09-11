"""Render harness over the real p4 packet: every case must resolve and every
panel must either render or degrade to None — never raise."""

import polars as pl
import pytest

from labeling.adapters import discover, load_cases
from labeling.plots import fig_map, fig_stop_ladder, fig_time_distance, fig_timeline
from labeling.resolver import build_bundle

from .conftest import ARTIFACTS, CASES_CSV, requires_artifacts

pytestmark = requires_artifacts


@pytest.fixture(scope="module")
def store():
    return discover(CASES_CSV, ARTIFACTS)


@pytest.fixture(scope="module")
def cases(store):
    return load_cases(store)


@pytest.fixture(scope="module")
def bundles(store, cases):
    return [
        build_bundle(store, row) for row in cases.iter_rows(named=True)
    ]


def test_all_cases_resolve_chunk_and_vehicle(bundles):
    assert len(bundles) == 18
    for b in bundles:
        assert b.chunk is not None, b.case_id
        assert b.chunk.start_epoch is not None, b.case_id
        assert b.vehicle_id is not None, (b.case_id, b.availability)
        assert b.chunk.pings is not None and b.chunk.pings.height > 0, b.case_id


def test_ping_counts_match_refined_chunks(bundles):
    """The pings we reconstruct should match the chunk's recorded n_pings —
    strong evidence the vehicle/time identification is right."""
    for b in bundles:
        if b.chunk.n_pings is None:
            continue
        got = b.chunk.pings.height
        assert abs(got - b.chunk.n_pings) <= max(2, 0.02 * b.chunk.n_pings), (
            b.case_id,
            got,
            b.chunk.n_pings,
        )


def test_all_cases_have_candidates_with_letters(bundles):
    for b in bundles:
        assert b.candidates, b.case_id
        letters = [c.letter for c in b.candidates]
        assert letters == sorted(letters)  # A, B, C... deterministic
        assert len(set(letters)) == len(letters)
        # the model's winner must be one of the cards (hidden from reviewer)
        if b.anchor_best_key:
            assert b.letter_of_key(b.anchor_best_key) is not None, b.case_id


def test_letters_are_time_ordered_not_score_ordered(bundles):
    for b in bundles:
        starts = [
            c.metrics.sched_start_epoch
            for c in b.candidates
            if c.metrics.sched_start_epoch is not None
        ]
        assert starts == sorted(starts), b.case_id


def test_all_panels_render_or_degrade(bundles):
    for b in bundles:
        m = fig_map(b)
        tl = fig_timeline(b)
        td = fig_time_distance(b, "A")
        assert m is not None, b.case_id
        assert tl is not None, b.case_id
        assert td is not None, b.case_id
        for c in b.candidates:
            fig_stop_ladder(b, c)  # None allowed (route-level candidates)


def test_degraded_bundle_without_run_dir(tmp_path, cases):
    """Cases CSV alone: no chunk identity, but nothing crashes."""
    csv = tmp_path / "cases.csv"
    csv.write_text(CASES_CSV.read_text())
    bare = discover(csv, tmp_path)
    row = load_cases(bare).row(0, named=True)
    b = build_bundle(bare, row)
    assert b.chunk is None
    assert b.availability  # explains what is missing
    assert fig_map(b) is None
    assert fig_timeline(b) is None
    assert fig_time_distance(b, "A") is None


def test_candidate_metrics_sane(bundles):
    for b in bundles:
        dur = b.chunk.end_epoch - b.chunk.start_epoch
        for c in b.candidates:
            m = c.metrics
            if m.overlap_s is not None:
                assert 0 <= m.overlap_s <= dur + 1, (b.case_id, c.letter)
            if m.chunk_containment is not None:
                assert 0.0 <= m.chunk_containment <= 1.0
            if m.stops_covered_frac is not None:
                assert 0.0 <= m.stops_covered_frac <= 1.0
            if m.direction_consistency is not None:
                assert -1.0 <= m.direction_consistency <= 1.0


def test_alternates_listing_neutral(bundles):
    for b in bundles:
        if b.alternates is None:
            continue
        assert "qscore" not in b.alternates.columns  # neutral: no scores
        assert b.alternates["candidate_key"].n_unique() == b.alternates.height


def test_dst_candidate_ladders_monotonic(bundles):
    """Regression: tz-aware GTFS stop events must not be re-localized (the
    fall-back night's 26:xx stops shifted an hour early and ladders ran
    backwards)."""
    for b in bundles:
        for c in b.candidates:
            se = c.sched_events
            if se is None or "arrival_epoch_s" in (None,):
                continue
            arrs = se.sort("stop_sequence")["arrival_epoch_s"].drop_nulls().to_list()
            assert arrs == sorted(arrs), (b.case_id, c.letter)


def test_dst_crossing_case_uses_elapsed_axis(store, cases):
    """C-7d5fcf98 spans the 2023-11-05 fall-back; wall-clock x positions
    would contradict epoch-based duration labels by an hour."""
    from labeling.plots import TimeAxis

    row = cases.filter(pl.col("case_id") == "C-7d5fcf98").row(0, named=True)
    b = build_bundle(store, row)
    axis = TimeAxis(b)
    assert axis.elapsed, "DST-crossing case must plot on an elapsed-time axis"
    fig = fig_timeline(b)
    assert fig is not None

    # and a mid-afternoon case keeps the friendlier wall-clock axis
    row2 = cases.filter(pl.col("case_id") == "C-1b38b137").row(0, named=True)
    assert not TimeAxis(build_bundle(store, row2)).elapsed


def test_neutral_figures_do_not_leak_model_verdicts(bundles):
    """Model vocabulary must not appear anywhere in the serialized neutral
    figures (trace names, hover templates, annotations)."""
    banned = ["low_margin", "accepted", "unresolved_fragment",
              "terminal_turnaround", "trip_core_candidate", "qscore"]
    for b in bundles:
        payloads = []
        for fig in [fig_map(b), fig_timeline(b), fig_time_distance(b, "A")]:
            if fig is not None:
                payloads.append(fig.to_json())
        for c in b.candidates:
            fig = fig_stop_ladder(b, c)
            if fig is not None:
                payloads.append(fig.to_json())
        blob = " ".join(payloads)
        for word in banned:
            assert word not in blob, (b.case_id, word)


def test_apc_events_belong_to_vehicle_window(bundles):
    for b in bundles:
        if b.apc_events is None or b.apc_events.is_empty():
            continue
        assert (
            b.apc_events["vehicle_id_raw"].cast(pl.String) == str(b.vehicle_id)
        ).all(), b.case_id
