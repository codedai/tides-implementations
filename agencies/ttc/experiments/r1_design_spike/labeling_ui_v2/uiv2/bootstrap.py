"""Make the spike's ``r1spike`` package importable from this standalone app.

The v2 UI lives in ``experiments/r1_design_spike/labeling_ui_v2/`` with its
own venv; the spike source lives in ``experiments/r1_design_spike/src/``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_r1spike_importable() -> None:
    """Put ``experiments/r1_design_spike/src`` first on sys.path if needed.

    Idempotent: does nothing when ``r1spike`` already imports, and never
    inserts the path twice.
    """
    try:
        import r1spike  # noqa: F401
    except ImportError:
        pass
    else:
        return
    src = str(Path(__file__).resolve().parents[2] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
