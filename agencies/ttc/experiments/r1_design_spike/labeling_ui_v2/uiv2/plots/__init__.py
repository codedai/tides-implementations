"""Plotly evidence figures for the v2 blind labeling UI.

Neutral rendering only: no model vocabulary, no diagnostics, nothing
score-ordered — candidate identity is an alias letter. Builders return
``None`` (never raise) on missing data; :func:`safe_plot` turns that into
a streamlit info message.
"""

from .base import TimeAxis, alias_color, safe_plot
from .boundary_plot import fig_boundary
from .route_map import fig_map
from .stop_ladder import fig_stop_ladder
from .time_progress import fig_time_progress
from .time_tracks import fig_time_tracks
from .timeline import fig_timeline

__all__ = [
    "TimeAxis",
    "alias_color",
    "fig_boundary",
    "fig_map",
    "fig_stop_ladder",
    "fig_time_progress",
    "fig_time_tracks",
    "fig_timeline",
    "safe_plot",
]
