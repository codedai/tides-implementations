"""Tests for the experimental metre-space geometry module.

All hand-computed fixtures live in a local metre frame anchored at
Toronto-ish coordinates (lat 43.7, lon -79.4) and are converted to
degrees with the same equirectangular scale the module uses, so the
expected residual/along values are exact to well under the stated
tolerances.
"""

from __future__ import annotations

import math

import polars as pl
import pytest
from r1spike import geometry
from r1spike.contracts import Reason, SpatialEvidence
from r1spike.geometry import (
    GeometryScope,
    ShapeProjection,
    assess_point,
    build_candidate_geometries,
    build_shape_geometry,
    coords_structurally_valid,
    project_point,
)

BASE_LAT = 43.7
BASE_LON = -79.4
M_PER_DEG_LAT = geometry.EARTH_RADIUS_M * math.pi / 180.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(BASE_LAT))

SHAPES_SCHEMA = {
    "shape_id": pl.String,
    "shape_pt_lat": pl.Float64,
    "shape_pt_lon": pl.Float64,
    "shape_pt_sequence": pl.Int64,
}


def _ll(east_m: float, north_m: float) -> tuple[float, float]:
    """Local metre offsets -> (lat, lon) around the Toronto base point."""
    return (BASE_LAT + north_m / M_PER_DEG_LAT, BASE_LON + east_m / M_PER_DEG_LON)


def _shapes_df(
    rows: list[tuple[str, float | None, float | None, int]],
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "shape_id": [r[0] for r in rows],
            "shape_pt_lat": [r[1] for r in rows],
            "shape_pt_lon": [r[2] for r in rows],
            "shape_pt_sequence": [r[3] for r in rows],
        },
        schema=SHAPES_SCHEMA,
    )


def _shape_rows(
    shape_id: str, points_m: list[tuple[float, float]]
) -> list[tuple[str, float, float, int]]:
    rows = []
    for i, (east_m, north_m) in enumerate(points_m):
        lat, lon = _ll(east_m, north_m)
        rows.append((shape_id, lat, lon, i + 1))
    return rows


def _scope(
    shape_id: str = "shape-L",
    route_id: str = "29",
    direction_id: int | None = 0,
    feed_id: str = "feed-2026-01",
) -> GeometryScope:
    return GeometryScope(
        feed_id=feed_id,
        route_id=route_id,
        direction_id=direction_id,
        shape_id=shape_id,
    )


# Two-segment L polyline: east 100 m along y=0, then north 100 m along
# x=100. Total length 200 m.
L_POINTS_M = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]


def _l_shape_geometry() -> geometry.ShapeGeometry:
    return build_shape_geometry(
        _shapes_df(_shape_rows("shape-L", L_POINTS_M)), _scope()
    )


class TestBuildShapeGeometry:
    def test_shuffled_input_row_order_produces_identical_geometry(self) -> None:
        rows = _shape_rows("shape-L", L_POINTS_M)
        shuffled = [rows[2], rows[0], rows[1]]
        geom_sorted = build_shape_geometry(_shapes_df(rows), _scope())
        geom_shuffled = build_shape_geometry(_shapes_df(shuffled), _scope())
        assert geom_shuffled == geom_sorted
        assert geom_shuffled.quality_status == "ok"

    def test_length_and_cumulative_bookkeeping_for_l_shape(self) -> None:
        geom = _l_shape_geometry()
        assert geom.n_points == 3
        assert geom.dropped_points == 0
        assert geom.length_m == pytest.approx(200.0, abs=0.01)
        assert geom.cum_lengths_m[0] == 0.0
        assert geom.cum_lengths_m[-1] == geom.length_m

    def test_duplicate_shape_pt_sequence_flags_status_and_keeps_first(self) -> None:
        rows = _shape_rows("shape-L", L_POINTS_M)
        duplicate_lat, duplicate_lon = _ll(500.0, 500.0)
        rows.append(("shape-L", duplicate_lat, duplicate_lon, 2))  # dup of seq 2
        geom = build_shape_geometry(_shapes_df(rows), _scope())
        assert geom.quality_status == "duplicate_sequence"
        assert geom.n_points == 3
        # The first occurrence of sequence 2 (the original corner-adjacent
        # vertex) must have been kept, so the length is unchanged.
        assert geom.length_m == pytest.approx(200.0, abs=0.01)

    def test_null_coordinate_rows_are_dropped_and_counted(self) -> None:
        rows: list[tuple[str, float | None, float | None, int]] = list(
            _shape_rows("shape-L", L_POINTS_M)
        )
        rows.insert(1, ("shape-L", None, BASE_LON, 99))
        geom = build_shape_geometry(_shapes_df(rows), _scope())
        assert geom.dropped_points == 1
        assert geom.n_points == 3
        assert geom.quality_status == "ok"

    def test_one_point_shape_is_degenerate(self) -> None:
        lat, lon = _ll(0.0, 0.0)
        geom = build_shape_geometry(
            _shapes_df([("shape-1pt", lat, lon, 1)]), _scope(shape_id="shape-1pt")
        )
        assert geom.quality_status == "degenerate"
        assert not geom.is_projectable()

    def test_all_identical_points_shape_is_degenerate(self) -> None:
        lat, lon = _ll(0.0, 0.0)
        rows = [("shape-same", lat, lon, i) for i in range(1, 4)]
        geom = build_shape_geometry(_shapes_df(rows), _scope(shape_id="shape-same"))
        assert geom.quality_status == "degenerate"

    def test_unknown_shape_id_yields_degenerate_empty_geometry(self) -> None:
        geom = build_shape_geometry(
            _shapes_df(_shape_rows("shape-L", L_POINTS_M)),
            _scope(shape_id="shape-absent"),
        )
        assert geom.quality_status == "degenerate"
        assert geom.n_points == 0


class TestProjectPoint:
    def test_point_30m_perpendicular_off_middle_of_segment_zero(self) -> None:
        geom = _l_shape_geometry()
        lat, lon = _ll(50.0, 30.0)
        proj = project_point(geom, lat, lon)
        assert proj.segment_index == 0
        assert proj.segment_fraction == pytest.approx(0.5, abs=0.01)
        assert proj.residual_m == pytest.approx(30.0, abs=0.5)
        assert proj.along_m == pytest.approx(50.0, abs=1.0)
        expected_lat, expected_lon = _ll(50.0, 0.0)
        assert proj.aligned_lat == pytest.approx(expected_lat, abs=2e-5)
        assert proj.aligned_lon == pytest.approx(expected_lon, abs=2e-5)
        assert proj.method == geometry.PROJECTION_METHOD

    def test_point_past_the_corner_projects_onto_segment_one(self) -> None:
        geom = _l_shape_geometry()
        lat, lon = _ll(130.0, 50.0)
        proj = project_point(geom, lat, lon)
        assert proj.segment_index == 1
        assert proj.segment_fraction == pytest.approx(0.5, abs=0.01)
        assert proj.residual_m == pytest.approx(30.0, abs=0.5)
        assert proj.along_m == pytest.approx(150.0, abs=1.0)

    def test_point_beyond_last_vertex_clamps_to_endpoint(self) -> None:
        geom = _l_shape_geometry()
        lat, lon = _ll(100.0, 130.0)
        proj = project_point(geom, lat, lon)
        assert proj.segment_index == 1
        assert proj.segment_fraction == 1.0
        assert proj.residual_m == pytest.approx(30.0, abs=0.5)
        assert proj.along_m == pytest.approx(200.0, abs=1.0)
        endpoint_lat, endpoint_lon = _ll(100.0, 100.0)
        assert proj.aligned_lat == pytest.approx(endpoint_lat, abs=2e-5)
        assert proj.aligned_lon == pytest.approx(endpoint_lon, abs=2e-5)

    def test_exact_tie_at_shared_corner_keeps_lowest_segment_index(self) -> None:
        # A point on the outside bisector of the corner clamps to the
        # shared corner vertex from both segments: identical distances.
        # The documented deterministic tie-break keeps segment 0.
        geom = _l_shape_geometry()
        lat, lon = _ll(130.0, -30.0)
        proj = project_point(geom, lat, lon)
        assert proj.segment_index == 0
        assert proj.segment_fraction == 1.0

    def test_zero_length_segment_is_skipped_but_bookkeeping_is_correct(
        self,
    ) -> None:
        points = [(0.0, 0.0), (100.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
        geom = build_shape_geometry(
            _shapes_df(_shape_rows("shape-zl", points)), _scope(shape_id="shape-zl")
        )
        assert geom.quality_status == "ok"
        assert geom.length_m == pytest.approx(200.0, abs=0.01)
        lat, lon = _ll(100.0, 130.0)
        proj = project_point(geom, lat, lon)
        assert proj.segment_index == 2  # zero-length segment 1 skipped
        assert proj.segment_fraction == 1.0
        assert proj.along_m == pytest.approx(200.0, abs=1.0)

    def test_projection_onto_degenerate_geometry_raises_value_error(self) -> None:
        lat, lon = _ll(0.0, 0.0)
        geom = build_shape_geometry(
            _shapes_df([("shape-1pt", lat, lon, 1)]), _scope(shape_id="shape-1pt")
        )
        with pytest.raises(ValueError, match="degenerate"):
            project_point(geom, *_ll(10.0, 10.0))

    def test_along_m_increases_monotonically_along_the_shape(self) -> None:
        geom = _l_shape_geometry()
        offsets_m = [
            (10.0, 5.0),
            (40.0, -5.0),
            (80.0, 8.0),
            (95.0, 20.0),
            (100.0, 150.0),
        ]
        alongs = [project_point(geom, *_ll(e, n)).along_m for e, n in offsets_m]
        assert alongs == sorted(alongs)
        assert all(b > a for a, b in zip(alongs, alongs[1:], strict=False))

    def test_residual_accuracy_for_known_111m_latitude_offset(self) -> None:
        # 0.001 degrees of latitude is 111.19 m on the spherical model.
        rows = [
            ("shape-ew", 43.7, -79.4, 1),
            ("shape-ew", 43.7, -79.398, 2),
        ]
        geom = build_shape_geometry(_shapes_df(rows), _scope(shape_id="shape-ew"))
        proj = project_point(geom, 43.701, -79.399)
        assert proj.residual_m == pytest.approx(111.19, rel=0.01)


class TestCoordsStructurallyValid:
    def test_valid_toronto_coordinates_are_accepted(self) -> None:
        assert coords_structurally_valid(43.7, -79.4) is True

    def test_none_coordinates_are_rejected(self) -> None:
        assert coords_structurally_valid(None, -79.4) is False
        assert coords_structurally_valid(43.7, None) is False
        assert coords_structurally_valid(None, None) is False

    def test_nan_coordinates_are_rejected(self) -> None:
        assert coords_structurally_valid(math.nan, -79.4) is False
        assert coords_structurally_valid(43.7, math.nan) is False

    def test_out_of_range_coordinates_are_rejected(self) -> None:
        assert coords_structurally_valid(90.001, -79.4) is False
        assert coords_structurally_valid(-90.001, -79.4) is False
        assert coords_structurally_valid(43.7, 180.001) is False
        assert coords_structurally_valid(43.7, -180.001) is False

    def test_null_island_export_artifact_is_rejected(self) -> None:
        assert coords_structurally_valid(0.0, 0.0) is False
        assert coords_structurally_valid(1e-9, -1e-9) is False

    def test_single_zero_coordinate_with_real_partner_is_accepted(self) -> None:
        # Only the *simultaneous* (0, 0) artifact is invalid.
        assert coords_structurally_valid(0.0, -79.4) is True
        assert coords_structurally_valid(43.7, 0.0) is True


class TestAssessPoint:
    BOUNDS = {"soft_bound_m": 50.0, "hard_bound_m": 250.0}

    def test_residual_30m_is_present_with_no_reason_codes(self) -> None:
        geom = _l_shape_geometry()
        assessment = assess_point(geom, *_ll(50.0, 30.0), **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.PRESENT
        assert assessment.reason_codes == ()
        assert isinstance(assessment.projection, ShapeProjection)

    def test_residual_100m_is_weak_with_projection_retained(self) -> None:
        geom = _l_shape_geometry()
        # South of segment 0, so segment 1 (the vertical leg) is farther.
        assessment = assess_point(geom, *_ll(50.0, -100.0), **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.WEAK
        assert assessment.reason_codes == (Reason.GEOM_WEAK,)
        assert assessment.projection is not None
        assert assessment.projection.residual_m == pytest.approx(100.0, abs=0.5)

    def test_residual_300m_is_hard_contradiction_with_projection_retained(
        self,
    ) -> None:
        geom = _l_shape_geometry()
        # South of segment 0, so segment 1 (the vertical leg) is farther.
        assessment = assess_point(geom, *_ll(50.0, -300.0), **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.HARD_CONTRADICTION
        assert assessment.reason_codes == (Reason.GEOM_HARD_CONTRADICTION,)
        # Contradiction is an evidence state, not data destruction: the
        # projection (and its residual) must survive for diagnostics.
        assert assessment.projection is not None
        assert assessment.projection.residual_m == pytest.approx(300.0, abs=0.5)

    def test_null_island_coordinates_yield_missing_invalid_coords(self) -> None:
        geom = _l_shape_geometry()
        assessment = assess_point(geom, 0.0, 0.0, **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.MISSING
        assert assessment.reason_codes == (Reason.GEOM_INVALID_COORDS,)
        assert assessment.projection is None

    def test_none_coordinates_yield_missing_invalid_coords(self) -> None:
        geom = _l_shape_geometry()
        assessment = assess_point(geom, None, None, **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.MISSING
        assert assessment.reason_codes == (Reason.GEOM_INVALID_COORDS,)

    def test_nan_coordinates_yield_missing_invalid_coords(self) -> None:
        geom = _l_shape_geometry()
        assessment = assess_point(geom, math.nan, -79.4, **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.MISSING
        assert assessment.reason_codes == (Reason.GEOM_INVALID_COORDS,)

    def test_missing_geometry_yields_missing_geom_reason(self) -> None:
        assessment = assess_point(None, *_ll(50.0, 30.0), **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.MISSING
        assert assessment.reason_codes == (Reason.GEOM_MISSING,)
        assert assessment.projection is None

    def test_degenerate_geometry_yields_missing_geom_reason(self) -> None:
        lat, lon = _ll(0.0, 0.0)
        geom = build_shape_geometry(
            _shapes_df([("shape-1pt", lat, lon, 1)]), _scope(shape_id="shape-1pt")
        )
        assessment = assess_point(geom, *_ll(10.0, 10.0), **self.BOUNDS)
        assert assessment.evidence is SpatialEvidence.MISSING
        assert assessment.reason_codes == (Reason.GEOM_MISSING,)


class TestBuildCandidateGeometries:
    def test_parallel_shapes_stay_separate_scoped_candidates(self) -> None:
        # Route 29 and 929 running 200 m apart on the same street: both
        # must be built, both must be assessable, and nothing may pick
        # one for the caller.
        street_29 = [(0.0, 0.0), (300.0, 0.0)]
        street_929 = [(0.0, 200.0), (300.0, 200.0)]
        shapes = _shapes_df(
            _shape_rows("shape-29", street_29) + _shape_rows("shape-929", street_929)
        )
        scope_29 = _scope(shape_id="shape-29", route_id="29")
        scope_929 = _scope(shape_id="shape-929", route_id="929")
        geoms = build_candidate_geometries(shapes, [scope_29, scope_929])

        key_29 = ("feed-2026-01", "29", 0, "shape-29")
        key_929 = ("feed-2026-01", "929", 0, "shape-929")
        assert set(geoms) == {key_29, key_929}

        lat, lon = _ll(150.0, 30.0)
        bounds = {"soft_bound_m": 50.0, "hard_bound_m": 250.0}
        assessment_29 = assess_point(geoms[key_29], lat, lon, **bounds)
        assessment_929 = assess_point(geoms[key_929], lat, lon, **bounds)

        assert assessment_29.projection is not None
        assert assessment_929.projection is not None
        assert assessment_29.projection != assessment_929.projection
        assert assessment_29.projection.residual_m == pytest.approx(30.0, abs=0.5)
        assert assessment_929.projection.residual_m == pytest.approx(170.0, abs=0.5)
        assert assessment_29.evidence is SpatialEvidence.PRESENT
        assert assessment_929.evidence is SpatialEvidence.WEAK

    def test_no_nearest_shape_convenience_api_exists(self) -> None:
        # The legacy anonymous nearest-shape fallback must not creep
        # back in: no public name in the module offers it.
        nearest_like = [
            name
            for name in dir(geometry)
            if "nearest" in name.lower() or "best_shape" in name.lower()
        ]
        assert nearest_like == []
