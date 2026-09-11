"""Storage/config/stage-interface contracts independent of historical outcomes."""

import json

import pytest

from tools.experiment05 import STAGES
from tools.experiment05.config import DEFAULT_CONFIG, load_config
from tools.experiment05.io import (
    concatenate_rows,
    read_json,
    read_rows,
    stable_id,
    write_json,
    write_rows,
)
from tools.experiment05.pipeline import (
    STAGE_OUTPUTS,
    _stage_output_pins,
    _validate_modes,
)


def test_every_stage_has_explicit_owned_output_interface():
    assert tuple(STAGE_OUTPUTS) == STAGES
    assert STAGES.index("project_avl") < STAGES.index("segment_trips")
    assert STAGES.index("generate_candidates") < STAGES.index("assign_scheduled")


@pytest.mark.parametrize("value", ["0", "2.5", '"8"', "true", "-1"])
def test_workers_must_be_positive_integer(tmp_path, value):
    path = tmp_path / "bad.toml"
    path.write_text(
        DEFAULT_CONFIG.read_text().replace("workers = 8", "workers = " + value)
    )
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize(
    "old,new",
    [
        ("atomic_window_s = 60.0", "atomic_window_s = 0.0"),
        ("minimum_window_points = 2", "minimum_window_points = true"),
        ("cache_success_only = true", "cache_success_only = false"),
        ("include_vehicles = true", "include_vehicles = false"),
        ("include_devices = false", 'include_devices = "false"'),
        (
            'match_modes = ["global", "early", "middle", "late"]',
            'match_modes = ["global", "late"]',
        ),
    ],
)
def test_config_rejects_invalid_threshold_and_policy_types(tmp_path, old, new):
    path = tmp_path / "bad.toml"
    path.write_text(DEFAULT_CONFIG.read_text().replace(old, new))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("policy", ["invented", "", None, True, 1, []])
def test_passenger_event_policy_is_explicit_and_validated(tmp_path, policy):
    path = tmp_path / "policy.toml"
    replacement = (
        "" if policy is None else ("passenger_event_policy = " + json.dumps(policy))
    )
    path.write_text(
        DEFAULT_CONFIG.read_text().replace(
            'passenger_event_policy = "apc_stop_interval_departure_proxy_v1"',
            replacement,
        )
    )
    with pytest.raises(ValueError, match="passenger_event_policy"):
        load_config(path)


def test_conservative_passenger_event_policy_remains_supported(tmp_path):
    path = tmp_path / "conservative.toml"
    path.write_text(
        DEFAULT_CONFIG.read_text().replace(
            'passenger_event_policy = "apc_stop_interval_departure_proxy_v1"',
            'passenger_event_policy = "omit_without_event_semantics"',
        )
    )
    assert load_config(path)[0]["export"]["passenger_event_policy"] == (
        "omit_without_event_semantics"
    )


def test_nested_stage_tables_roundtrip_and_hash_validation(tmp_path):
    rows = [
        {
            "rid": "source:a:7",
            "t": 1.25,
            "trip_id_performed": None,
            "details": {"a": [1, None, True]},
            "source_row": 7,
        },
        {
            "rid": "source:a:8",
            "t": 2.5,
            "details": {},
            "new_field": "value",
            "source_row": 8,
        },
    ]
    path = tmp_path / "rows.parquet"
    assert write_rows(path, rows) == 2
    decoded = read_rows(path)
    assert decoded[0]["details"] == rows[0]["details"]
    assert decoded[0]["trip_id_performed"] is None
    assert decoded[1]["new_field"] == "value"
    assert decoded[1]["trip_id_performed"] is None
    descriptor = json.loads(path.with_suffix(".schema.json").read_text())
    assert descriptor["json_columns"] == ["details"]
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="hash changed"):
        read_rows(path)


def test_empty_table_and_existing_stage_output_pins(tmp_path):
    (tmp_path / "stages/candidate_shards").mkdir(parents=True)
    path = tmp_path / "candidate_rankings.parquet"
    write_rows(path, [], columns=("trip_id_performed", "candidate_rank"))
    assert read_rows(path) == []
    # Retry inventory includes all owned files, not just files created this call.
    for name in ("local_window_evidence", "mode_results", "scheduled_lookup"):
        write_rows(tmp_path / f"{name}.parquet", [], columns=("id",))
    first = _stage_output_pins(tmp_path, "generate_candidates")
    second = _stage_output_pins(tmp_path, "generate_candidates")
    assert first == second and len(first) == 8


def test_streaming_shards_preserve_order_nullable_union_and_nested_values(tmp_path):
    paths = [tmp_path / f"shard-{index}.parquet" for index in range(4)]
    write_rows(paths[0], [], columns=("candidate_rank",))
    write_rows(paths[1], [{"id": "a", "candidate_rank": 1, "detail": "plain"}])
    write_rows(paths[2], [{"id": "b", "candidate_rank": 2, "detail": {"x": [1]}}])
    write_rows(paths[3], [{"id": "c", "candidate_rank": 3, "new": True}])
    target = tmp_path / "combined.parquet"
    assert concatenate_rows(target, paths) == 3
    rows = read_rows(target)
    assert [row["id"] for row in rows] == ["a", "b", "c"]
    assert [row["candidate_rank"] for row in rows] == [1, 2, 3]
    assert rows[0]["detail"] == "plain"
    assert rows[1]["detail"] == {"x": [1]}
    assert rows[2]["detail"] is None and rows[2]["new"] is True
    assert rows[0]["new"] is None
    paths[1].write_bytes(paths[1].read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="shard hash changed"):
        concatenate_rows(tmp_path / "rejected.parquet", paths)


def test_streaming_zero_rows_has_explicit_empty_contract(tmp_path):
    path = tmp_path / "zero.parquet"
    write_rows(path, [], columns=("id",))
    target = tmp_path / "all-empty.parquet"
    assert concatenate_rows(target, [path], columns=("id",)) == 0
    assert read_rows(target) == []


def test_candidate_pool_submissions_are_bounded_and_result_descriptors_sorted(
    monkeypatch,
):
    from tools.experiment05 import pipeline

    pending = set()
    maximum = 0

    class Future:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    class Executor:
        def __init__(self, **kwargs):
            self.workers = kwargs["max_workers"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, worker, job):
            nonlocal maximum
            pending.add(job)
            maximum = max(maximum, len(pending))
            assert len(pending) <= self.workers
            return Future(worker(job))

    def consume(result):
        pending.remove(result[0])
        return result[0], "stored-descriptor"

    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", Executor)
    monkeypatch.setattr(
        pipeline, "wait", lambda futures, **kwargs: ({next(iter(futures))}, set())
    )
    result = pipeline._parallel(
        list(reversed(range(17))),
        lambda job: (job, "large-payload"),
        {},
        {"execution": {"workers": 3}},
        ["global"],
        lambda **kwargs: None,
        consume=consume,
    )
    assert maximum == 3 and not pending
    assert result == [(index, "stored-descriptor") for index in range(17)]


def test_mode_names_and_identity_are_not_input_order_dependent():
    assert _validate_modes(["late", "global"]) == ["global", "late"]
    with pytest.raises(ValueError):
        _validate_modes(["global", "global"])
    with pytest.raises(ValueError):
        _validate_modes(["front"])
    assert stable_id("performed", {"b": 2, "a": 1}) == stable_id(
        "performed", {"a": 1, "b": 2}
    )
    assert stable_id("performed", "2024-01-01", "a") != stable_id(
        "performed", "2024-01-02", "a"
    )


def test_interrupted_old_metadata_temp_does_not_block_new_atomic_write(tmp_path):
    path = tmp_path / "run_manifest.json"
    orphan = tmp_path / "run_manifest.json.writing"
    orphan.write_text("interrupted diagnostic")
    write_json(path, {"stage": "before"})
    write_json(path, {"stage": "after"})
    assert read_json(path) == {"stage": "after"}
    assert orphan.read_text() == "interrupted diagnostic"


def test_io_refuses_ancestor_symlink_before_writing(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "alias"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        write_json(linked / "manifest.json", {})
    with pytest.raises(ValueError, match="Symlink"):
        write_rows(linked / "rows.parquet", [{"id": 1}])
    assert list(actual.iterdir()) == []
