"""Visualization is a hash-verified read model, never a second matching engine."""

import copy
import json
import threading
from urllib.parse import urlencode

import pytest

from tests.test_experiment05_web import request
from tools.experiment05.config import DEFAULT_CONFIG
from tools.experiment05.io import sha256, write_json, write_rows
from tools.experiment05.visualization import (
    VisualizationError,
    VisualizationStore,
    fragments,
    scheduled_vertices,
)
from tools.experiment05.web import create_server


@pytest.fixture
def saved(tmp_path):
    folder = tmp_path / "outputs/experiment_05/runs/run-visual"
    folder.mkdir(parents=True)
    (folder / "config.toml").write_bytes(DEFAULT_CONFIG.read_bytes())
    refs = [
        {
            "reference_id": ref,
            "family_id": ref + "-family",
            "direction_id": direction,
            "direction_label": label,
            "length_m": 1000,
            "points": [
                {"lat": 43, "lon": -79, "s_m": 0},
                {"lat": 43.01, "lon": -79, "s_m": 1000},
            ],
        }
        for ref, direction, label in [("ref-a", "0", "SOUTH"), ("ref-b", "1", "NORTH")]
    ]
    trips = [
        {
            "trip_id": tid,
            "reference_id": "ref-a",
            "scheduled": [
                {
                    "stop_id": "stop-a",
                    "stop_sequence": 1,
                    "s_m": 0,
                    "arrival_t": start,
                    "departure_t": start + 5,
                },
                {
                    "stop_id": "stop-b",
                    "stop_sequence": 2,
                    "s_m": 1000,
                    "arrival_t": start + 100,
                    "departure_t": start + 100,
                },
            ],
        }
        for tid, start in [("schedule-a", 80), ("schedule-b", 100)]
    ]
    write_json(folder / "gtfs/canonical.json", {"references": refs, "trips": trips})
    write_json(
        folder / "gtfs/bundle.json",
        {
            "stops": [
                {"stop_id": "stop-a", "stop_name": "Actual stop", "lat": 43, "lon": -79}
            ]
        },
    )
    seg = [
        {
            "trip_id_performed": tid,
            "vehicle_id": "vehicle-a",
            "direction_id": "0",
            "service_date": "2024-04-01",
            "raw_segment_start": start,
            "raw_segment_end": end,
            "effective_segment_start": start,
            "effective_segment_end": end,
            "platform_states": ["NORMAL"],
            "censor_reason": None,
        }
        for tid, start, end in [("performed:a", 100, 280), ("performed:b", 300, 310)]
    ]
    write_rows(folder / "performed_trip_segments.parquet", seg)

    def point(rid, t, s, **kw):
        return {
            "rid": rid,
            "source": "avl",
            "source_row": t,
            "trip_id_performed": "performed:a",
            "vehicle_id": "vehicle-a",
            "route_eligible": True,
            "t": t,
            "s_m": s,
            "lat": 43,
            "lon": -79,
            "reference_id": "ref-a",
            "fragment_id": "fragment-a",
            "trajectory_included": True,
            "projection_status": "ON_REFERENCE",
            **kw,
        }

    obs = [
        point("p1", 100, 0),
        point("duplicate", 100, 0, duplicate_of="p1"),
        point("p2", 110, 10),
        point(
            "rejected",
            120,
            500,
            trajectory_included=False,
            projection_status="OFF_REFERENCE",
        ),
        point("p3", 130, 20),
        point("p4", 250, 30),
        point("p5", 260, 5, reference_id="ref-b"),
        point("tail", 270, 5, censored_tail=True, exclude_from_matching=True),
        point("b1", 300, 0, trip_id_performed="performed:b"),
        point("b2", 310, 10, trip_id_performed="performed:b"),
        point("out-of-scope", 100, 0, route_eligible=False),
        {
            "rid": "apc-a",
            "source": "apc",
            "trip_id_performed": "performed:a",
            "vehicle_id": "vehicle-a",
            "route_eligible": True,
            "trajectory_included": False,
            "t": 99,
            "arrival": 99,
            "departure": 110,
            "lat": 0,
            "lon": 0,
            "mapped_stop_id": "stop-a",
            "boarding": 3,
            "alighting": 1,
            "apc_evidence_included": True,
            "canonical_stop_intervals": [
                {
                    "reference_id": "ref-a",
                    "s_min_m": 0,
                    "s_max_m": 0,
                    "ambiguous": False,
                }
            ],
        },
    ]
    write_rows(folder / "fused_projected_observations.parquet", obs)
    ranks = [
        {
            "trip_id_performed": "performed:a",
            "match_mode": "global",
            "candidate_id": "candidate-" + str(rank),
            "candidate_rank": rank,
            "trip_id_scheduled": scheduled,
            "reference_id": "ref-a",
            "score": float(rank),
            "hard_constraints_valid": True,
            "quality_eligible": True,
            "score_components": {"absolute_delay_level_cost": rank},
        }
        for rank, scheduled in [
            (1, "schedule-a"),
            (2, "schedule-b"),
            (3, "schedule-a"),
            (4, "schedule-b"),
        ]
    ]
    ranks.append(
        {
            **ranks[0],
            "trip_id_performed": "performed:b",
            "hard_constraints_valid": False,
            "quality_eligible": False,
        }
    )
    write_rows(folder / "candidate_rankings.parquet", ranks)
    assignment = [
        {
            "trip_id_performed": tid,
            "match_mode": "global",
            "trip_id_scheduled": scheduled,
            "assignment_status": "MATCHED" if scheduled else "UNMATCHED",
            "candidate_id": candidate,
            "candidate_rank": 2 if candidate else None,
            "unmatched_reason": None if scheduled else "TOP3_EXHAUSTED",
        }
        for tid, scheduled, candidate in [
            ("performed:a", "schedule-b", "candidate-2"),
            ("performed:b", None, None),
        ]
    ]
    write_rows(folder / "assignments.parquet", assignment)
    write_rows(
        folder / "mode_results.parquet",
        [
            {
                "trip_id_performed": row["trip_id_performed"],
                "match_mode": "global",
                "matcher_status": "PATTERN_ONLY",
                "selection": {
                    "selected_avl_ids": ["avl|p1", "avl|p2"],
                    "time_ranges": [{"start": 100, "end": 110}],
                },
            }
            for row in seg
        ],
    )
    write_rows(
        folder / "stop_visits_evidence.parquet",
        [
            {
                "trip_id_performed": "performed:a",
                "stop_id": "stop-a",
                "arrival_t": 99,
                "departure_t": 110,
                "source_rids": ["apc-a"],
            }
        ],
    )
    write_json(folder / "summary.json", {"validation_ok": True})
    manifest = {
        "schema_version": "experiment05-run-v1",
        "run_id": folder.name,
        "service_date": "2024-04-01",
        "route_id": "29",
        "status": "complete",
        "validation_ok": True,
        "match_modes": ["global"],
        "output_sha256": {
            str(p.relative_to(folder)): sha256(p)
            for p in folder.rglob("*")
            if p.is_file()
        },
    }
    write_json(folder / "run_manifest.json", manifest)
    return folder, manifest


def test_detail_is_source_preserving_and_never_reassigns(saved):
    folder, manifest = saved
    before = {name: sha256(folder / name) for name in manifest["output_sha256"]}
    store = VisualizationStore()
    catalog = store.get(folder, manifest, "catalog", "global")
    assert len(catalog["trips"]) == 2
    detail = store.get(folder, manifest, "trip", "global", "performed:a")
    assert len(detail["avl"]) == 8
    assert {row["rid"] for row in detail["avl"]} >= {"duplicate", "rejected", "tail"}
    assert "out-of-scope" not in {row["rid"] for row in detail["avl"]}
    assert len(detail["candidates"]) == 3
    assert [
        row["candidate_rank"] for row in detail["candidates"] if row["assigned"]
    ] == [2]
    assert detail["candidates"][0]["score_components"] == {
        "absolute_delay_level_cost": 1
    }
    assert detail["apc"][0]["coordinate_role"] == "STOP_LOCATION_NOT_VEHICLE_PING"
    assert (detail["apc"][0]["lat"], detail["apc"][0]["lon"]) == (43, -79)
    assert (detail["apc"][0]["arrival"], detail["apc"][0]["departure"]) == (99, 110)
    assert {p["rid"] for p in detail["avl"] if p["selected_for_mode"]} == {"p1", "p2"}
    assert detail["display_rules"]["detail_downsampled"] is False
    assert detail["scoring_recomputed"] is False
    assert before == {name: sha256(folder / name) for name in before}


def test_fragments_do_not_bridge_reject_gap_reference_or_censor(saved):
    folder, manifest = saved
    detail = VisualizationStore().get(folder, manifest, "trip", "global", "performed:a")
    assert [[p["rid"] for p in f["points"]] for f in detail["fragments"]] == [
        ["p1", "p2"],
        ["p3"],
        ["p4"],
        ["p5"],
    ]
    assert [f["reference_id"] for f in detail["fragments"]] == ["ref-a"] * 3 + ["ref-b"]


def test_overview_facets_and_diagnostic_fallback_are_not_assignments(saved):
    folder, manifest = saved
    overview = VisualizationStore().get(folder, manifest, "overview", "global")
    assert len(overview["facets"]) == 2
    facet = next(f for f in overview["facets"] if f["reference_id"] == "ref-a")
    a, b = facet["segments"]
    assert a["display_role"] == "ASSIGNED" and a["candidate"]["candidate_rank"] == 2
    assert b["display_role"] == "DIAGNOSTIC_FALLBACK"
    assert (
        not b["candidate"]["assigned"] and not b["candidate"]["hard_constraints_valid"]
    )
    assert overview["display_rules"]["reference_axes_shared"] is False
    assert overview["display_rules"]["fallback_is_assignment"] is False
    assert a["candidate"]["trajectory"][0]["t"] == 100  # No fitted delay applied.


def test_schedule_vertices_retain_dwell_without_fixed_grid():
    schedule = [
        {"s_m": 0, "arrival_t": 86395, "departure_t": 86410},
        {"s_m": 1000, "arrival_t": 87000, "departure_t": 87000},
    ]
    assert scheduled_vertices(schedule) == [
        {"s_m": 0, "t": 86395},
        {"s_m": 0, "t": 86410},
        {"s_m": 1000, "t": 87000},
    ]


def test_display_thinning_does_not_modify_original_or_drop_endpoints():
    rows = [
        {
            "rid": str(i),
            "t": i,
            "s_m": i,
            "trajectory_included": True,
            "reference_id": "r",
            "fragment_id": "f",
        }
        for i in range(500)
    ]
    before = copy.deepcopy(rows)
    shown = fragments(rows, 90, 160)
    assert len(shown[0]["points"]) == 160
    assert shown[0]["source_point_count"] == 500
    assert [shown[0]["points"][i]["rid"] for i in (0, -1)] == ["0", "499"]
    assert len(fragments(rows, 90)[0]["points"]) == 500 and rows == before


@pytest.mark.parametrize("kind", ["catalog", "trip", "overview"])
def test_cached_view_still_rejects_tampered_published_file(saved, kind):
    folder, manifest = saved
    store = VisualizationStore()
    store.get(folder, manifest, kind, "global", "performed:a")
    with (folder / "fused_projected_observations.parquet").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(VisualizationError, match="changed"):
        store.get(folder, manifest, kind, "global", "performed:a")


def test_symlink_and_unpublished_artifacts_rejected(saved, tmp_path):
    folder, manifest = saved
    file = folder / "gtfs/canonical.json"
    target = tmp_path / "outside.json"
    file.rename(target)
    file.symlink_to(target)
    with pytest.raises(VisualizationError, match="Unreadable"):
        VisualizationStore().get(folder, manifest, "catalog", "global")


@pytest.mark.parametrize(
    "mutation,mode,code",
    [
        ({"status": "running"}, "global", "RUN_NOT_PUBLISHED"),
        ({"validation_ok": False}, "global", "RUN_NOT_PUBLISHED"),
        ({}, "all", "INVALID_MODE"),
        ({}, "early", "MODE_NOT_PUBLISHED"),
    ],
)
def test_invalid_or_unpublished_requests_fail_closed(saved, mutation, mode, code):
    folder, manifest = saved
    with pytest.raises(VisualizationError) as error:
        VisualizationStore().get(folder, manifest | mutation, "catalog", mode)
    assert error.value.code == code


def test_unknown_trip_is_not_a_path(saved):
    folder, manifest = saved
    with pytest.raises(VisualizationError) as error:
        VisualizationStore().get(
            folder, manifest, "trip", "global", "../../config.toml"
        )
    assert error.value.code == "TRIP_NOT_FOUND"


@pytest.mark.parametrize("kind", ["trip", "overview"])
def test_saved_assignment_cannot_be_silently_replaced_by_fallback(saved, kind):
    folder, manifest = saved
    from tools.experiment05.io import read_rows

    path = folder / "assignments.parquet"
    assignments = read_rows(path)
    assignments[0]["candidate_id"] = "missing-candidate"
    write_rows(path, assignments)
    for changed in (path, path.with_suffix(".schema.json")):
        manifest["output_sha256"][str(changed.relative_to(folder))] = sha256(changed)
    with pytest.raises(VisualizationError) as error:
        VisualizationStore().get(folder, manifest, kind, "global", "performed:a")
    assert error.value.code == "ASSIGNMENT_CANDIDATE_MISMATCH"


def test_real_http_visualization_query_and_assets(saved):
    folder, _ = saved
    root = folder.parents[3]
    server = create_server(root=root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    web = server, None, root
    try:
        url = "/api/runs/run-visual/visualization/"
        status, body, _ = request(
            web,
            path=url
            + "trip?"
            + urlencode({"mode": "global", "trip_id": "performed:a"}),
        )
        assert (
            status == 200
            and json.loads(body)["trip"]["trip_id_performed"] == "performed:a"
        )
        for query in (
            "",
            "?mode=all",
            "?mode=global&mode=early",
            "?mode=global&extra=x",
        ):
            assert request(web, path=url + "catalog" + query)[0] == 400
        assert request(web, path=url + "catalog?mode=early")[0] == 404
        for asset in ("/viz.js", "/viz.css"):
            assert request(web, path=asset)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
