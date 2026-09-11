# ttc-labeling-ui-v2

Blind second-pass human labeling UI (protocol v2) for the r1 spike
`scheduled_trip` cases. Standalone uv project; all data is local and
restricted.

## Quick start

```sh
cd agencies/ttc/experiments/r1_design_spike/labeling_ui_v2
uv sync
uv run streamlit run app.py
```

The app auto-discovers `artifacts/human-labeling-v2-run01` and the p4
spike02r run dir (newest `experimental-r1-spike02r-*` containing the
restricted packet). Environment variable overrides:

- `LABELING_V2_RUN_DIR` — the run01 (protocol v2) directory
- `LABELING_V2_ARTIFACTS` — artifacts root
- `LABELING_V2_AUDIT_DIR` — audit trail directory
- `LABELING_V2_REVIEWER` — reviewer identity
- `LABELING_V2_SPIKE02R_DIR` — explicit spike02r run directory

Tests: `uv run pytest`.

## Blind protocol

First-pass labels and model diagnostics (anchor verdicts, qscores) stay
hidden until after the blind save; the post-save reveal is logged to the
audit trail. The v2 UI never reads `labeling_packet_redacted.csv` — its
summary text embeds an anchor-status word.

## Rendering note

Tables render as Markdown (`uiv2.tables.show_table`); `st.dataframe` is
forbidden because its pandas→pyarrow serialization hits a machine-level
arrow fault (segfault) on this machine.
