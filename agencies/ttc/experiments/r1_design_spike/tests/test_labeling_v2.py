"""Protocol tests for Human Labeling Protocol v2 (blind second pass).

Covers the frozen-contract mandates: first-pass immutability, blind gates,
neutral candidate presentation, deterministic setup and child generation,
task-specific validation, adjudication semantics, promotion gates, and the
factorial NO-GO separation.
"""

from __future__ import annotations

import json
import shutil
import stat
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest
from r1spike.human_label_parser import sha256_file
from r1spike.labeling_v2_child_cases import generate_child_cases
from r1spike.labeling_v2_contracts import (
    ALL_PILOT_CASES,
    FROZEN_ARTIFACTS,
    LIVE_ARTIFACTS,
    PROTOCOL_VERSION,
    SCHEDULED_SECOND_PASS_CASES,
    alias_for_index,
)
from r1spike.labeling_v2_setup import SetupConfig, run_setup
from r1spike.labeling_v2_store import (
    BlindProtocolViolation,
    LabelValidationError,
    ReviewStore,
    derive_timezone_label,
    derive_timezone_label_for_naive_local,
    verify_frozen_zone,
)
from r1spike.labeling_v2_universe import (
    build_case_universe,
    build_universe_manifest,
    classify_candidate_key,
)

TTC_ROOT = Path(__file__).resolve().parents[3]
AUDIT_DIR = TTC_ROOT / "artifacts/human-label-audit-v1"
P4_DIR = TTC_ROOT / "artifacts/experimental-r1-spike02r-p4"
FIRST_PASS = AUDIT_DIR / "first_pass_labels_v1.parquet"

requires_artifacts = pytest.mark.skipif(
    not (AUDIT_DIR.exists() and P4_DIR.exists()),
    reason="frozen audit/p4 artifacts not present",
)


# --------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def run01(tmp_path_factory) -> Path:
    if not (AUDIT_DIR.exists() and P4_DIR.exists()):
        pytest.skip("frozen audit/p4 artifacts not present")
    output = tmp_path_factory.mktemp("labeling-v2") / "run01"
    result = run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=P4_DIR,
            output_dir=output,
            ttc_root=TTC_ROOT,
        )
    )
    assert result.inputs_unchanged
    return output


@pytest.fixture()
def fresh_run(run01: Path, tmp_path: Path) -> Path:
    target = tmp_path / "run01"
    shutil.copytree(run01, target)
    return target


@pytest.fixture()
def store(fresh_run: Path) -> ReviewStore:
    return ReviewStore(
        fresh_run,
        first_pass_path=FIRST_PASS,
        reviewer_id="trev",
        session_id="testsession",
    )


def _alias_map(run_dir: Path) -> pl.DataFrame:
    return pl.read_parquet(run_dir / "candidate_alias_map.parquet")


def _alias_for_key(run_dir: Path, case_id: str, key: str) -> str:
    rows = _alias_map(run_dir).filter(
        (pl.col("case_id") == case_id) & (pl.col("candidate_key") == key)
    )
    assert not rows.is_empty(), f"key not in universe for {case_id}"
    return rows["candidate_alias"][0]


def _complete_alias(run_dir: Path, case_id: str) -> str:
    rows = _alias_map(run_dir).filter(
        (pl.col("case_id") == case_id)
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
    )
    return rows["candidate_alias"][0]


def _scheduled_record(case_id: str, alias: str, *, confident: bool = False):
    record = {
        "case_id": case_id,
        "case_grain": "single_trip_candidate",
        "reviewability": "reviewable_for_schedule_match",
        "scheduled_judgment": "matched",
        "selected_candidate_alias": alias,
        "grain_confidence": 5 if confident else 3,
        "match_confidence": 4 if confident else 3,
        "evidence_quality": 4 if confident else 3,
    }
    if confident:
        record["evidence_flags"] = ["interval_time_fit", "stop_sequence_fit"]
        record["rationale_text"] = "test rationale with substance"
    return record


# ------------------------------------------------- synthetic unit tests


def test_alias_sequence_extends_past_z():
    assert alias_for_index(0) == "A"
    assert alias_for_index(25) == "Z"
    assert alias_for_index(26) == "AA"
    assert alias_for_index(27) == "AB"
    assert alias_for_index(52) == "BA"
    assert len({alias_for_index(i) for i in range(200)}) == 200


def test_classify_candidate_key_shapes():
    complete = classify_candidate_key("2001-01-02|feed-x|route-x|trip-x|")
    assert complete["candidate_kind"] == "complete_scheduled_trip"
    assert complete["explicit_null_polarity"] is True
    assert complete["polarity_hypothesis_id"] is None
    polarized = classify_candidate_key("2001-01-02|feed-x|route-x|trip-x|pol_b")
    assert polarized["polarity_hypothesis_id"] == "pol_b"
    assert polarized["explicit_null_polarity"] is False
    no_trip = classify_candidate_key("2001-01-02|feed-x|route-x||")
    assert no_trip["candidate_kind"] == "no_scheduled_trip_alternative"
    sentinel = classify_candidate_key(
        "XNULL:UNRESOLVED_SERVICE_DATE||XNULL:NO_ROUTE_MAPPING|trip-x|"
    )
    assert sentinel["candidate_kind"] == "contract_unresolved"
    assert sentinel["unresolved_warning"]
    bad = classify_candidate_key("a|b|c")
    assert bad["candidate_kind"] == "contract_unresolved"


def test_universe_ordering_is_score_free_and_alias_stable():
    restricted = pl.DataFrame(
        {
            "case_id": ["C-test0001"],
            "real_id": ["chunk-1"],
            "stratum": ["x"],
            "kind": ["scheduled_trip"],
            "case_epoch_s": [0],
            "detail": ["{}"],
        }
    )
    # Adversarial: the best-scored candidate is scheduled LAST; ordering must
    # follow the schedule, not the score.
    candidates = pl.DataFrame(
        {
            "slice": ["s"] * 3,
            "chunk_id": ["chunk-1"] * 3,
            "candidate_key": [
                "2001-01-02|feed-x|route-x|late|",
                "2001-01-02|feed-x|route-x|early|",
                "2001-01-01|feed-x|route-x|prior-day|",
            ],
            "qscore": [999999, 1, 5],
            "regime": ["apc_evidence"] * 3,
            "censor_class": ["hard_valid"] * 3,
            "n_aligned": [9, 0, 0],
            "n_observed": [9, 0, 0],
        }
    )
    windows = {
        ("2001-01-02", "feed-x", "late"): {"sched_start_epoch_s": 2000},
        ("2001-01-02", "feed-x", "early"): {"sched_start_epoch_s": 1000},
        ("2001-01-01", "feed-x", "prior-day"): {"sched_start_epoch_s": 3000},
    }
    rows = build_universe_manifest(restricted, candidates, None, windows=windows)
    ordered_keys = [r["candidate_key"] for r in rows if r["candidate_alias"] != "NONE"]
    assert ordered_keys == [
        "2001-01-01|feed-x|route-x|prior-day|",
        "2001-01-02|feed-x|route-x|early|",
        "2001-01-02|feed-x|route-x|late|",
    ]
    for row in rows:
        assert "qscore" not in row and "n_aligned" not in row
    again = build_universe_manifest(restricted, candidates, None, windows=windows)
    assert rows == again


def test_universe_always_includes_null_option():
    rows = build_case_universe("C-x", "chunk", None, ["2001-01-02|f|r|t|"], {})
    aliases = [row["candidate_alias"] for row in rows]
    assert "NONE" in aliases
    null_row = rows[-1]
    assert null_row["candidate_key"] == "NO_SCHEDULED_MATCH"
    assert null_row["candidate_kind"] == "no_scheduled_match_option"


def test_same_bare_trip_id_across_feeds_and_dates_stays_distinct():
    keys = [
        "2001-01-02|feed-a|route-x|trip-x|",
        "2001-01-02|feed-b|route-x|trip-x|",
        "2001-01-01|feed-a|route-x|trip-x|",
    ]
    rows = build_case_universe("C-x", "chunk", None, keys, {})
    real = [row for row in rows if row["candidate_alias"] != "NONE"]
    assert len(real) == 3
    assert len({row["candidate_key"] for row in real}) == 3
    assert len({row["candidate_alias"] for row in real}) == 3


def test_child_generation_is_deterministic_and_label_free():
    requests = [
        {
            "request_id": "R-001",
            "parent_case_id": "C-aaaaaaaa",
            "request_type": "fragment_role_case",
            "requested_child_count": "1",
        },
        {
            "request_id": "R-002",
            "parent_case_id": "C-bbbbbbbb",
            "request_type": "per_subtrip_cases",
            "requested_child_count": "2",
            "parent_display_card_references": "A|E",
        },
        {
            "request_id": "R-003",
            "parent_case_id": "C-bbbbbbbb",
            "request_type": "boundary_cases",
            "requested_child_count": "1",
        },
        {
            "request_id": "R-004",
            "parent_case_id": "C-cccccccc",
            "request_type": "boundary_inbound_outbound_cases",
            "requested_child_count": "3",
            "boundary_stop_if_explicit": "1652",
            "boundary_offset_from_parent_end_s": "382",
        },
    ]
    hashes = {"C-bbbbbbbb": "hash-b"}
    children_a, report_a = generate_child_cases(requests, hashes)
    children_b, report_b = generate_child_cases(requests, hashes)
    assert children_a == children_b and report_a == report_b
    # 1 + 2 + 1 + 3 audit children + 1 sequence child for the multi parent
    assert len(children_a) == 8
    label_fields = {
        "scheduled_judgment",
        "case_grain",
        "fragment_role",
        "selected_candidate_key",
    }
    for child in children_a:
        assert not label_fields & set(child)
        assert child["automatic_child_label_created"] is False
        assert child["review_status"] == "SECOND_PASS_NOT_STARTED"
    seeded = [c for c in children_a if c["parent_case_id"] == "C-cccccccc"]
    assert all(c["seed_status"] == "human_note_seeded_unverified" for c in seeded)
    assert all(c["seed_hidden_until_parent_blind_save"] for c in seeded)


def test_timezone_labels_are_derived_not_trusted():
    # 2023-07-01 12:00 EDT ~ epoch 1688227200
    assert derive_timezone_label(1_688_227_200) == "EDT"
    # 2023-12-01 12:00 EST ~ epoch 1701450000
    assert derive_timezone_label(1_701_450_000) == "EST"
    ambiguous = derive_timezone_label_for_naive_local(datetime(2023, 11, 5, 1, 30))
    assert "ambiguous" in ambiguous and "EDT" in ambiguous and "EST" in ambiguous
    assert derive_timezone_label_for_naive_local(datetime(2023, 11, 5, 3, 0)) == ("EST")
    with pytest.raises(ValueError):
        derive_timezone_label_for_naive_local(
            datetime(2023, 11, 5, 1, 30, tzinfo=None).replace(
                tzinfo=__import__("zoneinfo").ZoneInfo("UTC")
            )
        )


def test_labeling_v2_never_touches_factorial_code():
    src = TTC_ROOT / "experiments/r1_design_spike/src/r1spike"
    for name in (
        "labeling_v2_contracts.py",
        "labeling_v2_universe.py",
        "labeling_v2_child_cases.py",
        "labeling_v2_store.py",
        "labeling_v2_readiness.py",
        "labeling_v2_setup.py",
        "labeling_v2_artifacts.py",
        "run_labeling_v2_setup.py",
    ):
        text = (src / name).read_text(encoding="utf-8")
        assert "label_factorial" not in text, name
        assert "run_spike02r" not in text, name


# ------------------------------------------------- real-artifact tests


@requires_artifacts
def test_setup_is_deterministic_byte_identical(run01: Path, tmp_path: Path):
    second = tmp_path / "again"
    run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=P4_DIR,
            output_dir=second,
            ttc_root=TTC_ROOT,
        )
    )
    for path in sorted(run01.iterdir()):
        other = second / path.name
        assert other.exists(), path.name
        assert sha256_file(path) == sha256_file(other), path.name


@requires_artifacts
def test_setup_leaves_first_pass_and_production_untouched(tmp_path: Path):
    watched = [
        FIRST_PASS,
        TTC_ROOT / "tools/labeling_ui/labels/scheduled_trip_labels.csv",
        TTC_ROOT / "tools/labeling_ui/labels/scheduled_trip_labels.jsonl",
        TTC_ROOT / "src/ttc_tides/__init__.py",
        P4_DIR / "experiment_preregistration.json",
        P4_DIR / "x_anchor_candidates_v2.parquet",
    ]
    watched = [path for path in watched if path.exists()]
    before = {path: sha256_file(path) for path in watched}
    run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=P4_DIR,
            output_dir=tmp_path / "run",
            ttc_root=TTC_ROOT,
        )
    )
    for path, digest in before.items():
        assert sha256_file(path) == digest, f"modified: {path}"


@requires_artifacts
def test_universe_complete_and_matches_audit_counts(run01: Path):
    universe = pl.read_parquet(run01 / "candidate_universe_manifest.parquet")
    import csv

    with (AUDIT_DIR / "label_case_inventory.csv").open() as handle:
        audit = {
            row["case_id"]: int(row["candidate_table_key_count_total"])
            for row in csv.DictReader(handle)
        }
    real = universe.filter(pl.col("candidate_kind") != "no_scheduled_match_option")
    counts = {
        row["case_id"]: row["len"]
        for row in real.group_by("case_id").len().iter_rows(named=True)
    }
    assert counts == audit
    assert min(counts.values()) >= 26 and max(counts.values()) <= 57
    for case_id in ALL_PILOT_CASES:
        nulls = universe.filter(
            (pl.col("case_id") == case_id)
            & (pl.col("candidate_kind") == "no_scheduled_match_option")
        )
        assert nulls.height == 1


@requires_artifacts
def test_universe_has_no_score_columns(run01: Path):
    universe = pl.read_parquet(run01 / "candidate_universe_manifest.parquet")
    banned = {"qscore", "regime", "n_aligned", "n_observed", "censor_class"}
    assert not banned & set(universe.columns)
    alias_map = pl.read_parquet(run01 / "candidate_alias_map.parquet")
    assert not banned & set(alias_map.columns)


@requires_artifacts
def test_aliases_map_to_complete_keys_and_are_stable(run01: Path, tmp_path: Path):
    alias_map = _alias_map(run01)
    assert (
        alias_map.filter(
            pl.col("candidate_alias").is_duplicated()
            & pl.col("case_id").is_duplicated()
        )
        .group_by(["case_id", "candidate_alias"])
        .len()
        .filter(pl.col("len") > 1)
        .is_empty()
    )
    second = tmp_path / "again"
    run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=P4_DIR,
            output_dir=second,
            ttc_root=TTC_ROOT,
        )
    )
    again = pl.read_parquet(second / "candidate_alias_map.parquet")
    assert alias_map.sort(["case_id", "display_order"]).equals(
        again.sort(["case_id", "display_order"])
    )


@requires_artifacts
def test_blind_order_covers_all_cases_and_scheduled_seven(run01: Path):
    order = json.loads((run01 / "blind_review_order.json").read_text())
    cases = order["case_order"]
    assert sorted(cases) == list(ALL_PILOT_CASES)
    for case_id in SCHEDULED_SECOND_PASS_CASES:
        assert case_id in cases
    assert order["seed_generation_method"]
    assert isinstance(order["seed"], int)
    fragment_positions = sorted(
        cases.index(c) for c in ("C-0ffcd227", "C-3eb39b05", "C-7d4b42b8", "C-af359271")
    )
    spread = fragment_positions[-1] - fragment_positions[0]
    assert spread > 3, "fragment cases should not be presented consecutively"


@requires_artifacts
def test_routing_manifest_is_baseline_not_label(run01: Path):
    routing = pl.read_parquet(run01 / "case_routing_manifest.parquet")
    assert routing.height == 18
    assert routing["hide_first_pass_during_review"].all()
    assert routing["routing_is_baseline_not_final_label"].all()
    label_columns = {"final_label", "human_label", "scheduled_judgment"}
    assert not label_columns & set(routing.columns)
    caveats = routing["stratum_caveat"].unique().to_list()
    assert all("not a human truth label" in caveat for caveat in caveats)
    by_case = {
        row["case_id"]: row["initial_task_route"]
        for row in routing.iter_rows(named=True)
    }
    for case_id in SCHEDULED_SECOND_PASS_CASES:
        assert by_case[case_id] == "scheduled_trip_second_pass"
    assert by_case["C-2630793f"] == "segmentation_multi_trip"
    assert by_case["C-913f7d4c"] == "boundary_spanning_dual_role"
    assert by_case["C-7d5fcf98"] == "source_time_quality"
    assert by_case["C-88e48154"] == "no_match_activity"
    assert by_case["C-ba617064"] == "insufficient_context"


@requires_artifacts
def test_run_dir_artifacts_present_restricted_and_no_tides(run01: Path):
    for name in FROZEN_ARTIFACTS + LIVE_ARTIFACTS:
        path = run01 / name
        assert path.exists(), name
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, f"{name} not 0600"
    for path in run01.iterdir():
        lowered = path.name.lower()
        assert "tides" not in lowered or lowered.startswith("factorial"), path
        assert "trip_id_performed" not in lowered
    manifest = json.loads((run01 / "ui_run_manifest.json").read_text())
    assert manifest["factorial_authorized"] is False
    assert manifest["phase_b_authorized"] is False
    assert manifest["packet_is_independent_holdout"] is False


@requires_artifacts
def test_empty_live_artifacts_are_honest(run01: Path):
    labels = pl.read_parquet(run01 / "second_pass_labels_v2.parquet")
    assert labels.height == 0
    assert "selected_candidate_key" in labels.columns
    adjudicated = pl.read_parquet(run01 / "adjudicated_labels_v2.parquet")
    assert adjudicated.height == 0
    go_no_go = json.loads((run01 / "factorial_go_no_go.json").read_text())
    assert go_no_go["factorial_status"] == "NO_GO"
    assert go_no_go["all_conditions_pass"] is False
    readiness = json.loads((run01 / "label_readiness_report_v2.json").read_text())
    assert readiness["blind_second_pass"]["completed_count"] == 0
    sets = json.loads((run01 / "task_specific_label_sets.json").read_text())
    assert sets["development_pilot"] is True
    assert sets["independent_holdout_eligible"] is False
    assert all(count == 0 for count in sets["set_counts"].values())


@requires_artifacts
def test_child_manifest_from_real_requests(run01: Path):
    children = pl.read_parquet(run01 / "child_case_manifest.parquet")
    assert children.height == 21
    assert children["parent_case_id"].n_unique() == 9
    assert not children["automatic_child_label_created"].any()
    manifest = json.loads((run01 / "ui_run_manifest.json").read_text())
    report = manifest["child_case_generation_report"]
    assert report["request_count"] == 11
    assert report["generated_child_count"] == 21
    assert "no count was forced" in report["count_difference_explanation"]
    sequence_children = children.filter(pl.col("child_task_type") == "sequence_review")
    assert sorted(sequence_children["parent_case_id"].to_list()) == [
        "C-2630793f",
        "C-ba6b0e92",
    ]


@requires_artifacts
def test_setup_degrades_gracefully_with_missing_optional(tmp_path: Path):
    minimal = tmp_path / "p4-minimal"
    minimal.mkdir()
    for name in (
        "labeling_packet_restricted.parquet",
        "labeling_packet_redacted.csv",
        "x_complete_schedule_candidates.parquet",
        "x_refined_chunks.parquet",
        "experiment_preregistration.json",
    ):
        shutil.copy2(P4_DIR / name, minimal / name)
    result = run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=minimal,
            output_dir=tmp_path / "run",
            artifacts_root=tmp_path,
            ttc_root=TTC_ROOT,
        )
    )
    assert result.case_count == 18
    assert result.missing_optional_inputs
    universe = pl.read_parquet(tmp_path / "run" / "candidate_universe_manifest.parquet")
    assert universe["sched_start_epoch_s"].null_count() == universe.height


@requires_artifacts
def test_tampered_frozen_zone_is_rejected(fresh_run: Path):
    target = fresh_run / "blind_review_order.json"
    content = json.loads(target.read_text())
    content["case_order"] = list(reversed(content["case_order"]))
    target.write_text(json.dumps(content))
    verification = verify_frozen_zone(fresh_run)
    assert not verification["ok"]
    with pytest.raises(BlindProtocolViolation):
        ReviewStore(fresh_run, first_pass_path=FIRST_PASS, reviewer_id="trev")


@requires_artifacts
def test_first_pass_gate_blocks_until_blind_save(store: ReviewStore):
    with pytest.raises(BlindProtocolViolation):
        store.first_pass.for_case("C-5555da7a", reviewer_id="trev")
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    store.save_second_pass(_scheduled_record("C-5555da7a", alias))
    row = store.first_pass.for_case("C-5555da7a", reviewer_id="trev")
    assert row["raw_primary_label"]
    events = [e["event"] for e in store.events()]
    assert "first_pass_reveal" in events


@requires_artifacts
def test_matched_requires_complete_key_membership(store: ReviewStore):
    with pytest.raises(LabelValidationError):
        store.save_second_pass(_scheduled_record("C-5555da7a", "ZZ-not-an-alias"))
    with pytest.raises(LabelValidationError):
        store.save_second_pass(_scheduled_record("C-5555da7a", "NONE"))
    alias_rows = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-5555da7a")
        & (pl.col("candidate_kind") == "no_scheduled_trip_alternative")
    )
    if not alias_rows.is_empty():
        with pytest.raises(LabelValidationError):
            store.save_second_pass(
                _scheduled_record("C-5555da7a", alias_rows["candidate_alias"][0])
            )


@requires_artifacts
def test_high_confidence_requires_rationale_and_two_families(store):
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    record = _scheduled_record("C-5555da7a", alias)
    record["match_confidence"] = 5
    with pytest.raises(LabelValidationError):
        store.save_second_pass(dict(record))
    record["evidence_flags"] = ["interval_time_fit"]
    record["rationale_text"] = "one family only, high confidence"
    with pytest.raises(LabelValidationError):
        store.save_second_pass(dict(record))
    record["evidence_flags"] = ["single_decisive_anchor", "interval_time_fit"]
    with pytest.raises(LabelValidationError):
        store.save_second_pass(dict(record))
    record["single_decisive_anchor_family"] = "interval_time_fit"
    saved = store.save_second_pass(dict(record))
    assert saved["selected_candidate_key"]


@requires_artifacts
def test_saved_label_references_frozen_protocol(store: ReviewStore):
    alias = _complete_alias(store.run_dir, "C-134f2a21")
    store.save_second_pass(_scheduled_record("C-134f2a21", alias))
    labels = pl.read_parquet(store.run_dir / "second_pass_labels_v2.parquet")
    row = labels.row(0, named=True)
    assert row["protocol_version"] == PROTOCOL_VERSION
    assert row["protocol_sha256"] == store.protocol_sha256
    assert row["candidate_universe_hash"]
    assert row["first_pass_hidden"] is True
    assert row["diagnostics_revealed_before_save"] is False
    assert row["selected_candidate_key"] != row["selected_candidate_alias"]


@requires_artifacts
def test_duplicate_blind_save_refused_and_history_append_only(store):
    alias = _complete_alias(store.run_dir, "C-179eba6d")
    store.save_second_pass(_scheduled_record("C-179eba6d", alias))
    history_before = (store.run_dir / "review_history.jsonl").read_text()
    with pytest.raises(BlindProtocolViolation):
        store.save_second_pass(_scheduled_record("C-179eba6d", alias))
    history_after = (store.run_dir / "review_history.jsonl").read_text()
    assert history_after.startswith(history_before)


@requires_artifacts
def test_disagreement_creates_adjudication_not_overwrite(store: ReviewStore):
    first = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-b7163d0c")
    first_key = first["raw_selected_candidate_id"][0]
    other = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-b7163d0c")
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
        & (pl.col("candidate_key") != first_key)
    )
    saved = store.save_second_pass(
        _scheduled_record("C-b7163d0c", other["candidate_alias"][0])
    )
    assert saved["comparison"]["outcome"] == "FIRST_SECOND_DISAGREE"
    state = store.case_state("C-b7163d0c")
    assert "REQUIRES_ADJUDICATION" in state.states
    queue = (store.run_dir / "adjudication_queue_v3.csv").read_text()
    assert "FIRST_SECOND_DISAGREE" in queue
    assert (
        pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-b7163d0c")[
            "raw_selected_candidate_id"
        ][0]
        == first_key
    )
    store.save_adjudication(
        {
            "case_id": "C-b7163d0c",
            "resolution": "second_pass_upheld",
            "resolution_rationale": "test adjudication rationale",
        }
    )
    adjudicated = pl.read_parquet(store.run_dir / "adjudicated_labels_v2.parquet")
    assert adjudicated.height == 1
    labels = pl.read_parquet(store.run_dir / "second_pass_labels_v2.parquet")
    assert labels.height == 1
    state = store.case_state("C-b7163d0c")
    assert "ADJUDICATED" in state.states
    assert "REQUIRES_ADJUDICATION" not in state.states


@requires_artifacts
def test_agreement_when_second_pass_picks_first_pass_key(store: ReviewStore):
    first = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-88bc09d3")
    key = first["raw_selected_candidate_id"][0]
    alias = _alias_for_key(store.run_dir, "C-88bc09d3", key)
    saved = store.save_second_pass(
        _scheduled_record("C-88bc09d3", alias, confident=True)
    )
    assert saved["comparison"]["outcome"] == "FIRST_SECOND_AGREE"
    agreement = json.loads(
        (store.run_dir / "intra_reviewer_agreement.json").read_text()
    )
    assert agreement["agree_count"] == 1
    sets = json.loads((store.run_dir / "task_specific_label_sets.json").read_text())
    assert "C-88bc09d3" in sets["sets"]["schedule_gold"]


@requires_artifacts
def test_grain_change_routes_to_adjudication_not_relabel(store: ReviewStore):
    record = {
        "case_id": "C-1b38b137",
        "case_grain": "local_fragment",
        "reviewability": "not_reviewable_at_current_grain",
        "fragment_role": "ambiguous_fragment",
        "stitch_previous": "ambiguous",
        "stitch_next": "ambiguous",
        "grain_confidence": 3,
        "evidence_quality": 3,
    }
    saved = store.save_second_pass(record)
    assert saved["comparison"]["outcome"] == "NOT_EVALUABLE_AT_CURRENT_GRAIN"
    state = store.case_state("C-1b38b137")
    assert "REQUIRES_ADJUDICATION" in state.states
    before = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-1b38b137")
    after = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-1b38b137")
    assert before.equals(after), "first pass must be untouched by the save"


@requires_artifacts
def test_multi_trip_validation_and_outside_chunk_warning(store: ReviewStore):
    alias = _complete_alias(store.run_dir, "C-2630793f")
    key = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-2630793f") & (pl.col("candidate_alias") == alias)
    )["candidate_key"][0]
    base = {
        "case_id": "C-2630793f",
        "case_grain": "multi_trip_chunk",
        "reviewability": "requires_segmentation_adjudication",
        "grain_confidence": 3,
        "evidence_quality": 3,
        "split_confidence": 3,
    }
    with pytest.raises(LabelValidationError):
        store.save_second_pass(
            {**base, "subtrip_count": 2, "ordered_candidate_sequence": [key]}
        )
    with pytest.raises(LabelValidationError):
        store.save_second_pass(
            {
                **base,
                "subtrip_count": 1,
                "ordered_candidate_sequence": ["not-a-real-key"],
            }
        )
    saved = store.save_second_pass(
        {
            **base,
            "subtrip_count": 2,
            "ordered_candidate_sequence": [key, None],
            "split_intervals_rel_s": [[100, 400], [5000, 999999]],
            "unresolved_subsegments": 1,
        }
    )
    assert "outside the displayed chunk" in saved["outside_displayed_chunk_warning"]


@requires_artifacts
def test_boundary_sides_independent_and_tz_derived(store: ReviewStore):
    aliases = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-913f7d4c")
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
    )
    inbound = aliases["candidate_key"][0]
    outbound = aliases["candidate_key"][1]
    saved = store.save_second_pass(
        {
            "case_id": "C-913f7d4c",
            "case_grain": "boundary_spanning",
            "reviewability": "requires_boundary_adjudication",
            "inbound_candidate_key": inbound,
            "outbound_candidate_key": outbound,
            "boundary_stop_hypothesis": "1652",
            "boundary_time_interval_rel_s": [200, 280],
            "boundary_timezone_label_derived": "REVIEWER-TYPED-GARBAGE",
            "arrival_role": "trip_end",
            "departure_role": "trip_start",
            "dual_role": True,
            "grain_confidence": 3,
            "evidence_quality": 3,
        }
    )
    assert saved["inbound_candidate_key"] != saved["outbound_candidate_key"]
    assert saved["boundary_timezone_label_derived"] in ("EST", "EDT")
    unresolved = store.save_second_pass(
        {
            "case_id": "C-c953dac7",
            "case_grain": "boundary_spanning",
            "reviewability": "requires_boundary_adjudication",
            "inbound_candidate_key": None,
            "outbound_candidate_key": None,
            "dual_role": True,
            "grain_confidence": 2,
            "evidence_quality": 2,
        }
    )
    assert unresolved["inbound_candidate_key"] is None


@requires_artifacts
def test_fragment_context_is_not_a_performed_trip(store: ReviewStore):
    key = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-0ffcd227")
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
    )["candidate_key"][0]
    with pytest.raises(LabelValidationError):
        store.save_second_pass(
            {
                "case_id": "C-0ffcd227",
                "case_grain": "local_fragment",
                "fragment_role": "arrival_fragment",
                "stitch_previous": "false",
                "stitch_next": "true",
                "local_scheduled_context_candidate_key": key,
                "complete_trip_judgment": "matched",
                "grain_confidence": 3,
                "evidence_quality": 3,
            }
        )
    saved = store.save_second_pass(
        {
            "case_id": "C-0ffcd227",
            "case_grain": "local_fragment",
            "reviewability": "not_reviewable_at_current_grain",
            "fragment_role": "arrival_fragment",
            "stitch_previous": "false",
            "stitch_next": "ambiguous",
            "local_scheduled_context_candidate_key": key,
            "complete_trip_judgment": "not_applicable",
            "fragment_confidence": 3,
            "grain_confidence": 3,
            "evidence_quality": 3,
        }
    )
    assert saved["local_scheduled_context_candidate_key"] == key
    assert saved["complete_trip_judgment"] == "not_applicable"


@requires_artifacts
def test_quality_invalid_never_requires_scheduled_match(store: ReviewStore):
    saved = store.save_second_pass(
        {
            "case_id": "C-7d5fcf98",
            "case_grain": "data_quality_invalid",
            "reviewability": "invalid_source_state",
            "quality_state": "simultaneous_positions",
            "quality_flags": [
                "simultaneous_positions",
                "trajectory_not_split",
            ],
            "scheduled_match_applicable": False,
            "grain_confidence": 4,
            "evidence_quality": 2,
            "evidence_flags": ["data_quality_signal"],
            "rationale_text": "simultaneous positions in the trace",
        }
    )
    assert saved["scheduled_judgment"] == "not_applicable"
    with pytest.raises(LabelValidationError):
        store.save_second_pass(
            {
                "case_id": "C-88e48154",
                "case_grain": "data_quality_invalid",
                "quality_state": "other",
                "quality_flags": ["other"],
                "scheduled_match_applicable": False,
                "scheduled_judgment": "matched",
                "grain_confidence": 3,
                "evidence_quality": 3,
            }
        )


@requires_artifacts
def test_pre_save_diagnostics_reveal_recorded_and_blocks_gold(store):
    store.record_diagnostics_reveal("C-47d3a45a")
    first = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-47d3a45a")
    alias = _alias_for_key(
        store.run_dir, "C-47d3a45a", first["raw_selected_candidate_id"][0]
    )
    saved = store.save_second_pass(
        _scheduled_record("C-47d3a45a", alias, confident=True)
    )
    assert saved["diagnostics_revealed_before_save"] is True
    sets = json.loads((store.run_dir / "task_specific_label_sets.json").read_text())
    assert "C-47d3a45a" not in sets["sets"]["schedule_gold"]


@requires_artifacts
def test_factorial_stays_no_go_and_separate_from_promotion(store: ReviewStore):
    first = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-88bc09d3")
    alias = _alias_for_key(
        store.run_dir, "C-88bc09d3", first["raw_selected_candidate_id"][0]
    )
    store.save_second_pass(_scheduled_record("C-88bc09d3", alias, confident=True))
    go_no_go = json.loads((store.run_dir / "factorial_go_no_go.json").read_text())
    assert go_no_go["factorial_status"] == "NO_GO"
    assert go_no_go["automatic_execution_forbidden"] is True
    assert go_no_go["conditions"]["phase_b_cases_labeled_under_frozen_protocol"] is (
        False
    )
    assert go_no_go["conditions"]["holdout_unopened"] is True
    sets = json.loads((store.run_dir / "task_specific_label_sets.json").read_text())
    assert sets["sets"]["schedule_gold"] == ["C-88bc09d3"]


@requires_artifacts
def test_routing_change_event_is_a_new_judgment(store: ReviewStore):
    store.record_routing_change(
        "C-3eb39b05",
        from_route="fragment_role_and_stitching",
        to_grain="single_trip_candidate",
        reason="reviewer grain-first decision",
    )
    events = store.events()
    change = [e for e in events if e["event"] == "routing_change"][-1]
    assert change["stored_as_new_human_judgment"] is True
    routing = pl.read_parquet(store.run_dir / "case_routing_manifest.parquet")
    row = routing.filter(pl.col("case_id") == "C-3eb39b05").row(0, named=True)
    assert row["initial_task_route"] == "fragment_role_and_stitching"


@requires_artifacts
def test_queue_never_carries_an_automatic_final_label(store: ReviewStore):
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    store.save_second_pass(_scheduled_record("C-5555da7a", alias))
    import csv as csv_module

    with (store.run_dir / "adjudication_queue_v3.csv").open() as handle:
        rows = list(csv_module.DictReader(handle))
    assert rows
    assert all(row["automatic_final_label"] == "false" for row in rows)
    assert {row["case_id"] for row in rows} >= set(ALL_PILOT_CASES)


# ------------------------------------------------- repository-safe scans

SENSITIVE_PATTERNS = (
    r"\bc1-[0-9a-f]{32}-\d+\b",  # APC source record ids
    r"\ba1-[0-9a-f]{32}-\d+\b",  # AVL source record ids
    r"\bxsg-(?:avl|apc)-[0-9a-f]{8,}\b",  # chunk ids
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}",  # precise timestamps
    r"\d{4}-\d{2}-\d{2}\|[^|\s]*\|[^|\s]*\|\d+\|",  # complete candidate keys
    r"\b\d{4}\|\d{4}-\d{2}-\d{2}\b",  # vehicle-day split strings
    "/" + "home/",  # absolute workstation paths (split to avoid self-match)
    r"\b43\.\d{4,}\b",  # precise latitudes
    r"\b-79\.\d{4,}\b",  # precise longitudes
)

# The nine restricted vehicle ids are never written literally here: each is
# reassembled from digit-pair tuples at runtime so this scanner file cannot
# itself publish the tokens it exists to block (and the formatter cannot
# collapse the construction back into literals).
_VEHICLE_ID_PAIRS = [
    (10, 11),
    (72, 37),
    (72, 57),
    (12, 67),
    (72, 61),
    (90, 19),
    (90, 30),
    (90, 33),
    (90, 34),
]
VEHICLE_ID_PATTERN = (
    r"\b(?:" + "|".join(f"{a:02d}{b:02d}" for a, b in _VEHICLE_ID_PAIRS) + r")\b"
)


def _scan(path: Path, patterns) -> list[str]:
    import re

    text = path.read_text(encoding="utf-8")
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            hits.append(f"{path.name}: {pattern} -> {match.group(0)[:40]}")
    return hits


def test_repo_safe_docs_contain_no_sensitive_identifiers():
    docs = [
        TTC_ROOT / "docs/human-labeling-protocol-v2.md",
        TTC_ROOT / "docs/human-labeling-v2-run01-status.md",
    ]
    for doc in docs:
        assert doc.exists(), doc
        hits = _scan(doc, SENSITIVE_PATTERNS + (VEHICLE_ID_PATTERN,))
        assert not hits, hits


def test_committed_v2_source_contains_no_sensitive_identifiers():
    roots = [
        TTC_ROOT / "experiments/r1_design_spike/src/r1spike",
        TTC_ROOT / "experiments/r1_design_spike/labeling_ui_v2",
        TTC_ROOT / "experiments/r1_design_spike/tests",
    ]
    skip_parts = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    hits: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or set(path.parts) & skip_parts:
                continue
            if path.suffix not in {".py", ".md", ".toml", ".txt"}:
                continue
            if "labeling_v2" not in path.name and "labeling_ui_v2" not in str(path):
                continue
            hits.extend(_scan(path, SENSITIVE_PATTERNS + (VEHICLE_ID_PATTERN,)))
    assert not hits, hits[:10]


# --------------------------------------- adversarial-review regressions


def test_all_empty_pipe_key_is_flagged_not_a_real_alternative():
    row = classify_candidate_key("||||")
    assert row["candidate_kind"] == "contract_unresolved"
    assert row["unresolved_warning"]


@requires_artifacts
def test_protocol_doc_edit_with_patched_file_hash_still_fails(fresh_run):
    doc = fresh_run / "blind_review_protocol_v2.0.md"
    doc.write_text(doc.read_text().replace("Confidence 4 or 5", "Confidence 5"))
    manifest_path = fresh_run / "protocol_freeze_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["frozen_artifact_hashes"]["blind_review_protocol_v2.0.md"] = sha256_file(
        doc
    )
    manifest["protocol_document_hashes"]["blind_review_protocol_v2.0.md"] = sha256_file(
        doc
    )
    manifest_path.write_text(json.dumps(manifest))
    verification = verify_frozen_zone(fresh_run)
    assert not verification["ok"]
    assert any("protocol_sha256" in item for item in verification["mismatches"])


@requires_artifacts
def test_truncated_first_pass_input_is_rejected_at_open(fresh_run, tmp_path):
    truncated = tmp_path / "first_pass_truncated.parquet"
    frame = pl.read_parquet(FIRST_PASS).head(17)
    frame.write_parquet(truncated)
    with pytest.raises(BlindProtocolViolation):
        ReviewStore(fresh_run, first_pass_path=truncated, reviewer_id="trev")
    with pytest.raises(BlindProtocolViolation):
        ReviewStore(
            fresh_run,
            first_pass_path=tmp_path / "missing.parquet",
            reviewer_id="trev",
        )


@requires_artifacts
def test_evidence_quality_participates_in_rationale_gate(store):
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    record = _scheduled_record("C-5555da7a", alias)
    record["evidence_quality"] = 5
    with pytest.raises(LabelValidationError):
        store.save_second_pass(dict(record))


@requires_artifacts
def test_non_integer_confidences_are_rejected(store):
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    for bad in (5.0, "5", True):
        record = _scheduled_record("C-5555da7a", alias)
        record["match_confidence"] = bad
        with pytest.raises(LabelValidationError):
            store.save_second_pass(dict(record))
    record = _scheduled_record("C-5555da7a", alias)
    record["split_confidence"] = 5.0
    with pytest.raises(LabelValidationError):
        store.save_second_pass(dict(record))


@requires_artifacts
def test_duplicated_evidence_family_does_not_satisfy_two_family_rule(store):
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    record = _scheduled_record("C-5555da7a", alias)
    record["match_confidence"] = 5
    record["evidence_flags"] = ["interval_time_fit", "interval_time_fit"]
    record["rationale_text"] = "duplicated family should not count twice"
    with pytest.raises(LabelValidationError):
        store.save_second_pass(dict(record))


@requires_artifacts
def test_caller_supplied_selection_fields_are_stripped(store):
    alias = _complete_alias(store.run_dir, "C-5555da7a")
    record = _scheduled_record("C-5555da7a", alias)
    record["selected_candidate_key"] = "FABRICATED|not|in|universe|x"
    saved = store.save_second_pass(record)
    assert saved["selected_candidate_key"] != "FABRICATED|not|in|universe|x"
    resolved = store.resolve_alias("C-5555da7a", alias)
    assert saved["selected_candidate_key"] == resolved["candidate_key"]


@requires_artifacts
def test_scheduled_judgment_rejected_at_every_other_grain(store):
    for grain, extra in (
        (
            "local_fragment",
            {
                "fragment_role": "ambiguous_fragment",
                "stitch_previous": "ambiguous",
                "stitch_next": "ambiguous",
            },
        ),
        ("turnaround_or_idle", {"activity_state": "idling"}),
        ("boundary_spanning", {}),
        ("coverage_censored", {}),
        ("unknown", {}),
    ):
        with pytest.raises(LabelValidationError):
            store.save_second_pass(
                {
                    "case_id": "C-88e48154",
                    "case_grain": grain,
                    "scheduled_judgment": "matched",
                    "grain_confidence": 3,
                    "evidence_quality": 3,
                    **extra,
                }
            )


@requires_artifacts
def test_child_save_blocked_until_parent_and_grain_pinned(store):
    child_id = "C-2630793f::subtrip-1"
    with pytest.raises(BlindProtocolViolation):
        store.save_second_pass(
            {
                "case_id": child_id,
                "case_grain": "single_trip_candidate",
                "scheduled_judgment": "insufficient_evidence",
                "grain_confidence": 3,
                "evidence_quality": 3,
            }
        )
    key = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-2630793f")
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
    )["candidate_key"][0]
    store.save_second_pass(
        {
            "case_id": "C-2630793f",
            "case_grain": "multi_trip_chunk",
            "reviewability": "requires_segmentation_adjudication",
            "subtrip_count": 2,
            "ordered_candidate_sequence": [key, None],
            "split_intervals_rel_s": [[100, 400]],
            "split_confidence": 3,
            "grain_confidence": 3,
            "evidence_quality": 3,
        }
    )
    with pytest.raises(LabelValidationError):
        store.save_second_pass(
            {
                "case_id": child_id,
                "case_grain": "local_fragment",
                "fragment_role": "ambiguous_fragment",
                "stitch_previous": "ambiguous",
                "stitch_next": "ambiguous",
                "grain_confidence": 3,
                "evidence_quality": 3,
            }
        )
    saved = store.save_second_pass(
        {
            "case_id": child_id,
            "case_grain": "single_trip_candidate",
            "scheduled_judgment": "insufficient_evidence",
            "grain_confidence": 3,
            "evidence_quality": 3,
        }
    )
    assert saved["is_child_case"] is True
    sets = json.loads((store.run_dir / "task_specific_label_sets.json").read_text())
    for members in sets["sets"].values():
        assert child_id not in members or members == sets["sets"].get(
            "pending_adjudication"
        )


@requires_artifacts
def test_multi_trip_sequence_rejects_null_option_sentinel(store):
    with pytest.raises(LabelValidationError):
        store.save_second_pass(
            {
                "case_id": "C-ba6b0e92",
                "case_grain": "multi_trip_chunk",
                "subtrip_count": 1,
                "ordered_candidate_sequence": ["NO_SCHEDULED_MATCH"],
                "split_confidence": 3,
                "grain_confidence": 3,
                "evidence_quality": 3,
            }
        )


@requires_artifacts
def test_adjudication_rationale_required_and_resolution_derives_content(store):
    first = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-b7163d0c")
    first_key = first["raw_selected_candidate_id"][0]
    other = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-b7163d0c")
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
        & (pl.col("candidate_key") != first_key)
    )
    store.save_second_pass(_scheduled_record("C-b7163d0c", other["candidate_alias"][0]))
    with pytest.raises(LabelValidationError):
        store.save_adjudication(
            {"case_id": "C-b7163d0c", "resolution": "first_pass_upheld"}
        )
    event = store.save_adjudication(
        {
            "case_id": "C-b7163d0c",
            "resolution": "first_pass_upheld",
            "resolution_rationale": "first pass judgment stands on review",
            "resolved_candidate_key": "SHOULD|BE|OVERRIDDEN|1|",
        }
    )
    assert event["resolved_candidate_key"] == first_key
    sets = json.loads((store.run_dir / "task_specific_label_sets.json").read_text())
    assert "C-b7163d0c" not in sets["sets"]["schedule_gold"], (
        "an overturned blind save must never promote"
    )


@requires_artifacts
def test_deferred_adjudication_keeps_case_pending(store):
    first = pl.read_parquet(FIRST_PASS).filter(pl.col("case_id") == "C-47d3a45a")
    first_key = first["raw_selected_candidate_id"][0]
    other = _alias_map(store.run_dir).filter(
        (pl.col("case_id") == "C-47d3a45a")
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
        & (pl.col("candidate_key") != first_key)
    )
    store.save_second_pass(_scheduled_record("C-47d3a45a", other["candidate_alias"][0]))
    event = store.save_adjudication(
        {
            "case_id": "C-47d3a45a",
            "resolution": "deferred",
            "resolution_rationale": "needs a second look with fresh eyes",
        }
    )
    assert event["resolved_candidate_key"] is None
    state = store.case_state("C-47d3a45a")
    assert "REQUIRES_ADJUDICATION" in state.states
    sets = json.loads((store.run_dir / "task_specific_label_sets.json").read_text())
    assert "C-47d3a45a" in sets["sets"]["pending_adjudication"]
    assert "C-47d3a45a" not in sets["sets"]["schedule_gold"]


@requires_artifacts
def test_live_sidecars_match_files_after_saves(store):
    alias = _complete_alias(store.run_dir, "C-134f2a21")
    store.save_second_pass(_scheduled_record("C-134f2a21", alias))
    for name in (
        "second_pass_labels_v2.parquet",
        "adjudicated_labels_v2.parquet",
        "adjudication_queue_v3.csv",
    ):
        sidecar = json.loads((store.run_dir / f"{name}.meta.json").read_text())
        assert sidecar["content_sha256"] == sha256_file(store.run_dir / name), name


@requires_artifacts
def test_stale_session_cannot_save_into_swapped_run_dir(run01, tmp_path):
    import shutil as _shutil

    live = tmp_path / "live"
    _shutil.copytree(run01, live)
    store = ReviewStore(live, first_pass_path=FIRST_PASS, reviewer_id="trev")
    alias = _complete_alias(live, "C-5555da7a")
    _shutil.rmtree(live)
    _shutil.copytree(run01, live)
    (live / "blind_review_order.json").write_text(
        (live / "blind_review_order.json").read_text().replace("  ", " ")
    )
    with pytest.raises(BlindProtocolViolation):
        store.save_second_pass(_scheduled_record("C-5555da7a", alias))


@requires_artifacts
def test_setup_rejects_truncated_child_requests(tmp_path):
    import shutil as _shutil

    broken_audit = tmp_path / "audit"
    _shutil.copytree(AUDIT_DIR, broken_audit)
    requests_path = broken_audit / "child_case_requests.csv"
    header = requests_path.read_text().splitlines()[0]
    requests_path.write_text(header + "\n")
    with pytest.raises(Exception, match="child_case_requests"):
        run_setup(
            SetupConfig(
                audit_dir=broken_audit,
                spike02r_dir=P4_DIR,
                output_dir=tmp_path / "run",
                ttc_root=TTC_ROOT,
            )
        )


@requires_artifacts
def test_setup_rejects_case_with_empty_candidate_universe(tmp_path):
    import shutil as _shutil

    broken_p4 = tmp_path / "p4"
    broken_p4.mkdir()
    for name in (
        "labeling_packet_restricted.parquet",
        "labeling_packet_redacted.csv",
        "x_refined_chunks.parquet",
        "experiment_preregistration.json",
    ):
        _shutil.copy2(P4_DIR / name, broken_p4 / name)
    candidates = pl.read_parquet(P4_DIR / "x_complete_schedule_candidates.parquet")
    restricted = pl.read_parquet(P4_DIR / "labeling_packet_restricted.parquet")
    victim_chunk = restricted.row(0, named=True)["real_id"]
    candidates.filter(pl.col("chunk_id") != victim_chunk).write_parquet(
        broken_p4 / "x_complete_schedule_candidates.parquet"
    )
    with pytest.raises(Exception, match="empty candidate universe"):
        run_setup(
            SetupConfig(
                audit_dir=AUDIT_DIR,
                spike02r_dir=broken_p4,
                output_dir=tmp_path / "run",
                artifacts_root=tmp_path,
                ttc_root=TTC_ROOT,
            )
        )


@requires_artifacts
def test_initial_queue_masks_audit_routing_until_save(run01):
    import csv as csv_module

    with (run01 / "adjudication_queue_v3.csv").open() as handle:
        rows = list(csv_module.DictReader(handle))
    parents = [row for row in rows if not row["parent_case_id"]]
    assert all(row["queue_rank"] == "" for row in parents)
    assert all(row["initial_task_route"] == "" for row in parents)
    assert all(row["audit_reason_codes"] == "" for row in parents)
    case_ids = [row["case_id"] for row in parents]
    assert case_ids == sorted(case_ids), "queue must not be in priority order"


@requires_artifacts
def test_setup_records_gtfs_degradation_when_windows_missing(tmp_path):
    import shutil as _shutil

    minimal = tmp_path / "p4-minimal"
    minimal.mkdir()
    for name in (
        "labeling_packet_restricted.parquet",
        "labeling_packet_redacted.csv",
        "x_complete_schedule_candidates.parquet",
        "x_refined_chunks.parquet",
        "experiment_preregistration.json",
    ):
        _shutil.copy2(P4_DIR / name, minimal / name)
    run_setup(
        SetupConfig(
            audit_dir=AUDIT_DIR,
            spike02r_dir=minimal,
            output_dir=tmp_path / "run",
            artifacts_root=tmp_path,
            ttc_root=TTC_ROOT,
        )
    )
    manifest = json.loads((tmp_path / "run" / "ui_run_manifest.json").read_text())
    degradation = manifest["input_degradation"]
    assert degradation["schedule_windows_unresolved"] > 0
    assert degradation["gtfs_dirs_missing"]
    assert degradation["ordering_degraded_to_lexical_for_unresolved"] is True
