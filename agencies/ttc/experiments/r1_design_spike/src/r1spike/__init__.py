"""Experimental R1 design-validation spike.

This package is an isolated, non-publishable experiment. It is NOT a
production implementation of S3-S8, does not implement the final S7
contract, and must never claim TIDES conformance or close R1.

Authorization basis: experimental design-validation spike; not
ADR-approved S3+ production work (see ADR 0002).
"""

PROFILE = "diagnostic-experimental-r1-spike"
PUBLISHABLE = False
TIDES_CONFORMANCE_CLAIMED = False
AUTHORIZATION_BASIS = (
    "experimental design-validation spike; not ADR-approved S3+ production"
)
# Composite experimental algorithm version for every module in this spike.
# Bumped when spike output semantics change. Never reused for production.
ALGORITHM_VERSION = "x-r1-spike-1"
