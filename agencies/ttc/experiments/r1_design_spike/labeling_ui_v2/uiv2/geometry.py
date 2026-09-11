"""Small planar geometry helpers.

Vendored verbatim from the v1 UI (``tools/labeling_ui/labeling/geometry.py``)
for the v2 project; v1 stays frozen because the audit hashes it.

City-scale distances only, so an equirectangular projection around a local
reference latitude is accurate enough (< 0.1% error over ~20 km).
"""

from __future__ import annotations

import math

import numpy as np

EARTH_R_M = 6_371_000.0


def to_xy(
    lat: np.ndarray, lon: np.ndarray, ref_lat_deg: float
) -> tuple[np.ndarray, np.ndarray]:
    """Degrees -> meters in a local equirectangular frame."""
    k = math.cos(math.radians(ref_lat_deg))
    x = np.radians(np.asarray(lon, dtype=float)) * EARTH_R_M * k
    y = np.radians(np.asarray(lat, dtype=float)) * EARTH_R_M
    return x, y


def polyline_arc_lengths(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cumulative arc length at each vertex, starting at 0."""
    if len(x) == 0:
        return np.array([])
    seg = np.hypot(np.diff(x), np.diff(y))
    return np.concatenate([[0.0], np.cumsum(seg)])


def project_points_onto_polyline(
    px: np.ndarray,
    py: np.ndarray,
    vx: np.ndarray,
    vy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project points (px, py) onto the polyline with vertices (vx, vy).

    Returns ``(arc_m, dist_m)``: arc-length position of the closest point on
    the line and the perpendicular distance to it, per input point.
    Vectorized over segments; fine for a few hundred points x a few thousand
    vertices.
    """
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    if len(vx) == 0 or len(px) == 0:
        return np.full(len(px), np.nan), np.full(len(px), np.nan)
    if len(vx) == 1:
        d = np.hypot(px - vx[0], py - vy[0])
        return np.zeros(len(px)), d

    ax, ay = vx[:-1], vy[:-1]  # segment starts
    dx, dy = np.diff(vx), np.diff(vy)  # segment vectors
    seg_len2 = dx * dx + dy * dy
    seg_len2 = np.where(seg_len2 == 0, 1e-12, seg_len2)
    arc0 = polyline_arc_lengths(vx, vy)[:-1]

    # (n_points, n_segments) parameter of the closest point on each segment
    t = ((px[:, None] - ax) * dx + (py[:, None] - ay) * dy) / seg_len2
    t = np.clip(t, 0.0, 1.0)
    cx = ax + t * dx
    cy = ay + t * dy
    d2 = (px[:, None] - cx) ** 2 + (py[:, None] - cy) ** 2
    best = np.argmin(d2, axis=1)
    rows = np.arange(len(px))
    arc = arc0[best] + t[rows, best] * np.sqrt(seg_len2[best])
    dist = np.sqrt(d2[rows, best])
    return arc, dist


def min_dist_to_points(
    px: np.ndarray, py: np.ndarray, qx: np.ndarray, qy: np.ndarray
) -> np.ndarray:
    """For each point p, the distance to the nearest q."""
    px = np.asarray(px, dtype=float)
    if len(px) == 0:
        return np.array([])
    if len(qx) == 0:
        return np.full(len(px), np.nan)
    d2 = (px[:, None] - qx) ** 2 + (py[:, None] - qy) ** 2
    return np.sqrt(d2.min(axis=1))


def cumulative_distance_m(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Cumulative along-track distance of an ordered point sequence."""
    lat = np.asarray(lat, dtype=float)
    if len(lat) == 0:
        return np.array([])
    x, y = to_xy(lat, np.asarray(lon, dtype=float), float(np.nanmean(lat)))
    return polyline_arc_lengths(x, y)


def direction_consistency(arc_positions: np.ndarray) -> float | None:
    """How monotonically a time-ordered arc-position sequence moves forward.

    +1: strictly forward along the line, -1: strictly backward, ~0: mixed.
    Steps under 10 m are ignored as jitter. None when there are not enough
    meaningful steps.
    """
    a = np.asarray(arc_positions, dtype=float)
    a = a[~np.isnan(a)]
    if len(a) < 3:
        return None
    steps = np.diff(a)
    steps = steps[np.abs(steps) > 10.0]
    if len(steps) < 2:
        return None
    return float((steps > 0).mean() - (steps < 0).mean())
