import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# real artifacts (present in this workspace; tests that need them skip cleanly
# elsewhere)
ARTIFACTS = APP_DIR.parents[1] / "artifacts"
CASES_CSV = ARTIFACTS / "experimental-r1-spike02r-p4" / "labeling_packet_redacted.csv"

requires_artifacts = pytest.mark.skipif(
    not CASES_CSV.exists(), reason="p4 run artifacts not present"
)
