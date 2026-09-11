import numpy as np

from labeling import geometry


def test_project_onto_straight_line():
    # 1 km straight line east at lat 0-ish scale
    vx = np.array([0.0, 1000.0])
    vy = np.array([0.0, 0.0])
    px = np.array([250.0, 990.0, -50.0])
    py = np.array([30.0, -10.0, 0.0])
    arc, dist = geometry.project_points_onto_polyline(px, py, vx, vy)
    assert np.allclose(arc, [250.0, 990.0, 0.0])
    assert np.allclose(dist, [30.0, 10.0, 50.0])


def test_project_picks_nearest_segment():
    # L-shaped polyline
    vx = np.array([0.0, 100.0, 100.0])
    vy = np.array([0.0, 0.0, 100.0])
    arc, dist = geometry.project_points_onto_polyline(
        np.array([110.0]), np.array([50.0]), vx, vy
    )
    assert np.isclose(arc[0], 150.0)  # 100 along leg 1 + 50 up leg 2
    assert np.isclose(dist[0], 10.0)


def test_direction_consistency():
    assert geometry.direction_consistency(np.arange(0, 2000, 100)) == 1.0
    assert geometry.direction_consistency(np.arange(2000, 0, -100)) == -1.0
    mixed = geometry.direction_consistency(
        np.array([0, 100, 50, 150, 100, 200, 150, 250])
    )
    assert mixed is not None and -0.5 < mixed < 0.5
    assert geometry.direction_consistency(np.array([0.0, 1.0])) is None  # jitter only


def test_empty_inputs():
    arc, dist = geometry.project_points_onto_polyline(
        np.array([]), np.array([]), np.array([0.0, 1.0]), np.array([0.0, 1.0])
    )
    assert len(arc) == 0
    assert geometry.min_dist_to_points(
        np.array([1.0]), np.array([1.0]), np.array([]), np.array([])
    )[0] != 0  # nan
    assert len(geometry.cumulative_distance_m(np.array([]), np.array([]))) == 0
