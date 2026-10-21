# Python package

The GTFS preprocessing stage is implemented; AVL/APC ingest, cross-source
reconciliation, TIDES construction, and publication are still future work.
The current package layout is:

```text
src/
└── ttc_tides/
    ├── ingest/gtfs.py       # ZIP identity, inventory, streaming member reads, lineage
    ├── normalize/gtfs.py    # Typed fields and row quarantine
    ├── reconcile/feeds.py   # Manifest validation, service coverage, feed precedence
    ├── reconcile/schedule.py# Daily service, trips, stop events, and p2 patterns
    ├── quality/gtfs.py      # Conservation, integrity, and plausibility findings
    └── cli.py               # Offline GTFS commands
```

Future stages should add modules only when their contracts are implemented;
the tree above does not contain placeholder production APIs.

Package-boundary rules:

- ingest modules do not perform TIDES mapping;
- source-specific normalization does not import publication code;
- reconciliation produces evidence and scores, not forced matches;
- TIDES builders consume reconciled contracts rather than raw CSVs;
- quality annotation precedes valid-output filtering;
- publication never writes into `database_offline/`;
- all identifiers derived by code are deterministic and versioned.

Implementation should follow [`../docs/architecture.md`](../docs/architecture.md) rather than treating this illustrative module tree as a final API.
