# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A community showcase of TIDES (Transit Integrated Data Exchange Specification) implementations, stewarded by MobilityData. It is **not a single software project** — it is a collection of independent contributor subfolders, each owned and maintained by the contributing organization:

- `agencies/<org>/` — transit agencies (currently `mountain-line` and `wmata`)
- `vendors/<org>/` — vendors and consultancies (currently empty)

Most content is markdown. Contributor subfolders are expected to have a root `README.md` covering the organization, a primary contact, what the implementation covers, and related resources (see `CONTRIBUTING.md`) — though the existing `agencies/wmata/` folder was merged without one; do not add one unprompted. Everything is Apache 2.0; contributors retain copyright. PRs go against `main`; maintainer review covers tone, clarity, structure, and accidental disclosure of sensitive content (credentials, internal URLs) — not technical correctness of contributed code.

`agencies/wmata/` has two parts: `sdh-open-source/` (the codebase below) and `sdh-writeup/wmata-sdh-writeup.md`, a prose case study of the same SMART Data Hub architecture. Architecture-level changes to one may need matching edits in the other.

## The one real codebase: `agencies/wmata/sdh-open-source/`

A **redacted snapshot** of WMATA's SMART Data Hub production repo, published as a reference implementation. Placeholders like `[AGENCY]`, `[ORGANIZATION]`, and `[project-name]` appear throughout docs intentionally — do not "fix" them. Files removed in redaction that docs still reference: `warehouse/profiles.yml.template` and the whole `warehouse/data-load-scripts/` directory — so the sample-data loading workflow in `docs/warehouse/` is reference-only in this snapshot. (`.vscode/` does exist, with settings, tasks, and launch configs.) Secrets, sample data, and `warehouse/profiles.yml` are gitignored; never commit them.

### Fresh-clone prerequisites

Several documented commands fail on a fresh clone until you know this:

- **`warehouse/profiles.yml` is gitignored, absent, and its template was redacted away.** It is required by sqlfluff (dbt templater with `target = local`, configured in the root `pyproject.toml`), by `dagster dev` (which runs `dbt parse` at startup via `prepare_if_dev()` in `pipelines/dbt/project.py`), and transitively by any pytest that imports `pipelines.definitions`. Fix: create a minimal DuckDB profile named `warehouse` with a `local` target and run `dbt deps` in `warehouse/` — the "Setup dbt" step in `.github/workflows/pre_commit.yaml` shows the exact profile.
- **Full `pytest` needs Oracle Instant Client, even though tests are mocked.** `pipelines/tests/conftest.py` sets mock `AZURE_*` vars, which disables demo mode, so importing `pipelines.env` calls `oracledb.init_oracle_client()` (DPI-1047 without it). `test_definitions.py` additionally needs the dbt manifest (`warehouse/target/manifest.json`, produced by `dbt parse` or a `dagster dev` startup). Individual test files that avoid `pipelines.definitions` run without the manifest.
- **`dagster dev` does not need a `.env`.** When `AZURE_TENANT_ID` is unset, `pipelines/env.py` enables demo mode: a stub Azure credential is substituted, definitions load and the UI is browsable, but pipelines cannot execute. A populated `.env` (see `docs/pipelines/README.md`) is needed only to actually run pipelines.

### Commands

All commands run from `agencies/wmata/sdh-open-source/` unless noted. Python 3.12 + `uv` (default groups `dagster-dev` + `dbt-dev` cover everything). Prefix with `uv run` if the `.venv` is not activated; `uv run --env-file .env ...` for commands needing environment variables.

```sh
uv sync                          # install all dev dependencies (from sdh-open-source root)
pre-commit run --all-files       # ruff + ruff-format, sqlfluff, markdownlint, nbstripout, check-merge-conflict

# Dagster — run from sdh-open-source root (code location is pipelines.definitions)
dagster dev                      # UI at http://localhost:3000

# dbt — must cd into warehouse/ first
dbt run                          # all models
dbt run --select model_name      # one model (no .sql suffix)
dbt test                         # all dbt tests

# Python tests (Dagster pipelines)
pytest -m unit                             # markers: unit, integration, slow, dagster
pytest pipelines/tests/test_utils.py       # single file
pytest --cov --cov-report=html             # coverage report in htmlcov/

# Linters directly
ruff check . --fix
sqlfluff fix path/to/model.sql   # dbt-templated; dialect duckdb; config in root pyproject.toml

# Docs site
uv run python docs/sphinx/build.py         # output: docs/sphinx/build/index.html
```

The required CI check (`.github/workflows/pre_commit.yaml`) is **not** `pre-commit run --all-files`: it lints only files changed vs `main`, using dedicated ruff/markdownlint actions and the manual-stage `sqlfluff-lint-ci` hook. It will not flag pre-existing repo-wide violations.

For the docs site, edit `docs/sphinx/templates/` and the original `docs/**/*.md` only — `docs/sphinx/source/` is generated output that `build.py` deletes and regenerates on every run.

### Architecture

ELT lakehouse: Dagster ingests source data (Oracle DBs, agency APIs → Azure Blob Storage as Apache Iceberg tables via Lakekeeper catalog), dbt transforms it into TIDES-compliant and analytical tables, queried through Trino in production or DuckDB locally. OpenMetadata harvests lineage from dbt/Trino/Dagster. Infra is OpenTofu in `tf/`.

**Pipelines (`pipelines/`)** — one package per data domain (`gtfs/`, `bus_info/`, `realtime_bus_info/`, `bus_ridership/`, `faregate/`, `open_pay/`, `openmetadata/`, `iceberg_operations/`), each exposing a `definitions` module. `pipelines/definitions.py` merges them all via `Definitions.merge()` plus a shared `azure_credential` resource from `env.py`.

- **dbt-in-Dagster wiring** (spread across `pipelines/dbt/assets.py`, `pipelines/dbt/sensors.py`, and the `+meta: dagster: group:` settings in `dbt_project.yml`): all dbt models run as one `@dbt_assets` multi-asset executing `dbt build`. GTFS-group models (`bronze_gtfs`/`silver_gtfs`) are deliberately excluded from automation and instead triggered by `gtfs_dbt_sensor` on new `feed_hash` partitions; every other dbt model gets an eager `AutomationCondition` evaluated by `dbt_automation_sensor`, which **defaults to STOPPED** — nothing auto-materializes until that sensor is started. Warn-severity dbt test results are rewritten to passed in the asset stream.
- **The `[Project Name]_ENVIRONMENT` env var** (default `dev`) couples two behaviors: in dev, `env.py` sets `ROW_LIMIT = 100`, applied as a ROWNUM cap to every Oracle ingestion query (`resources/utils.py`), and `partitions.py` restricts partitions to narrow fixed historical windows; `stg` removes the cap and uses open-ended partitions. Truncated local data is this switch, not a bug.

**Warehouse (`warehouse/`)** — dbt project on a medallion layout where dbt folders map to schemas: `staging/` and `intermediate/` → `bronze_*`, `mart/` → `silver_*`, `metrics/` → `gold_*`, subdivided by domain (gtfs, bus, fares, faregate, rail, ridership). Caveat: those schema names materialize verbatim only on targets whose name starts with `deployed`; on all other targets (including local DuckDB) `macros/generate_schema_name.sql` prefixes them with the profile's schema, e.g. `<target.schema>_bronze_gtfs`.

- Most models carry `enabled: "{{ target.name != 'consultant' }}"` — a legacy target that disables nearly everything except GTFS.
- `enable_realtime` var toggles AVL/realtime models (`dbt run --vars '{"enable_realtime": false}'`).
- Incremental models use microbatch with `incremental_begin_date` and `microbatch_lookback_days` vars.
- **All warehouse SQL must run on both DuckDB (local) and Trino (production).** Dialect-specific SQL goes through macros in `warehouse/macros/dialect_agnostic_*.sql` that branch on `target.type` (e.g. `parse_datetime`, `gtfs_time_string_to_interval`, `first_agg`). Macro names do not match file names — grep for the macro name, not the file.
- Packages: `dbt_utils` with `trino_utils` first in dispatch order.

### Deployment and infrastructure

Production runs on **Azure Container Apps** — `docs/dagster/deployment.md` is stale (AKS/Helm era, references nonexistent `tf/*.tf` files); trust `docs/infrastructure/README.md` and `tf/envs/README.md` instead.

- Each `tf/envs/<env>` (dev, stg, poc) is a self-contained OpenTofu root: `az login`, then `cd tf/envs/<env> && tofu init/plan/apply`. Remote state lives in an Azure Storage account bootstrapped once by `tf/create_state_storage.sh`. Three flags in each env's gitignored `.auto.tfvars` (`has_network`, `has_db_registration`, `has_entra`) gate resources by deployment stage — a near-empty plan under false flags is expected, not breakage.
- Image deploy chain (from sdh-open-source root; each script reads `.env`): `scripts/pull-dotenv <env>` (writes `.env` from Key Vault) → `scripts/build-dagster-containers` (podman-builds two `pipelines/Containerfile` targets and bakes a Trino JWT in at build time, so it needs `az login` and a populated `.env`) → `scripts/push-dagster-containers` → `scripts/restart-dagster-containers`. `scripts/bootstrap-dev` is a full Ubuntu/WSL machine bootstrap (sudo apt installs, Oracle Instant Client, appends to `~/.bashrc`) — do not run it in an already-configured environment.
- Auth model: Entra ID OAuth2 for humans, OAuth2 client credentials service-to-service (Dagster→Trino/Lakekeeper etc.), Trino bcrypt password users for JDBC tools, user-assigned managed identities for Azure resources, all secrets in Key Vault. See `docs/authentication/overview.md` and `docs/infrastructure/`.

### Testing philosophy

(docs/pipelines/testing_dagster.md): data expectations (columns, non-nulls) belong in dbt tests; Dagster gets runtime asset checks plus selective pytest unit tests of helpers and error handling. Avoid mock-heavy integration and end-to-end tests.

### Conventions

Conventional commit messages; trunk-based short-lived branches rebased on main; SQL style enforced by sqlfluff (lowercase keywords/identifiers, dbt templater, config in the root `pyproject.toml`); Python by ruff (line length 88). Exploratory work goes in `warehouse/scratch/` (gitignored).

### Key docs

- `docs/CONTRIBUTING.md` — environment setup, workflow
- `docs/warehouse/README.md` and `docs/warehouse/sample_data_workflow.md` — dbt/DuckDB local development (sample-data loading scripts were removed in redaction; reference only)
- `docs/warehouse/dbt_tests_quality.md` — dbt testing guidance
- `docs/pipelines/README.md` — Dagster pipeline setup (env vars, Oracle client, Key Vault)
- `docs/infrastructure/README.md` and `tf/envs/README.md` — deployment and infrastructure (authoritative; prefer over the stale `docs/dagster/deployment.md`)
