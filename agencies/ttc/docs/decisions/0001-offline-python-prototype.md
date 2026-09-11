# ADR 0001: Offline Python prototype architecture

- Status: accepted for the prototype
- Date: 2026-08-05

## Context

The WMATA reference implementation demonstrates a production-oriented TIDES data hub built around cloud storage, Dagster, dbt/SQL, distributed query infrastructure, and metadata services. The TTC work currently needs to investigate whether local GTFS, AVL, and APC exports can be reconciled and converted to TIDES. Fast iteration, transparent matching experiments, and offline reproducibility matter more than production operations.

The available fixture includes an 840 MB AVL CSV, a 97 MB APC CSV, and 441 MB of compressed, overlapping GTFS versions. The data is large enough to require efficient scans but does not justify a distributed platform.

## Decision

Build the proof of concept as a local Python ETL package with:

- immutable inputs under `database_offline/`;
- Python transformations, with Polars as the planned tabular engine;
- Parquet landing, normalized, matching, and quality artifacts;
- explicit source lineage and deterministic identifiers;
- a curated, versioned GTFS feed manifest;
- TIDES v1.0 CSV resources and `datapackage.json` as the final output;
- Frictionless and custom cross-resource validation;
- a future simple CLI rather than a production orchestrator.

Retain WMATA's layered staging/intermediate/quality/final reasoning, GTFS versioning, and traceable rejection pattern. Do not adopt its SQL warehouse or infrastructure stack for this prototype.

## Consequences

### Positive

- The full concept can run without network or service credentials.
- Matching logic can be expressed, tested, and profiled directly in Python.
- Parquet avoids repeatedly parsing the large CSV inputs during experiments.
- The structure can later be orchestrated or deployed without coupling the investigation to a platform now.
- Quality and unmatched data remain visible rather than disappearing during joins.

### Costs and constraints

- The project must implement its own lightweight stage orchestration and run metadata.
- Memory behavior must be tested; lazy scans and partitioned writes are required.
- Parquet is an internal format only. A separate publication step must enforce canonical TIDES CSV schemas.
- A local file pipeline does not provide production concurrency, access control, scheduling, or catalog services.
- Scheduled-trip matching remains agency-specific investigative logic even though the final tables are standardized.

## Alternatives not selected

### Copy the WMATA stack

Not selected because cloud services, a SQL warehouse, dbt, and production orchestration would add setup and operational questions before the TTC matching concept is proven.

### DuckDB plus dbt

Not selected because it would retain SQL as the transformation language, contrary to the project's explicit Python-processing constraint. DuckDB can still be used manually by analysts outside the pipeline contract.

### In-memory pandas-only processing

Not selected as the default because the AVL input is large enough that expanded in-memory representation may be unnecessarily expensive. The planned interface should support lazy and streaming execution.

### Write final TIDES CSV directly from raw files

Not selected because GTFS version selection, service-date inference, trip segmentation, source reconciliation, and quality review require inspectable intermediate results.

## Revisit conditions

Revisit this decision after the offline proof succeeds or if measurements show that the local execution model cannot process the fixture within acceptable memory and time. Production scheduling, databases, or cloud deployment require a separate ADR and are not implied by this decision.
