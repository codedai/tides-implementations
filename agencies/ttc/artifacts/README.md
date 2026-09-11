# Generated artifacts

This directory is reserved for local pipeline output and is ignored by Git except for this README.

Planned run layout:

```text
artifacts/
└── <run-id>/
    ├── landing/
    ├── normalized/
    ├── matched/
    ├── quality/
    ├── tides/
    │   ├── datapackage.json
    │   └── *.csv
    └── reports/
```

Artifacts are disposable in the sense that they can be rebuilt, but the pipeline must never delete or overwrite another run implicitly. A run manifest should identify the exact source hashes, configuration, code version, and TIDES schema reference used to create them.
