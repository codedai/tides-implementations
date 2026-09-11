"""Headless full-app tests: the blind protocol as the reviewer experiences it.

Uses streamlit's AppTest against a hermetic copy of run01. The assertions
are the protocol: no first-pass value, packet summary, model diagnostic, or
audit routing vocabulary is renderable before the blind save; the grain
question gates the task form; the null option is always present; filters
report hidden counts and are logged; interaction logging is honest; saving
writes the append-only history. First-pass content used in leak assertions
is loaded from the restricted parquet at runtime — never hard-coded here.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import APP_DIR, ARTIFACTS, AUDIT_DIR, requires_artifacts
from uiv2.views.forms import SCHEDULED_JUDGMENT_LABELS

pytestmark = requires_artifacts

# 粒度单选的选项值是中文显示标签（GRAIN_OPTIONS 的 label 侧）；
# 入库的仍是英文 grain 值，见 uiv2.views.forms.GRAIN_OPTIONS。
GRAIN_SINGLE_TRIP = "一个可评的、trip 尺度的 chunk"

STATIC_LEAK_STRINGS = (
    "MATCH_TOP_CANDIDATE",
    "MATCH_OTHER_CANDIDATE",
    "low_margin",
    "anchor status",
    "qscore",
)

ROUTING_VOCABULARY = (
    "计划班次盲审二审",
    "多 trip 分段评审",
    "跨边界双角色评审",
    "片段角色与缝合评审",
    "上下文不足片段评审",
    "源数据 / 时间质量评审",
    "无匹配活动评审",
)


@pytest.fixture(scope="module")
def first_pass_leaks() -> list[str]:
    frame = pl.read_parquet(AUDIT_DIR / "first_pass_labels_v1.parquet")
    leaks = list(STATIC_LEAK_STRINGS)
    for note in frame["raw_notes"].drop_nulls().to_list():
        note = note.strip()
        if len(note) >= 20:
            leaks.append(note[:40])
    return leaks


@pytest.fixture()
def at(fresh_run01: Path, monkeypatch) -> AppTest:
    monkeypatch.setenv("LABELING_V2_RUN_DIR", str(fresh_run01))
    monkeypatch.setenv("LABELING_V2_ARTIFACTS", str(ARTIFACTS))
    monkeypatch.setenv("LABELING_V2_AUDIT_DIR", str(AUDIT_DIR))
    monkeypatch.setenv("LABELING_V2_REVIEWER", "apptest")
    test = AppTest.from_file(str(APP_DIR / "app.py"), default_timeout=300)
    test.run()
    return test


def _page_text(at: AppTest) -> str:
    parts: list[str] = []
    kinds = (
        "markdown",
        "caption",
        "text",
        "warning",
        "info",
        "success",
        "error",
        "subheader",
        "title",
    )
    for kind in kinds:
        for element in getattr(at, kind, []):
            parts.append(str(getattr(element, "value", "")))
    for metric in at.metric:
        parts.append(f"{metric.label}={metric.value}")
    for radio in at.radio:
        parts.append(str(radio.label))
        parts.append(str(list(getattr(radio, "options", []))))
    for box in at.selectbox:
        parts.append(str(box.label))
    for expander in getattr(at, "expander", []):
        parts.append(str(getattr(expander, "label", "")))
    return "\n".join(parts)


def _select_case(at: AppTest, case_id: str) -> AppTest:
    at.radio(key="case_select").set_value(case_id).run()
    return at


def _grain_radio(at: AppTest, case_id: str):
    return at.radio(key=f"grain::{case_id}")


def _complete_alias(run_dir: Path, case_id: str) -> str:
    alias_map = pl.read_parquet(run_dir / "candidate_alias_map.parquet")
    rows = alias_map.filter(
        (pl.col("case_id") == case_id)
        & (pl.col("candidate_kind") == "complete_scheduled_trip")
    )
    return rows["candidate_alias"][0]


def test_first_case_renders_blind(at: AppTest, first_pass_leaks):
    text = _page_text(at)
    assert "案例 C-" in text
    for leak in first_pass_leaks:
        assert leak not in text, leak
    assert "存盘前保持隐藏" in text


def test_pre_grain_page_shows_no_routing_vocabulary(at: AppTest):
    at = _select_case(at, "C-134f2a21")
    text = _page_text(at)
    assert "任务名将在你回答粒度问题后揭示" in text
    for phrase in ROUTING_VOCABULARY:
        assert phrase not in text, phrase
    grain = _grain_radio(at, "C-134f2a21")
    assert grain.value is None, "the grain question must have no preselection"
    grain.set_value(GRAIN_SINGLE_TRIP).run()
    text = _page_text(at)
    assert "计划班次盲审二审" in text


def test_grain_question_gates_task_form(at: AppTest, first_pass_leaks):
    at = _select_case(at, "C-134f2a21")
    judgment_radios = [radio for radio in at.radio if str(radio.label) == "判断"]
    assert not judgment_radios, "task form must not render before the grain answer"
    _grain_radio(at, "C-134f2a21").set_value(GRAIN_SINGLE_TRIP).run()
    judgment_radios = [radio for radio in at.radio if str(radio.label) == "判断"]
    assert judgment_radios, "scheduled form should appear after the grain answer"
    # AppTest reports the *displayed* options, so this asserts the localized
    # null option is on screen ...
    options = list(judgment_radios[0].options)
    assert SCHEDULED_JUDGMENT_LABELS["no_scheduled_match"] in options, (
        "null option must always be visible"
    )
    # ... and this asserts the stored value is unchanged by the translation:
    # selecting by the English enum still works and is what the record keeps.
    at.radio(key="sched::judgment::C-134f2a21").set_value("no_scheduled_match").run()
    assert at.radio(key="sched::judgment::C-134f2a21").value == "no_scheduled_match"
    text = _page_text(at)
    for leak in first_pass_leaks:
        assert leak not in text, leak


def test_candidate_universe_shows_all_without_model_truncation(at: AppTest):
    routing = pl.read_parquet(
        Path(at.session_state["v2_config"].run_dir) / "case_routing_manifest.parquet"
    )
    biggest = routing.sort("candidate_count", descending=True).row(0, named=True)
    at = _select_case(at, biggest["case_id"])
    text = _page_text(at)
    assert f"共 {biggest['candidate_count']} 个候选" in text
    assert biggest["candidate_count"] >= 50
    assert "你自己设置的筛选条件" not in text


def test_filters_report_hidden_count_and_are_logged(at: AppTest, fresh_run01):
    # Within one case the complete candidates share route and date — the
    # discriminating reviewer filter is the scheduled-start range slider.
    case_id = "C-134f2a21"
    universe = pl.read_parquet(fresh_run01 / "candidate_universe_manifest.parquet")
    starts = (
        universe.filter(
            (pl.col("case_id") == case_id) & pl.col("sched_start_epoch_s").is_not_null()
        )["sched_start_epoch_s"]
        .sort()
        .to_list()
    )
    assert len(set(starts)) >= 3, "test needs spread scheduled starts"
    at = _select_case(at, case_id)
    narrowed = (int(starts[1]), int(starts[-1]))
    at.slider(key=f"fs::{case_id}").set_value(narrowed).run()
    text = _page_text(at)
    assert "你自己设置的筛选条件" in text
    history = (fresh_run01 / "review_history.jsonl").read_text()
    assert "candidate_filter_applied" in history


def test_blind_save_writes_history_then_allows_reveal(at: AppTest, fresh_run01):
    case_id = "C-134f2a21"
    at = _select_case(at, case_id)
    _grain_radio(at, case_id).set_value(GRAIN_SINGLE_TRIP).run()
    at.radio(key=f"sched::judgment::{case_id}").set_value("matched").run()
    alias = _complete_alias(fresh_run01, case_id)
    at.selectbox(key=f"sched::alias::{case_id}").set_value(alias).run()
    text = _page_text(at)
    assert "揭示一审判断" not in text
    assert "揭示模型诊断" not in text
    at.button(key=f"save::{case_id}").click().run()
    history = (fresh_run01 / "review_history.jsonl").read_text()
    assert history.count('"event": "second_pass_save"') >= 1
    labels = pl.read_parquet(fresh_run01 / "second_pass_labels_v2.parquet")
    assert labels.height == 1
    row = labels.row(0, named=True)
    assert row["case_id"] == case_id
    assert row["first_pass_hidden"] is True
    assert row["selected_candidate_key"]
    text = _page_text(at)
    assert "盲审判断已存盘" in text
    assert "揭示一审判断" in text
    assert "揭示模型诊断" in text


def test_interaction_logging_is_honest(at: AppTest, fresh_run01):
    case_id = "C-179eba6d"
    at = _select_case(at, case_id)
    _grain_radio(at, case_id).set_value(GRAIN_SINGLE_TRIP).run()
    at.radio(key=f"sched::judgment::{case_id}").set_value("insufficient_evidence").run()
    at.button(key=f"save::{case_id}").click().run()
    labels = pl.read_parquet(fresh_run01 / "second_pass_labels_v2.parquet")
    row = labels.row(0, named=True)
    import json as json_module

    panels = json_module.loads(row["panels_viewed"])
    assert panels == ["map"], (
        "only the default panel was opened; the record must not claim more"
    )
    assert row["adjacent_context_used"] is False


def test_reveal_after_save_is_logged_and_shows_first_pass(at: AppTest, fresh_run01):
    case_id = "C-179eba6d"
    at = _select_case(at, case_id)
    _grain_radio(at, case_id).set_value(GRAIN_SINGLE_TRIP).run()
    at.radio(key=f"sched::judgment::{case_id}").set_value("insufficient_evidence").run()
    at.button(key=f"save::{case_id}").click().run()
    at.button(key=f"fpreveal::{case_id}").click().run()
    text = _page_text(at)
    assert "raw_primary_label" in text
    history = (fresh_run01 / "review_history.jsonl").read_text()
    assert '"event": "first_pass_reveal"' in history
    at.button(key=f"diagreveal::{case_id}").click().run()
    history = (fresh_run01 / "review_history.jsonl").read_text()
    assert '"event": "diagnostics_reveal"' in history


def test_disagreement_shows_exact_outcome_and_updates_agreement(
    at: AppTest, fresh_run01
):
    case_id = "C-b7163d0c"
    at = _select_case(at, case_id)
    _grain_radio(at, case_id).set_value(GRAIN_SINGLE_TRIP).run()
    at.radio(key=f"sched::judgment::{case_id}").set_value("ambiguous_candidates").run()
    at.button(key=f"save::{case_id}").click().run()
    text = _page_text(at)
    assert "FIRST_SECOND_DISAGREE" in text
    import json as json_module

    agreement = json_module.loads(
        (fresh_run01 / "intra_reviewer_agreement.json").read_text()
    )
    assert agreement["compared_case_count"] == 1
    assert agreement["disagree_count"] == 1
