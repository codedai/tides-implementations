"""Test bootstrap for the R1 design spike.

Adds the spike ``src`` directory to ``sys.path`` so the ``r1spike``
package imports without installing anything. The spike deliberately does
not modify the production ``ttc_tides`` package or its test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

SPIKE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(SPIKE_SRC) not in sys.path:
    sys.path.insert(0, str(SPIKE_SRC))
