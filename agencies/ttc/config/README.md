# Configuration boundary

This directory will hold reviewed, version-controlled decisions that should not be embedded in Python logic.

Planned structure:

```text
config/
├── feed_manifest.csv       # GTFS archive hashes, validity, precedence, and approval
├── pipeline.yaml           # Timezone, tolerances, matching thresholds, and algorithm versions
└── mappings/
    ├── avl.yaml            # Confirmed source-value mappings such as operational trip types
    └── apc.yaml            # Confirmed APC field and stop-code exceptions
```

No configuration files have been created yet because feed provenance, operational code meanings, tolerances, and mapping exceptions still require investigation. Defaults that materially affect matching or TIDES semantics must eventually live here, include a reason, and be covered by tests.

Secrets and machine-specific absolute paths do not belong in this directory.
