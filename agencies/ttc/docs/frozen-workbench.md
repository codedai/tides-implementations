# Frozen workbench: 2023-11-06 · route 29

The current reproducible profile is **04c**, derived from **04**, not 04b.
The release retains complete 03d, 04, 04b and 04c settings, including all
thresholds and all four decision modes. Earlier frozen snapshots remain intact.
This is a diagnostic experiment, not independently calibrated trip certification.

## One command

From `agencies/ttc`, rebuild directly from the original CSV files, then inspect
the new results in the browser:

```sh
./workbench.sh run --serve --port 8780
```

Open `http://127.0.0.1:8780`. Without `--serve`, the command generates results and
exits. `--workers 1` reduces CPU concurrency; the default is 8. Each run writes a
new directory under `artifacts/workbench-builds/`; existing results are never
overwritten. `--output` can specify another **new** directory.

To inspect the retained 03d/04/04b/04c workspace instead of rebuilding it:

```sh
./workbench.sh serve
```

Open `http://127.0.0.1:8767`. This serves saved results without regenerating an
obsolete baseline. Existing review notes remain in the existing review database.

To verify code, configuration, dependencies and raw input identities:

```sh
./workbench.sh verify
```

The existing local environment is used offline. On another installation, restore
the pinned Python and dependencies with `uv sync --frozen` first; raw files must
be provided separately. The launcher fails on a version/hash mismatch rather
than silently changing the experiment.

## Frozen execution

`config/workbench-release.json` selects a SHA-256-locked snapshot under
`releases/`. It contains the business Python code, web assets, experiment
profiles, `uv.lock`, Python/package versions, and hashes of raw CSVs, the feed
manifest, and relevant GTFS archives. It contains no large result copies.
The launcher verifies the snapshot and re-executes its entry point; subsequent
edits to working business modules do not silently change this release.

The dependency chain is:

1. Stream the original AVL/APC CSVs into a single-day normalized input, retaining
   full-file source row identities, payload hashes and genuine timestamps.
2. Deduplicate AVL, remove PI/PO, segment by direction/destination and the joint
   30-minute-gap policy.
3. Select route 29, build canonical GTFS references, sequence-project observations,
   and match using the selected frozen profile (default 04c) in all four modes.
4. Publish candidate components, diagnostics, overview and shadow-evaluation
   artifacts; validate output hashes and browser-facing payloads.

No old fixture, Exp01/01b/02 result, or saved Exp03 projection is an input to a
fresh rebuild. Preparation artifacts are generated inside the new output.
`build-inputs.json`, `build-complete.json` (or `build-failure.json`) record the
release, inputs, scope and outcome. Direct builds label shadow reports
`NO_COMPARATOR`; no benchmark accuracy is invented.

Profiles `--profile 03d`, `--profile 04` and `--profile 04b` remain available with their
saved settings. A fresh run has new provenance and is not claimed to be a
byte-identical historical replay; historic 04/04b were score replays of earlier
projection artifacts. The retained original result files are unchanged.

## Single-global case storage

New four-mode case files store the full global result only at
`matching_modes.global.result`. The duplicate top-level `result` is omitted and
the file carries this transport descriptor:

```json
{"_case_storage":{"schema":"gtfs-case-single-global-v1","global_result_path":"matching_modes.global.result"}}
```

This changes serialization only, not experiment settings or matching mathematics.
No candidate, per-point trace, window score or partial-mode result is dropped.
`runner.read_case`, the workbench adapter and audit readers accept both historical
unmarked files and the new marked format. They reconstruct the legacy top-level
entry with an independent in-memory copy, so modifying one access path does not
unexpectedly modify the other. The storage marker is not exposed to matching code.
Unknown/malformed markers, missing reference targets and conflicting global copies
fail closed. Raw `json.load` is not a replacement for the version-aware reader.

Output SHA-256 pins cover the actual compressed file, not its decoded contents.
New build/replay provenance includes the codec; the matcher scoring fingerprint
does not change merely because its transport changes. Existing 03d–04c artifacts
have **not** been migrated or edited: their contents, hashes and disk usage remain
unchanged. A historical migration must separately verify semantic equality and
publish updated storage provenance; do not manually remove fields from pinned runs.

## Retention and careful cleanup

Full 03d, 04, 04b and 04c results, original CSV/GTFS data, exported CSVs, human notes,
and their supporting day fixture are retained. The old direction-stage files
still needed by retained raw-detail views are copied into a small checksummed
shared checkpoint in `artifacts/merge-debug-20231106/retention/`.

The retention registry is a physical-location overlay only. Original retained
manifests, output inventories and review fingerprints are not rewritten. It is
used only when the old parent directory is absent; a corrupt original parent
must not be bypassed. Both the snapshot and live fixture/inventory are checked.
The website's rerun action resolves the same verified checkpoint when its selected
GTFS parent refers to the retired direction run. New results pin the checkpoint's
actual path. Fresh raw rebuilds still generate their own direction preparation.
Append-only score replays can inherit a registered source's retained parent only
through the same workspace's completed, hash-bound source lineage. Source metadata
and the complete pin inventory must agree, parent identities/pins cannot change,
and cyclic, excessive-depth, symlink or tampered chains fail closed. This is a
read-only location resolution: no old manifests or registry bindings are rewritten.
It is not a claim to rehash every historical case or recompute its scores.

Cleanup uses an explicit file inventory and SHA-256 protection of retained
results. Files are first moved into reversible quarantine. Only after a cold
server checks all retained vehicles and all four matching modes can the
quarantine be purged. Audit reports and small deleted-run metadata survive;
large deleted result files do not, but can be recomputed as new experiments.

Deleting 03c removes the live input of 03d's historical score replay. A
pre-retention hash attestation records that those inputs passed before deletion.
It does **not** claim they remain available or that the historical computation
was rerun. Strict historical replay audits requiring 03c will report missing
input; current result inspection and fresh raw rebuilds do not require it.
