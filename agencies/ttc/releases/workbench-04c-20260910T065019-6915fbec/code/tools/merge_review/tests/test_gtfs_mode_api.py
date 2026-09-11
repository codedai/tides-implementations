"""Matching-mode requests are explicit, read-only and scoped to one saved run."""

import pytest

from tools.merge_review.tests.test_debug_server import (
    RunData,
    debug_site,  # noqa: F401
)
from tools.merge_review.tests.test_server import request


class ModeData(RunData):
    available_matching_modes = ["global", "early", "middle", "late"]

    def _check(self, mode):
        if mode not in self.available_matching_modes:
            raise ValueError("此历史版本没有计算指定模式")

    def metadata(self):
        return {
            **super().metadata(),
            "available_matching_modes": self.available_matching_modes,
        }

    def catalog(self, method, vehicle, matching_mode="global"):
        self._check(matching_mode)
        return [
            {
                **row,
                "matching_mode": matching_mode,
                "gtfs_status": "MATCHED"
                if matching_mode == "global"
                else "PATTERN_ONLY",
            }
            for row in super().catalog(method, vehicle)
        ]

    def case(self, method, vehicle, case_id, context_s=180, matching_mode="global"):
        self._check(matching_mode)
        return {
            "case": {"id": case_id},
            "observations": [{"rid": "source", "t": 100}],
            "avl": [{"rid": "source", "t": 100}],
            "apc": [],
            "provenance": {"run_path": str(self.run)},
            "gtfs": {
                "matching_mode": matching_mode,
                "available_matching_modes": self.available_matching_modes,
                "decision_selection": {"mode": matching_mode},
                "result": {
                    "status": "MATCHED"
                    if matching_mode == "global"
                    else "PATTERN_ONLY",
                    "candidates": [],
                    "all_candidates": [],
                },
                "candidates": [],
                "windows": {"windows": [{"id": matching_mode}]},
                "projection": {
                    "points": [{"rid": "source", "t": 100, "s": 200, "status": "VALID"}]
                },
            },
        }

    def day_overview(self, matching_mode="global"):
        self._check(matching_mode)
        return {
            "run_id": self.run.name,
            "matching_mode": matching_mode,
            "cases": [{"id": "case", "candidate": None}],
            "matching_recomputed": False,
        }


@pytest.fixture
def mode_site(debug_site):  # noqa: F811
    server, store = debug_site
    for run_id in ("run-a", "run-b"):
        _, run_store = server.resolve_run(run_id)
        server.run_cache[run_id] = (
            ModeData(server.workspace.get_run(run_id)),
            run_store,
        )
    return server, store


def test_four_modes_switch_saved_result_not_raw_rows_or_notes(mode_site):
    server, _ = mode_site

    def saved_notes():
        return {
            run: {
                key: value
                for key, value in store.export().items()
                if key != "exported_at"
            }
            for run, (_, store) in server.run_cache.items()
        }

    snapshot = saved_notes()
    raw = []
    for mode in ModeData.available_matching_modes:
        query = f"run=run-a&matching_mode={mode}"
        catalog = request(
            mode_site, f"/api/catalog?method=avl_context&vehicle=vehicle&{query}"
        )
        assert catalog[0] == 200
        assert catalog[1]["matching_mode"] == mode
        assert catalog[1]["cases"][0]["matching_mode"] == mode
        detail = request(
            mode_site,
            f"/api/case?method=avl_context&vehicle=vehicle&case_id=case&{query}",
        )
        assert detail[0] == 200
        gtfs = detail[1]["gtfs"]
        assert gtfs["matching_mode"] == mode
        assert gtfs["windows"]["windows"][0]["id"] == mode
        raw.append(detail[1]["observations"])
        overview = request(mode_site, f"/api/day-overview?{query}")
        assert overview[0] == 200
        assert overview[1]["matching_mode"] == mode
        assert overview[1]["run_id"] == "run-a"
        assert overview[1]["matching_recomputed"] is False
    assert all(value == raw[0] for value in raw)
    assert saved_notes() == snapshot


def test_mode_less_detail_and_overview_remain_global(mode_site):
    detail = request(
        mode_site, "/api/case?run=run-a&method=avl_context&vehicle=vehicle&case_id=case"
    )
    assert detail[0] == 200
    assert detail[1]["gtfs"]["matching_mode"] == "global"
    assert (
        request(mode_site, "/api/day-overview?run=run-a")[1]["matching_mode"]
        == "global"
    )


def test_run_alias_is_explicit_and_does_not_change_other_tab(mode_site):
    for run_id in ("run-a", "run-b", "run-a"):
        response = request(
            mode_site, f"/api/day-overview?run_id={run_id}&matching_mode=late"
        )
        assert response[0] == 200
        assert response[1]["run_id"] == run_id
    assert request(mode_site, "/api/day-overview?run=run-a&run_id=run-a")[0] == 200
    assert (
        request(mode_site, "/api/meta?run=run-a")[1]["available_matching_modes"]
        == ModeData.available_matching_modes
    )


@pytest.mark.parametrize(
    "query",
    [
        "matching_mode=bad",
        "matching_mode=",
        "matching_mode=early&matching_mode=late",
        "run=run-a&run=run-b",
        "run_id=run-a&run_id=run-b",
        "run=run-a&run_id=run-b",
        "run=../../elsewhere",
    ],
)
def test_invalid_or_ambiguous_overview_request_fails(mode_site, query):
    assert request(mode_site, "/api/day-overview?" + query)[0] == 400


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/case?method=avl_context&vehicle=vehicle&case_id=case&",
        "/api/catalog?method=avl_context&vehicle=vehicle&",
    ],
)
def test_detail_and_catalog_validate_mode_before_using_it(mode_site, endpoint):
    assert request(mode_site, endpoint + "run=run-a&matching_mode=bad")[0] == 400
    assert (
        request(
            mode_site, endpoint + "run=run-a&matching_mode=early&matching_mode=late"
        )[0]
        == 400
    )


def test_historical_run_rejects_uncomputed_partial_and_legacy_overview(mode_site):
    server, _ = mode_site
    server.run_cache["run-a"][0].available_matching_modes = ["global"]
    assert (
        request(mode_site, "/api/day-overview?run=run-a&matching_mode=early")[0] == 400
    )
    assert (
        request(mode_site, "/api/day-overview?run=run-a&matching_mode=global")[0] == 200
    )
    _, store = server.run_cache["run-a"]
    server.run_cache["run-a"] = (RunData(server.workspace.get_run("run-a")), store)
    assert request(mode_site, "/api/day-overview?run=run-a")[0] == 400
    assert (
        request(
            mode_site,
            "/api/case?run=run-a&method=avl_context&vehicle=vehicle&case_id=case&matching_mode=early",
        )[0]
        == 400
    )
    assert (
        request(
            mode_site,
            "/api/case?run=run-a&method=avl_context&vehicle=vehicle&case_id=case",
        )[0]
        == 200
    )
