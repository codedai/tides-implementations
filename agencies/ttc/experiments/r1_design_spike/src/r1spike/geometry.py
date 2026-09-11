"""Experimental metre-space shape geometry and spatial-evidence assessment.

STATUS: experimental design-validation spike code (see package docstring
and ``ALGORITHM_VERSION``). Not production, not TIDES-conformant, not an
approved S3+ implementation.

Legacy failure modes this module corrects:

* The legacy project snapped every AVL ping to the nearest *densified
  vertex* of ONE hard-coded shape. Here, points are projected onto
  polyline *segments* (parametric projection clamped to the segment),
  so residuals are true point-to-polyline distances, not vertex-spacing
  artifacts.
* Distances were computed in raw degree space, where one degree of
  longitude at Toronto latitudes is ~72 km versus ~111 km per degree of
  latitude. Here, all distances are computed in metres via an
  equirectangular projection about each shape's mean latitude.
* Residuals were discarded and off-route points were forced onto the
  route. Here, every projection retains ``residual_m``, and off-route
  points become explicit evidence states (``WEAK`` /
  ``HARD_CONTRADICTION``) — contradiction is recorded, never erased.
* An anonymous nearest-shape fallback silently picked a geometry.
  Here, there is deliberately NO nearest-shape convenience API: callers
  must evaluate every scoped candidate geometry separately
  (``build_candidate_geometries``) and compare the assessments.

The ``soft_bound_m`` / ``hard_bound_m`` residual thresholds are
caller-supplied EXPERIMENTAL configuration. They are unapproved: no
threshold value in this module has been ratified for production use.

Equirectangular accuracy note: for shapes spanning tens of kilometres at
Toronto latitudes (~43.6-43.8 N), the small-angle/local-plane error of
this projection is far below the metre tolerances used for evidence
classification; see the accompanying tests for a latitude-offset sanity
check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import polars as pl

from r1spike.contracts import Reason, SpatialEvidence

GEOMETRY_VERSION = "xg1"
PROJECTION_METHOD = "equirect_segment_projection_v1"
EARTH_RADIUS_M = 6_371_008.8

QUALITY_OK = "ok"
QUALITY_DEGENERATE = "degenerate"
QUALITY_DUPLICATE_SEQUENCE = "duplicate_sequence"

# Coordinates within this many degrees of (0, 0) simultaneously are
# treated as the null-island export artifact, not a real position.
_NULL_ISLAND_EPS_DEG = 1e-6


@dataclass(frozen=True)
class GeometryScope:
    """Fully-scoped identity of one candidate shape geometry.

    A geometry is never anonymous: it belongs to exactly one
    (feed, route, direction, shape) tuple, so downstream evidence can
    always be traced to the hypothesis it was computed against.
    """

    feed_id: str
    route_id: str
    direction_id: int | None
    shape_id: str
    geometry_version: str = GEOMETRY_VERSION


@dataclass(frozen=True)
class ShapeGeometry:
    """Precomputed projection arrays for one scoped shape polyline.

    Internal arrays (all tuples of floats, index-aligned per vertex
    unless noted):

    * ``lats`` / ``lons`` — usable vertex coordinates in degrees, in
      ascending ``shape_pt_sequence`` order after cleaning.
    * ``xs`` / ``ys`` — equirectangular metre coordinates of each
      vertex: ``x = R * cos(lat0) * radians(lon)``,
      ``y = R * radians(lat)``, with ``lat0`` the mean latitude of the
      usable vertices (``lat0_rad`` / ``cos_lat0`` are stored for the
      inverse transform).
    * ``seg_lengths_m`` — per-segment euclidean metre length
      (``len(seg_lengths_m) == n_points - 1``). Zero-length segments
      are kept here so cumulative bookkeeping stays correct, but they
      are skipped as projection targets.
    * ``cum_lengths_m`` — cumulative metres from the shape start to
      each vertex (``cum_lengths_m[0] == 0.0``,
      ``cum_lengths_m[-1] == length_m``).

    ``quality_status`` is one of ``"ok"``, ``"degenerate"`` (fewer than
    two distinct usable vertices; projection is refused), or
    ``"duplicate_sequence"`` (duplicate ``shape_pt_sequence`` values
    were collapsed to their first stable-sorted occurrence — projectable
    but suspect). ``dropped_points`` counts rows discarded for null
    coordinates; any nonzero value is a data-quality concern.
    """

    scope: GeometryScope
    quality_status: str
    n_points: int
    length_m: float
    dropped_points: int
    lat0_rad: float
    cos_lat0: float
    lats: tuple[float, ...]
    lons: tuple[float, ...]
    xs: tuple[float, ...]
    ys: tuple[float, ...]
    seg_lengths_m: tuple[float, ...]
    cum_lengths_m: tuple[float, ...]

    def is_projectable(self) -> bool:
        """True when ``project_point`` will accept this geometry."""
        return self.quality_status != QUALITY_DEGENERATE


@dataclass(frozen=True, slots=True)
class ShapeProjection:
    """Result of projecting one point onto one scoped shape polyline."""

    scope: GeometryScope
    aligned_lat: float
    aligned_lon: float
    residual_m: float
    along_m: float
    segment_index: int
    segment_fraction: float
    method: str


@dataclass(frozen=True)
class SpatialAssessment:
    """Spatial evidence for one point against one scoped geometry.

    ``projection`` is retained for WEAK and HARD_CONTRADICTION so
    diagnostics keep the residual — contradiction is an evidence state,
    not data destruction.
    """

    evidence: SpatialEvidence
    projection: ShapeProjection | None
    reason_codes: tuple[str, ...]


def _to_xy(lat: float, lon: float, cos_lat0: float) -> tuple[float, float]:
    """Forward equirectangular transform (degrees -> metres)."""
    x = EARTH_RADIUS_M * cos_lat0 * math.radians(lon)
    y = EARTH_RADIUS_M * math.radians(lat)
    return x, y


def _to_lat_lon(x: float, y: float, cos_lat0: float) -> tuple[float, float]:
    """Inverse equirectangular transform (metres -> degrees)."""
    lat = math.degrees(y / EARTH_RADIUS_M)
    lon = math.degrees(x / (EARTH_RADIUS_M * cos_lat0))
    return lat, lon


def build_shape_geometry(
    shapes: pl.DataFrame,
    scope: GeometryScope,
) -> ShapeGeometry:
    """Build precomputed metre-space geometry for one scoped shape.

    Input handling (polars) is deliberately defensive:

    * rows are filtered to ``scope.shape_id`` and EXPLICITLY stable-
      sorted by ``shape_pt_sequence`` — row order is never trusted;
    * duplicate ``shape_pt_sequence`` values keep their first
      stable-sorted occurrence and mark the geometry
      ``"duplicate_sequence"``;
    * rows with null latitude or longitude are dropped and counted in
      ``dropped_points``;
    * fewer than two *distinct* usable vertices marks the geometry
      ``"degenerate"`` (``project_point`` refuses it).

    ``shape_dist_traveled`` is intentionally ignored; distances are
    recomputed from coordinates.
    """
    subset = (
        shapes.filter(pl.col("shape_id") == scope.shape_id)
        .sort("shape_pt_sequence", maintain_order=True)
        .select("shape_pt_sequence", "shape_pt_lat", "shape_pt_lon")
    )

    n_raw = subset.height
    deduped = subset.unique(
        subset=["shape_pt_sequence"], keep="first", maintain_order=True
    )
    had_duplicate_sequence = deduped.height < n_raw

    usable = deduped.drop_nulls(subset=["shape_pt_lat", "shape_pt_lon"])
    dropped_points = deduped.height - usable.height

    lats = tuple(usable.get_column("shape_pt_lat").to_list())
    lons = tuple(usable.get_column("shape_pt_lon").to_list())
    n_points = len(lats)

    distinct_coords = len(set(zip(lats, lons, strict=True)))
    if distinct_coords < 2:
        quality_status = QUALITY_DEGENERATE
    elif had_duplicate_sequence:
        quality_status = QUALITY_DUPLICATE_SEQUENCE
    else:
        quality_status = QUALITY_OK

    lat0_rad = math.radians(sum(lats) / n_points) if n_points else 0.0
    cos_lat0 = math.cos(lat0_rad)

    xy = [_to_xy(lat, lon, cos_lat0) for lat, lon in zip(lats, lons, strict=True)]
    xs = tuple(p[0] for p in xy)
    ys = tuple(p[1] for p in xy)

    seg_lengths: list[float] = []
    cum_lengths: list[float] = [0.0] if n_points else []
    for i in range(n_points - 1):
        seg_len = math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i])
        seg_lengths.append(seg_len)
        cum_lengths.append(cum_lengths[-1] + seg_len)

    return ShapeGeometry(
        scope=scope,
        quality_status=quality_status,
        n_points=n_points,
        length_m=cum_lengths[-1] if cum_lengths else 0.0,
        dropped_points=dropped_points,
        lat0_rad=lat0_rad,
        cos_lat0=cos_lat0,
        lats=lats,
        lons=lons,
        xs=xs,
        ys=ys,
        seg_lengths_m=tuple(seg_lengths),
        cum_lengths_m=tuple(cum_lengths),
    )


def project_point(
    geom: ShapeGeometry,
    lat: float,
    lon: float,
) -> ShapeProjection:
    """Project one point onto the nearest polyline SEGMENT of ``geom``.

    For every segment, the parametric projection ``t`` is clamped to
    ``[0, 1]`` and the squared metre-space distance to the clamped point
    is evaluated; the global minimum wins. On exact distance ties
    between segments the LOWEST ``segment_index`` is kept — this is
    serialization determinism only, not spatial evidence that one
    segment is more likely than the other.

    Zero-length segments are skipped as projection targets (their
    parametric projection is undefined) but still occupy their
    ``segment_index`` and contribute 0 m to ``along_m`` bookkeeping.

    Raises ``ValueError`` on degenerate geometry: a residual against a
    shape with fewer than two distinct usable vertices would be
    meaningless, and silently producing one is exactly the legacy
    forced-assignment failure this spike exists to remove.
    """
    if not geom.is_projectable():
        raise ValueError(
            "cannot project onto degenerate geometry for scope "
            f"{geom.scope!r} (quality_status={geom.quality_status!r})"
        )

    px, py = _to_xy(lat, lon, geom.cos_lat0)

    best_d2 = math.inf
    best_index = -1
    best_t = 0.0
    best_qx = 0.0
    best_qy = 0.0
    for i, seg_len in enumerate(geom.seg_lengths_m):
        if seg_len == 0.0:
            continue
        ax, ay = geom.xs[i], geom.ys[i]
        bx, by = geom.xs[i + 1], geom.ys[i + 1]
        t = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / (seg_len * seg_len)
        t = min(1.0, max(0.0, t))
        qx = ax + t * (bx - ax)
        qy = ay + t * (by - ay)
        d2 = (px - qx) ** 2 + (py - qy) ** 2
        if d2 < best_d2:  # strict < keeps the lowest index on exact ties
            best_d2 = d2
            best_index = i
            best_t = t
            best_qx = qx
            best_qy = qy

    aligned_lat, aligned_lon = _to_lat_lon(best_qx, best_qy, geom.cos_lat0)
    along_m = geom.cum_lengths_m[best_index] + best_t * geom.seg_lengths_m[best_index]
    return ShapeProjection(
        scope=geom.scope,
        aligned_lat=aligned_lat,
        aligned_lon=aligned_lon,
        residual_m=math.sqrt(best_d2),
        along_m=along_m,
        segment_index=best_index,
        segment_fraction=best_t,
        method=PROJECTION_METHOD,
    )


def coords_structurally_valid(lat: float | None, lon: float | None) -> bool:
    """True when a lat/lon pair is structurally usable as a position.

    False for ``None``, NaN, out-of-range values (``|lat| > 90``,
    ``|lon| > 180``), and the (0, 0) null-island export artifact
    (both coordinates within 1e-6 degrees of zero simultaneously).
    """
    if lat is None or lon is None:
        return False
    if math.isnan(lat) or math.isnan(lon):
        return False
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return False
    if abs(lat) < _NULL_ISLAND_EPS_DEG and abs(lon) < _NULL_ISLAND_EPS_DEG:
        return False
    return True


def assess_point(
    geom: ShapeGeometry | None,
    lat: float | None,
    lon: float | None,
    *,
    soft_bound_m: float,
    hard_bound_m: float,
) -> SpatialAssessment:
    """Classify one point's spatial evidence against one scoped geometry.

    ``soft_bound_m`` / ``hard_bound_m`` are caller-supplied EXPERIMENTAL,
    UNAPPROVED thresholds (keyword-only so call sites stay explicit).

    Rules:

    * structurally invalid coordinates -> ``MISSING`` with
      ``Reason.GEOM_INVALID_COORDS`` (no projection attempted);
    * ``geom`` is ``None`` or degenerate -> ``MISSING`` with
      ``Reason.GEOM_MISSING``;
    * ``residual_m <= soft_bound_m`` -> ``PRESENT``, no reason codes;
    * ``soft_bound_m < residual_m <= hard_bound_m`` -> ``WEAK`` with
      ``Reason.GEOM_WEAK``, projection retained;
    * ``residual_m > hard_bound_m`` -> ``HARD_CONTRADICTION`` with
      ``Reason.GEOM_HARD_CONTRADICTION``, projection STILL retained —
      contradiction is an evidence state for diagnostics, never a
      forced re-assignment and never data destruction.
    """
    if not (0.0 <= soft_bound_m <= hard_bound_m):
        raise ValueError(
            "bounds must satisfy 0 <= soft_bound_m <= hard_bound_m; got "
            f"soft_bound_m={soft_bound_m!r}, hard_bound_m={hard_bound_m!r}"
        )

    if not coords_structurally_valid(lat, lon):
        return SpatialAssessment(
            evidence=SpatialEvidence.MISSING,
            projection=None,
            reason_codes=(Reason.GEOM_INVALID_COORDS,),
        )
    if geom is None or not geom.is_projectable():
        return SpatialAssessment(
            evidence=SpatialEvidence.MISSING,
            projection=None,
            reason_codes=(Reason.GEOM_MISSING,),
        )

    assert lat is not None and lon is not None  # narrowed by validity check
    projection = project_point(geom, lat, lon)
    if projection.residual_m <= soft_bound_m:
        return SpatialAssessment(
            evidence=SpatialEvidence.PRESENT,
            projection=projection,
            reason_codes=(),
        )
    if projection.residual_m <= hard_bound_m:
        return SpatialAssessment(
            evidence=SpatialEvidence.WEAK,
            projection=projection,
            reason_codes=(Reason.GEOM_WEAK,),
        )
    return SpatialAssessment(
        evidence=SpatialEvidence.HARD_CONTRADICTION,
        projection=projection,
        reason_codes=(Reason.GEOM_HARD_CONTRADICTION,),
    )


def build_candidate_geometries(
    shapes: pl.DataFrame,
    scopes: list[GeometryScope],
) -> dict[tuple[str, str, int | None, str], ShapeGeometry]:
    """Build one scoped geometry per candidate scope.

    Key = ``(feed_id, route_id, direction_id, shape_id)``; one entry per
    scope. There is deliberately NO nearest-shape convenience in this
    module: the legacy pipeline's anonymous nearest-shape fallback is a
    failure mode, not a feature. Callers must assess a point against
    every scoped candidate geometry separately and compare the resulting
    evidence — no API here picks a winner automatically.
    """
    return {
        (
            scope.feed_id,
            scope.route_id,
            scope.direction_id,
            scope.shape_id,
        ): build_shape_geometry(shapes, scope)
        for scope in scopes
    }
