"""Markdown table rendering.

``st.dataframe`` is FORBIDDEN in this codebase: its pandas -> pyarrow
serialization segfaults on this machine (polars + pyarrow + protobuf loaded
in the same streamlit script-runner thread). Tables here are small, so a
Markdown pipe table is fine.

Adapted from the ``show_table`` helper in the v1 UI's ``app.py``; v1 stays
frozen because the audit hashes it.
"""

from __future__ import annotations

import polars as pl


def _cell(v: object) -> str:
    if v is None:
        s = ""
    elif isinstance(v, float):
        s = f"{v:.1f}"
    else:
        s = str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def show_table(data: pl.DataFrame | list[dict], *, max_rows: int | None = None) -> None:
    """Render a compact table as a Markdown pipe table via ``st.markdown``.

    None renders as "", floats to 1 decimal; ``|`` is escaped and newlines
    are stripped inside cells. When ``max_rows`` truncates, a visible
    "... N more rows hidden" line reports the exact hidden count — rows are
    never silently dropped.
    """
    import streamlit as st  # lazy: importing this module must not need streamlit

    rows = data.to_dicts() if isinstance(data, pl.DataFrame) else data
    if not rows:
        st.caption("(empty)")
        return
    hidden = 0
    if max_rows is not None and len(rows) > max_rows:
        hidden = len(rows) - max_rows
        rows = rows[:max_rows]
    cols = list(rows[0].keys())
    lines = [
        "| " + " | ".join(_cell(c) for c in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    lines += ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows]
    st.markdown("\n".join(lines))
    if hidden:
        st.caption(f"… {hidden} more rows hidden")
