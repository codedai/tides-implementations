"""Trusted local data root, separate from immutable executable code snapshots.

Only the local launcher sets this environment variable. It is deliberately not
a web request parameter, and defaults preserve the existing repository layout.
"""

import os
from pathlib import Path


def project_root():
    configured = os.environ.get("TIDES_WORKBENCH_DATA_ROOT")
    root = Path(configured) if configured else Path(__file__).resolve().parents[2]
    if not root.is_absolute():
        raise ValueError("TIDES_WORKBENCH_DATA_ROOT must be an absolute directory")
    if not root.is_dir():
        raise ValueError("TIDES_WORKBENCH_DATA_ROOT does not exist")
    return root.resolve()
