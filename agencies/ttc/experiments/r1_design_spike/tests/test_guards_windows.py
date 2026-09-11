"""Tests for publication guards (``artifacts_io``) and window ownership.

The guard tests prove the spike is structurally unable to emit a
TIDES-shaped artifact; the window tests pin the deterministic ownership
rules that make batching an execution schedule, never a semantic input.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from r1spike.artifacts_io import PublicationGuardError, SpikeRun
from r1spike.candidate_generation import ApcRecord
from r1spike.contracts import (
    NO_ATTACHMENT,
    NO_ROUTE_MAPPING,
    UNRESOLVED_SERVICE_DATE,
    ApcCandidate,
)
from r1spike.segmentation_baselines import Segment
from r1spike.windows import apc_owner, build_windows, segment_owner, window_order

ASSUMPTIONS = ["wall date stands in for source date key"]
UNRESOLVED_CONTRACTS = ["D-11 branch mapping semantics"]


def make_run(run_dir: Path) -> SpikeRun:
    return SpikeRun(
        run_dir,
        config_hash="cfg-hash",
        contract_hash="contract-hash",
        input_hashes={"x_avl.csv": "abc123"},
        assumptions=list(ASSUMPTIONS),
        unresolved_design_contracts=list(UNRESOLVED_CONTRACTS),
    )


def make_valid_run(tmp_path: Path) -> SpikeRun:
    return make_run(tmp_path / "artifacts" / "experimental-r1-spike-t1")


class TestPublicationGuards:
    def test_parquet_write_produces_artifact_and_marker_sidecar(
        self, tmp_path: Path
    ) -> None:
        run = make_valid_run(tmp_path)
        df = pl.DataFrame({"segment_id": ["xs1-a", "xs1-b"], "n_pings": [3, 5]})
        path = run.write_parquet(
            df,
            "x_segment_diagnostics.parquet",
            grain="one row per experimental segment",
            key=["segment_id"],
        )
        assert path.exists()
        meta_path = run.run_dir / "x_segment_diagnostics.parquet.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["profile"] == "diagnostic-experimental-r1-spike"
        assert meta["publishable"] is False
        assert meta["tides_conformance_claimed"] is False
        assert meta["authorization_basis"].startswith(
            "experimental design-validation spike"
        )
        assert meta["assumptions"] == ASSUMPTIONS
        assert meta["unresolved_design_contracts"] == UNRESOLVED_CONTRACTS
        assert meta["row_grain"] == "one row per experimental segment"
        assert meta["key"] == ["segment_id"]
        assert meta["schema_version"] == 1
        assert meta["rows"] == 2
        assert meta["columns"] == ["segment_id", "n_pings"]

    def test_run_dir_not_directly_under_artifacts_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PublicationGuardError, match="directly under artifacts/"):
            make_run(tmp_path / "output" / "experimental-r1-spike-t1")

    def test_run_dir_without_experimental_prefix_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(
            PublicationGuardError, match="must live under an experimental-r1-spike-"
        ):
            make_run(tmp_path / "artifacts" / "nightly-run-01")

    def test_run_dir_inside_database_offline_is_refused(self, tmp_path: Path) -> None:
        bad = tmp_path / "database_offline" / "artifacts" / "experimental-r1-spike-t1"
        with pytest.raises(PublicationGuardError, match="immutable source"):
            make_run(bad)

    @pytest.mark.parametrize(
        "name",
        [
            "trips_performed.parquet",
            "vehicle_locations.parquet",
            "stop_visits.csv",
            "x_tides_output.parquet",  # contains the 'tides' token
        ],
    )
    def test_tides_shaped_tabular_names_are_refused(
        self, tmp_path: Path, name: str
    ) -> None:
        run = make_valid_run(tmp_path)
        df = pl.DataFrame({"a": [1]})
        with pytest.raises(PublicationGuardError, match="TIDES"):
            run.write_parquet(df, name, grain="one row per a", key=["a"])
        assert not (run.run_dir / name).exists()

    def test_datapackage_json_is_refused(self, tmp_path: Path) -> None:
        run = make_valid_run(tmp_path)
        with pytest.raises(PublicationGuardError, match="TIDES"):
            run.write_json({"resources": []}, "datapackage.json")
        assert not (run.run_dir / "datapackage.json").exists()

    def test_parquet_without_experimental_prefix_is_refused(
        self, tmp_path: Path
    ) -> None:
        run = make_valid_run(tmp_path)
        df = pl.DataFrame({"a": [1]})
        with pytest.raises(PublicationGuardError, match="x_ prefix"):
            run.write_parquet(df, "segments.parquet", grain="one row per a", key=["a"])

    def test_non_allowlisted_json_is_refused(self, tmp_path: Path) -> None:
        run = make_valid_run(tmp_path)
        with pytest.raises(PublicationGuardError, match="not an allowlisted"):
            run.write_json({"note": "scratch"}, "notes.json")

    def test_allowlisted_metrics_json_succeeds_with_markers(
        self, tmp_path: Path
    ) -> None:
        run = make_valid_run(tmp_path)
        path = run.write_json({"n_segments": 4}, "metrics.json")
        assert path.name == "metrics.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["n_segments"] == 4
        assert payload["profile"] == "diagnostic-experimental-r1-spike"
        assert payload["publishable"] is False
        assert payload["tides_conformance_claimed"] is False

    def test_write_manifest_produces_run_manifest_with_markers(
        self, tmp_path: Path
    ) -> None:
        run = make_valid_run(tmp_path)
        run.write_json({"n_segments": 4}, "metrics.json")
        path = run.write_manifest({"finding": "none"})
        assert path.name == "run_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == "experimental-r1-spike-t1"
        assert manifest["profile"] == "diagnostic-experimental-r1-spike"
        assert manifest["publishable"] is False
        assert manifest["tides_conformance_claimed"] is False
        assert "metrics.json" in manifest["artifacts"]
        assert manifest["finding"] == "none"


SPAN_START = 1_700_000_000
SPAN_END = SPAN_START + 86_400  # windows of 6 h tile this span


def make_segment(start_s: int, end_s: int, *, segment_id: str) -> Segment:
    return Segment(
        segment_id=segment_id,
        variant="x4a",
        vehicle="veh-1",
        record_ids=("r1", "r2"),
        start_instant_s=start_s,
        end_instant_s=end_s,
        time_uncertain=False,
        n_unresolved=0,
        local_dates=("2023-11-14",),
        route_label_counts={"39": 2},
    )


def make_apc_record(
    *,
    arrival_candidates: tuple[tuple[int, int], ...],
    local_date: str = "2023-11-14",
) -> ApcRecord:
    return ApcRecord(
        record_id="apc-1",
        vehicle="veh-1",
        arrival_wall="2023-11-14 12:00:00",
        departure_wall=None,
        local_date=local_date,
        route_label="39",
        branch_label=None,
        direction_label="NB",
        stop_code="1234",
        latitude=43.7,
        longitude=-79.4,
        arrival_candidates=arrival_candidates,
        departure_candidates=(),
    )


def make_candidate(segment_id: str) -> ApcCandidate:
    return ApcCandidate(
        apc_source_record_id="apc-1",
        time_fold_hypothesis=0,
        service_date_hypothesis="2023-11-14",
        feed_id="feed-1",
        vehicle_mapping_hypothesis="veh-1",
        route_mapping_hypothesis="route-39",
        stop_mapping_hypothesis="stop-1",
        candidate_segment_id=segment_id,
        feature_vector={},
    )


def make_null_candidate() -> ApcCandidate:
    # is_null_alternative() keys on candidate_segment_id == NO_ATTACHMENT.
    return ApcCandidate(
        apc_source_record_id="apc-1",
        time_fold_hypothesis=None,
        service_date_hypothesis=UNRESOLVED_SERVICE_DATE,
        feed_id=None,
        vehicle_mapping_hypothesis=None,
        route_mapping_hypothesis=NO_ROUTE_MAPPING,
        stop_mapping_hypothesis=NO_ATTACHMENT,
        candidate_segment_id=NO_ATTACHMENT,
        feature_vector={},
    )


class TestWindowOwnership:
    def test_build_windows_tiles_the_span_contiguously(self) -> None:
        windows = build_windows(SPAN_START, SPAN_END, hours=6)
        assert len(windows) == 5
        assert [w.index for w in windows] == [0, 1, 2, 3, 4]
        assert windows[0].start_s == SPAN_START
        for prev, nxt in zip(windows, windows[1:], strict=False):
            assert prev.end_s == nxt.start_s
        assert windows[-1].end_s >= SPAN_END

    def test_segment_owner_is_window_containing_start_instant(self) -> None:
        windows = build_windows(SPAN_START, SPAN_END, hours=6)
        segment = make_segment(
            SPAN_START + 30_000, SPAN_START + 33_000, segment_id="xs1-" + "0" * 32
        )
        assert segment_owner(segment, windows) == 1

    def test_apc_owner_follows_earliest_starting_candidate_segment(self) -> None:
        windows = build_windows(SPAN_START, SPAN_END, hours=6)
        early = make_segment(
            SPAN_START + 1_000, SPAN_START + 2_000, segment_id="xs1-" + "a" * 32
        )
        late = make_segment(
            SPAN_START + 50_000, SPAN_START + 52_000, segment_id="xs1-" + "b" * 32
        )
        segments_by_id = {early.segment_id: early, late.segment_id: late}
        record = make_apc_record(arrival_candidates=((0, SPAN_START + 51_000),))
        candidates = [
            make_candidate(late.segment_id),
            make_candidate(early.segment_id),
            make_null_candidate(),
        ]
        # The arrival instant itself sits in window 2, but the earliest-
        # starting candidate segment (window 0) owns the record.
        assert apc_owner(record, candidates, segments_by_id, windows) == 0

    def test_apc_owner_without_candidates_uses_earliest_arrival_instant(self) -> None:
        windows = build_windows(SPAN_START, SPAN_END, hours=6)
        record = make_apc_record(
            arrival_candidates=((1, SPAN_START + 70_000), (0, SPAN_START + 30_000)),
        )
        assert apc_owner(record, [], {}, windows) == 1

    def test_null_only_candidate_list_falls_back_to_time_hypotheses(self) -> None:
        windows = build_windows(SPAN_START, SPAN_END, hours=6)
        record = make_apc_record(arrival_candidates=((0, SPAN_START + 30_000),))
        assert apc_owner(record, [make_null_candidate()], {}, windows) == 1

    def test_no_time_hypotheses_fall_back_to_stable_source_calendar(self) -> None:
        windows = build_windows(SPAN_START, SPAN_END, hours=6)
        record = make_apc_record(arrival_candidates=(), local_date="2023-11-14")
        first = apc_owner(record, [], {}, windows)
        assert isinstance(first, int)
        assert 0 <= first < len(windows)
        assert apc_owner(record, [], {}, windows) == first

    def test_window_order_forward_reverse_and_seeded_shuffle(self) -> None:
        assert window_order(5, "forward", seed=0) == [0, 1, 2, 3, 4]
        assert window_order(5, "reverse", seed=0) == [4, 3, 2, 1, 0]
        shuffled = window_order(20, "shuffled", seed=99)
        assert shuffled == window_order(20, "shuffled", seed=99)  # same seed
        assert sorted(shuffled) == list(range(20))  # a true permutation
        assert shuffled != list(range(20))

    def test_different_shuffle_seeds_give_different_orders(self) -> None:
        assert window_order(20, "shuffled", seed=1) != window_order(
            20, "shuffled", seed=2
        )

    def test_unknown_window_order_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown window order"):
            window_order(5, "sideways", seed=0)
