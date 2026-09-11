"""Firewall and baseline (B0/B1) tests for the R1 design spike.

Three concerns: (1) the scoring firewall — passenger quantities and
legacy identity fields can never register in, or enter, a score;
(2) B0, the legacy artifact-conditioned baseline, faithfully reproduces
the legacy join's failure modes so they stay measurable; (3) B1, the
bounded spatiotemporal baseline, accepts only a unique hard-valid
attachment and otherwise abstains.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
from pathlib import Path

import polars as pl
import pytest
from r1spike.baselines import (
    LEGACY_ARTIFACT_NAMES,
    B1Decision,
    b0_join,
    b1_b2_agreement,
    b1_resolve,
    check_b0_availability,
)
from r1spike.candidate_generation import ApcRecord
from r1spike.contracts import (
    EXCLUDED_IDENTITY_FEATURES,
    ApcCandidate,
    EvidenceRegime,
    Reason,
    SegmentCandidate,
)
from r1spike.scoring import ScoreContract, load_contract
from r1spike.segmentation_baselines import Segment, SegPing, segment_vehicle

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "experimental_score_contract.yaml"
)

# Lowercase names as registered in EXCLUDED_IDENTITY_FEATURES plus the
# legacy source columns' original capitalization: the firewall check
# lowercases the incoming name, so both spellings must be rejected.
FIREWALLED_INPUT_NAMES = [
    "boarding",
    "alighting",
    "arrload",
    "depload",
    "avgsample",
    "notrips",
    "legacy_tripid",
    "Boarding",
    "Alighting",
    "Arrload",
    "Depload",
    "AvgSample",
    "NoTrips",
    "Legacy_TripID",
]

PASSENGER_FIELD_NAMES = {
    "boarding",
    "alighting",
    "arrival_load",
    "departure_load",
    "avg_sample",
    "no_trips",
}


@pytest.fixture(scope="module")
def conservative_contract() -> ScoreContract:
    return load_contract(CONTRACT_PATH, "conservative")


def _ping(record_id: str, instant_s: int, *, tatrip_id: str | None) -> SegPing:
    return SegPing(
        record_id=record_id,
        vehicle="veh-9",
        instant_s=instant_s,
        candidate_instants=(instant_s,),
        fold_status="resolved",
        local_date="2023-11-05",
        route_label="39",
        destination_label=None,
        run_label=None,
        tatrip_id=tatrip_id,
        latitude=None,
        longitude=None,
    )


def _segment(
    segment_id: str,
    *,
    vehicle: str = "veh-1",
    start: int = 10_000,
    end: int = 12_000,
    time_uncertain: bool = False,
) -> Segment:
    return Segment(
        segment_id=segment_id,
        variant="x4a",
        vehicle=vehicle,
        record_ids=("p1", "p2"),
        start_instant_s=start,
        end_instant_s=end,
        time_uncertain=time_uncertain,
        n_unresolved=1 if time_uncertain else 0,
        local_dates=("2023-11-05",),
        route_label_counts={"39": 2},
    )


def _record(
    record_id: str = "apc-1",
    *,
    vehicle: str = "veh-1",
    arrival_candidates: tuple[tuple[int, int], ...] = ((0, 10_500),),
    departure_candidates: tuple[tuple[int, int], ...] = (),
) -> ApcRecord:
    return ApcRecord(
        record_id=record_id,
        vehicle=vehicle,
        arrival_wall="2023-11-05 02:55:00",
        departure_wall=None,
        local_date="2023-11-05",
        route_label="39",
        branch_label=None,
        direction_label=None,
        stop_code="1234",
        latitude=None,
        longitude=None,
        arrival_candidates=arrival_candidates,
        departure_candidates=departure_candidates,
    )


class TestScoringFirewall:
    @pytest.mark.parametrize("feature_name", FIREWALLED_INPUT_NAMES)
    def test_score_candidate_rejects_firewalled_feature_case_insensitively(
        self, conservative_contract: ScoreContract, feature_name: str
    ) -> None:
        assert feature_name.lower() in EXCLUDED_IDENTITY_FEATURES
        with pytest.raises(ValueError, match="firewalled"):
            conservative_contract.score_candidate(
                "apc_attachment",
                {feature_name: 12},
                EvidenceRegime.AVL_ONLY,
            )

    def test_score_candidate_rejects_unregistered_feature_name(
        self, conservative_contract: ScoreContract
    ) -> None:
        with pytest.raises(ValueError, match="unregistered"):
            conservative_contract.score_candidate(
                "apc_attachment",
                {"x_made_up_feature_never_registered": 1},
                EvidenceRegime.AVL_ONLY,
            )

    def test_contract_construction_refuses_registering_firewalled_feature(
        self, conservative_contract: ScoreContract
    ) -> None:
        """The firewall holds at registration time too: a raw contract
        dict that registers a passenger feature (any capitalization —
        validation lowercases the name) must not construct."""
        raw = copy.deepcopy(conservative_contract.raw)
        raw["features"]["boarding"] = copy.deepcopy(
            raw["features"]["att_route_agreement_millis"]
        )
        with pytest.raises(ValueError, match="firewalled"):
            ScoreContract(raw, "conservative")
        raw_capitalized = copy.deepcopy(conservative_contract.raw)
        raw_capitalized["features"]["Boarding"] = copy.deepcopy(
            raw_capitalized["features"]["att_route_agreement_millis"]
        )
        with pytest.raises(ValueError, match="firewalled"):
            ScoreContract(raw_capitalized, "conservative")

    def test_candidate_dataclasses_structurally_carry_no_passenger_fields(
        self,
    ) -> None:
        """Required test 16, structural half.

        Perturbing passenger values cannot change B1/B2 candidates or
        scores because they never enter the model: ``ApcRecord`` (the
        record shape feeding candidate generation and B1) and the
        candidate tuples ``SegmentCandidate``/``ApcCandidate`` (the only
        feature flow into scoring) carry no passenger field at all, so
        there is no code path from a passenger value to a candidate or a
        score. Nothing is generated here — the dataclass field lists are
        the whole proof.
        """
        for cls in (ApcRecord, SegmentCandidate, ApcCandidate):
            field_names = {f.name for f in dataclasses.fields(cls)}
            assert not field_names & PASSENGER_FIELD_NAMES, cls.__name__
            assert not field_names & EXCLUDED_IDENTITY_FEATURES, cls.__name__


class TestB0Legacy:
    def test_b0_join_accepts_9083_s_stale_match_that_the_contract_hard_rejects(
        self, conservative_contract: ScoreContract
    ) -> None:
        """Required test 9, legacy half.

        The legacy backward as-of join has no tolerance: an APC row for
        the same TripID 9,083 s before the AVL ping still joins. The
        spike's contract hard-rejects the same gap
        (``att_interval_gap_s`` hard bound 3,600 s, XR1.TEMPORAL.STALE),
        so the LEGACY baseline accepts exactly what the spike's contract
        hard-rejects — measured, never treated as correctness.
        """
        avl = pl.DataFrame({"TripID": [1], "Message_DateTime": [100_000]})
        apc = pl.DataFrame(
            {
                "TripID": [1],
                "StopArrivalTime": [100_000 - 9_083],
                "Arrload": [12.0],
            }
        )
        joined = b0_join(avl, apc)
        assert joined.height == 1
        row = joined.row(0, named=True)
        assert row["pc"] == 12
        assert row["Message_DateTime"] - row["StopArrivalTime"] == 9_083

        spec = conservative_contract.features["att_interval_gap_s"]
        assert spec.hard_bound_max == 3_600
        qscore, reason, hard_invalid = conservative_contract.feature_qscore(spec, 9_083)
        assert hard_invalid is True
        assert qscore is None
        assert reason == Reason.TEMPORAL_STALE

    def test_b0_join_coerces_missing_load_to_integer_zero(self) -> None:
        """No prior APC row exists for the ping, yet ``pc`` comes out as
        integer 0 — the legacy pipeline conflates missing evidence with
        a true zero load."""
        avl = pl.DataFrame({"TripID": [7], "Message_DateTime": [5_000]})
        apc = pl.DataFrame(
            {"TripID": [7], "StopArrivalTime": [6_000], "Arrload": [3.0]}
        )
        row = b0_join(avl, apc).row(0, named=True)
        assert row["Arrload"] is None
        assert row["pc"] == 0

    def test_b0_join_truncates_fractional_loads_toward_zero(self) -> None:
        avl = pl.DataFrame({"TripID": [1, 2], "Message_DateTime": [1_000, 1_000]})
        apc = pl.DataFrame(
            {
                "TripID": [1, 2],
                "StopArrivalTime": [900, 900],
                "Arrload": [2.5, 7.9],
            }
        )
        joined = b0_join(avl, apc).sort("TripID")
        assert joined["pc"].to_list() == [2, 7]

    def test_tripid_permutation_changes_b0_output_but_not_x4a_segmentation(
        self,
    ) -> None:
        """Required test 17.

        Legacy ``TripID`` is a precomputed legacy join key with no
        spike-side input at all: no spike module consumes it, so
        permuting it can only move B0's join pairs. The raw AVL field
        ``tatripId`` (``SegPing.tatrip_id``) is a different field and
        enters only X4-B's debounced soft context evidence; X4-A never
        reads it, so permuting it changes no X4-A grouping and no
        segment identifier.
        """
        avl = pl.DataFrame({"TripID": ["A", "B"], "Message_DateTime": [1_000, 1_000]})
        apc = pl.DataFrame(
            {
                "TripID": ["A", "B"],
                "StopArrivalTime": [900, 900],
                "Arrload": [5.0, 7.0],
            }
        )
        apc_permuted = pl.DataFrame(
            {
                "TripID": ["B", "A"],
                "StopArrivalTime": [900, 900],
                "Arrload": [5.0, 7.0],
            }
        )
        joined = b0_join(avl, apc)
        original = dict(
            zip(joined["TripID"].to_list(), joined["pc"].to_list(), strict=True)
        )
        joined_permuted = b0_join(avl, apc_permuted)
        permuted = dict(
            zip(
                joined_permuted["TripID"].to_list(),
                joined_permuted["pc"].to_list(),
                strict=True,
            )
        )
        assert original == {"A": 5, "B": 7}
        assert permuted == {"A": 7, "B": 5}
        assert original != permuted

        pings = [
            _ping("p1", 0, tatrip_id="T1"),
            _ping("p2", 60, tatrip_id="T1"),
            _ping("p3", 120, tatrip_id="T2"),
            _ping("p4", 180, tatrip_id="T2"),
        ]
        permuted_pings = [
            dataclasses.replace(p, tatrip_id=t)
            for p, t in zip(pings, ["T2", "T1", "T1", "T2"], strict=True)
        ]
        baseline = segment_vehicle(pings, variant="x4a", max_continuity_gap_s=600)
        after_permutation = segment_vehicle(
            permuted_pings, variant="x4a", max_continuity_gap_s=600
        )
        assert [s.record_ids for s in baseline] == [("p1", "p2", "p3", "p4")]
        assert [s.record_ids for s in after_permutation] == [
            s.record_ids for s in baseline
        ]
        assert [s.segment_id for s in after_permutation] == [
            s.segment_id for s in baseline
        ]

    def test_availability_false_when_legacy_artifacts_absent(
        self, tmp_path: Path
    ) -> None:
        result = check_b0_availability([tmp_path])
        assert result.available is False
        assert result.sha256 == {}
        assert len(result.checked_paths) == len(LEGACY_ARTIFACT_NAMES)
        # One artifact alone is not enough either.
        partial = tmp_path / "partial"
        partial.mkdir()
        (partial / LEGACY_ARTIFACT_NAMES[0]).write_bytes(b"avl only")
        partial_result = check_b0_availability([partial])
        assert partial_result.available is False
        assert partial_result.sha256 == {}

    def test_availability_true_with_both_artifacts_records_sha256(
        self, tmp_path: Path
    ) -> None:
        contents = {
            name: f"synthetic {name}".encode() for name in LEGACY_ARTIFACT_NAMES
        }
        for name, data in contents.items():
            (tmp_path / name).write_bytes(data)
        result = check_b0_availability([tmp_path])
        assert result.available is True
        assert result.sha256 == {
            name: hashlib.sha256(data).hexdigest() for name, data in contents.items()
        }


class TestB1Baseline:
    def test_unique_surviving_segment_is_accepted(self) -> None:
        decision = b1_resolve(
            _record(),
            [_segment("seg-A")],
            max_gap_s=600,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39"},
        )
        assert decision.status == "accepted_unique"
        assert decision.segment_id == "seg-A"
        assert decision.n_surviving == 1

    def test_two_surviving_segments_abstain_multiple(self) -> None:
        segments = [
            _segment("seg-A"),
            _segment("seg-B", start=12_100, end=13_000),
        ]
        decision = b1_resolve(
            _record(arrival_candidates=((0, 12_050),)),  # 50 s from each
            segments,
            max_gap_s=600,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39", "seg-B": "39"},
        )
        assert decision.status == "abstain_multiple"
        assert decision.segment_id is None
        assert decision.n_surviving == 2

    def test_no_survivors_for_gap_vehicle_route_or_time_uncertainty(self) -> None:
        """Each hard filter alone empties the survivor set: gap beyond
        the bound, wrong vehicle namespace, route-label mismatch, and a
        time-uncertain segment are all abstain_none."""
        gap_too_large = b1_resolve(
            _record(arrival_candidates=((0, 20_000),)),  # 8,000 s away
            [_segment("seg-A")],
            max_gap_s=600,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39"},
        )
        wrong_vehicle = b1_resolve(
            _record(),
            [_segment("seg-A", vehicle="veh-2")],
            max_gap_s=600,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39"},
        )
        route_mismatch = b1_resolve(
            _record(),
            [_segment("seg-A")],
            max_gap_s=600,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "501"},
        )
        time_uncertain = b1_resolve(
            _record(),
            [_segment("seg-A", time_uncertain=True)],
            max_gap_s=600,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39"},
        )
        for decision in (gap_too_large, wrong_vehicle, route_mismatch, time_uncertain):
            assert decision.status == "abstain_none"
            assert decision.segment_id is None
            assert decision.n_surviving == 0

    def test_non_unique_stop_mapping_abstains_regardless_of_perfect_evidence(
        self,
    ) -> None:
        decision = b1_resolve(
            _record(),
            [_segment("seg-A")],
            max_gap_s=600,
            stop_mapping_unique=False,
            route_labels_by_segment={"seg-A": "39"},
        )
        assert decision.status == "abstain_none"
        assert decision.segment_id is None
        assert decision.n_surviving == 0

    def test_ambiguous_record_survives_if_either_fold_gap_fits_the_bound(
        self,
    ) -> None:
        """Fall-back ambiguity: two arrival candidates 3,600 s apart.
        Fold 0's gap (6,000 s) violates the bound but fold 1's (2,400 s)
        fits, so the record survives; a bound below both gaps abstains."""
        record = _record(arrival_candidates=((0, 4_000), (1, 7_600)))
        accepted = b1_resolve(
            record,
            [_segment("seg-A")],
            max_gap_s=3_000,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39"},
        )
        assert accepted.status == "accepted_unique"
        assert accepted.segment_id == "seg-A"
        rejected = b1_resolve(
            record,
            [_segment("seg-A")],
            max_gap_s=2_000,
            stop_mapping_unique=True,
            route_labels_by_segment={"seg-A": "39"},
        )
        assert rejected.status == "abstain_none"

    def test_b1_b2_agreement_report_covers_all_five_buckets(self) -> None:
        b1_decisions = {
            "r1": B1Decision("r1", "accepted_unique", "seg-A", 1),
            "r2": B1Decision("r2", "accepted_unique", "seg-A", 1),
            "r3": B1Decision("r3", "accepted_unique", "seg-A", 1),
            "r4": B1Decision("r4", "abstain_none", None, 0),
            "r5": B1Decision("r5", "abstain_multiple", None, 2),
        }
        b2_dispositions = {
            "r1": "accepted",
            "r2": "accepted",
            "r3": "low_score",
            "r4": "accepted",
            "r5": "ambiguous",
        }
        b2_selected_segment: dict[str, str | None] = {
            "r1": "seg-A",
            "r2": "seg-B",
            "r3": None,
            "r4": "seg-C",
            "r5": None,
        }
        report = b1_b2_agreement(b1_decisions, b2_dispositions, b2_selected_segment)
        assert report == {
            "both_accept_same_segment": 1,
            "both_accept_different_segment": 1,
            "b1_accept_b2_abstain": 1,
            "b2_accept_b1_abstain": 1,
            "both_abstain": 1,
        }
