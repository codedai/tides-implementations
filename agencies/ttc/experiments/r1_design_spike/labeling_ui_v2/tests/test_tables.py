"""Markdown table renderer: escaping and honest truncation."""

from __future__ import annotations

import sys
import types

from uiv2.tables import show_table


class _Capture:
    def __init__(self) -> None:
        self.markdown_calls: list[str] = []
        self.caption_calls: list[str] = []

    def markdown(self, text: str) -> None:
        self.markdown_calls.append(text)

    def caption(self, text: str) -> None:
        self.caption_calls.append(text)


def _run(rows, **kwargs) -> _Capture:
    capture = _Capture()
    fake = types.ModuleType("streamlit")
    fake.markdown = capture.markdown
    fake.caption = capture.caption
    original = sys.modules.get("streamlit")
    sys.modules["streamlit"] = fake
    try:
        show_table(rows, **kwargs)
    finally:
        if original is not None:
            sys.modules["streamlit"] = original
        else:
            del sys.modules["streamlit"]
    return capture


def test_pipe_and_newline_escaping_and_none_and_floats():
    capture = _run([{"a": "x|y", "b": "line1\nline2", "c": None, "d": 1.2345}])
    rendered = capture.markdown_calls[0]
    assert "x\\|y" in rendered
    assert "line1 line2" in rendered
    assert "| 1.2 |" in rendered
    assert "None" not in rendered


def test_truncation_reports_exact_hidden_count():
    rows = [{"n": i} for i in range(30)]
    capture = _run(rows, max_rows=12)
    assert capture.markdown_calls[0].count("\n") == 12 + 1  # header + sep + 12
    assert capture.caption_calls == ["…… 另有 18 行未显示"]


def test_empty_rows_render_placeholder():
    capture = _run([])
    assert capture.caption_calls == ["（空）"]
