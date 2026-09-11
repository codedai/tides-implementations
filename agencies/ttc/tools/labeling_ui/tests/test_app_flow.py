"""Headless UI flow test with streamlit's AppTest: load the real packet,
pick a label, save, and check the CSV/JSONL outputs."""

import csv

import pytest
from streamlit.testing.v1 import AppTest

from .conftest import APP_DIR, ARTIFACTS, CASES_CSV, requires_artifacts

pytestmark = requires_artifacts


@pytest.fixture()
def at(tmp_path, monkeypatch):
    monkeypatch.setenv("LABELING_UI_CASES", str(CASES_CSV))
    monkeypatch.setenv("LABELING_UI_ARTIFACTS", str(ARTIFACTS))
    monkeypatch.setenv("LABELING_UI_LABELS_DIR", str(tmp_path / "labels"))
    monkeypatch.setenv("LABELING_UI_REVIEWER", "test")
    at = AppTest.from_file(str(APP_DIR / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    return at


def test_app_renders_first_case(at):
    assert any("C-0ffcd227" in str(x.value) for x in at.markdown), (
        "first case header missing"
    )
    # neutral view: the model verdict must not appear outside diagnostics
    body = " ".join(str(x.value) for x in at.markdown) + " ".join(
        str(c.value) for c in at.caption
    )
    assert "low_margin" not in body


def test_save_label_writes_files(at, tmp_path):
    # choose the first radio option ("Match Candidate A") and save
    radio = next(r for r in at.radio if str(r.label).startswith("Primary judgment"))
    radio.set_value(radio.options[0])
    at.run()
    save = next(
        b for b in at.button if str(b.label).startswith("💾 Save label")
    )
    save.click()
    at.run()
    assert not at.exception

    csv_path = tmp_path / "labels" / "scheduled_trip_labels.csv"
    assert csv_path.exists()
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 1
    row = rows[0]
    assert row["case_id"] == "C-0ffcd227"
    assert row["reviewer"] == "test"
    assert row["label"] in ("MATCH_TOP_CANDIDATE", "MATCH_OTHER_CANDIDATE")
    assert row["selected_candidate_id"]
    assert row["app_version"]
    assert (tmp_path / "labels" / "scheduled_trip_labels.jsonl").exists()

    # the neutral view after saving must not disclose model agreement:
    # MATCH_TOP/MATCH_OTHER live only in the persisted files
    visible_text = " ".join(
        str(x.value) for x in list(at.markdown) + list(at.caption)
    ) + " ".join(str(m.value) for m in at.metric)
    assert "MATCH_TOP" not in visible_text
    assert "MATCH_OTHER" not in visible_text


def test_notes_in_same_request_as_save_are_persisted(at, tmp_path):
    """Regression: text committed by the same request as the click (blur ==
    click in a real browser) must reach the saved record."""
    radio = next(r for r in at.radio if str(r.label).startswith("Primary judgment"))
    radio.set_value(radio.options[0])
    at.run()
    # simulate blur+click in one request: set the widget value and click
    # WITHOUT an intermediate at.run()
    notes = next(t for t in at.text_area if str(t.label) == "Notes")
    notes.set_value("typed right before saving")
    save = next(b for b in at.button if str(b.label).startswith("💾 Save label"))
    save.click()
    at.run()
    assert not at.exception
    rows = list(
        csv.DictReader((tmp_path / "labels" / "scheduled_trip_labels.csv").open())
    )
    assert rows[0]["notes"] == "typed right before saving"


def test_next_button_advances(at):
    nxt = next(b for b in at.button if str(b.label).startswith("▶ Next"))
    nxt.click()
    at.run()
    assert not at.exception
    assert at.session_state["current_case_id"] == "C-7d4b42b8"
