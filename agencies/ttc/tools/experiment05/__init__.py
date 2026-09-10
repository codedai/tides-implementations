"""Experiment 05: date-selected, source-preserving batch transit pipeline."""

PIPELINE_VERSION = "experiment-05-v1"
STAGES = (
    "load_raw", "normalize", "event_timeline", "project_avl", "segment_trips",
    "attach_apc", "generate_candidates", "assign_scheduled", "map_tides",
    "validate_publish",
)
