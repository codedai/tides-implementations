# Tests

Unit tests for the GTFS preprocessing stage live in `unit/`, built entirely on synthetic in-memory fixtures from `conftest.py`. They cover structural ZIP/header failures, archive identity, typed parsing and quarantine, exception-only calendars, feed approval and precedence, post-midnight/DST time behavior, stable stop-pattern identifiers, schedule integrity, deterministic output, and CLI source-boundary safety.

The eventual full multi-source layout should separate:

```text
tests/
├── fixtures/       # Small synthetic GTFS/AVL/APC inputs safe to commit
├── unit/           # Pure parsing, matching, identifier, and validation behavior
├── integration/    # Multi-source pipeline stages over synthetic fixtures
└── acceptance/     # Deterministic package build and TIDES v1.0 validation
```

Real source extracts must not be copied into test fixtures unless redistribution permission is confirmed. Synthetic fixtures should cover midnight service, overlapping GTFS feeds, changed route IDs, stop-code mapping, loops, ambiguous trips, duplicates, invalid values, and deterministic reruns.

The full local data may be used for opt-in profiling and acceptance checks, but ordinary tests should remain fast and self-contained.
