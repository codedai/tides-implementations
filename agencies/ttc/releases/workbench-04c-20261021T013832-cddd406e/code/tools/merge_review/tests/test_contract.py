"""Independent static UI contracts; browser_smoke.mjs covers actual behavior."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"


class Document(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.tags = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture
def document():
    return Document((STATIC / "index.html").read_text())


def test_ids_are_unique(document):
    ids = [attrs["id"] for _, attrs in document.tags if "id" in attrs]
    assert len(ids) == len(set(ids))


def test_no_default_remote_resources_or_inline_event_handlers(document):
    for tag, attrs in document.tags:
        assert not any(key.startswith("on") for key in attrs)
        if tag in {"script", "img", "iframe", "source", "video", "audio"}:
            source = attrs.get("src", "")
            assert not source.startswith(("http:", "https:", "//"))
        if tag == "link":
            assert not attrs.get("href", "").startswith(("http:", "https:", "//"))


def test_three_views_have_accessible_names(document):
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in document.tags if "id" in attrs}
    for name in ("map-svg", "timeline-svg", "progress-svg"):
        tag, attrs = by_id[name]
        assert tag == "svg"
        assert attrs.get("role") == "img"
        assert attrs.get("aria-label")


def test_candidate_and_clock_limitations_are_explicit():
    text = (STATIC / "index.html").read_text()
    assert "not a confirmed performed trip" in text
    assert "Inter-source clock offsets are not corrected" in text
    assert "not scheduled-trip completion percentage" in text
    assert "Distance resets at breaks" in text
    assert "association results and source data are unchanged" in text


def test_notes_length_matches_server_store_contract(document):
    notes = next(
        attrs for _, attrs in document.tags if attrs.get("id") == "review-notes"
    )
    assert int(notes["maxlength"]) == 10000


def test_review_verdicts_are_deliberate_not_default_accepted(document):
    verdicts = [attrs for _, attrs in document.tags if attrs.get("name") == "verdict"]
    assert {item["value"] for item in verdicts} == {
        "accepted",
        "problem",
        "uncertain",
        "unreviewed",
    }
    assert [item["value"] for item in verdicts if "checked" in item] == ["unreviewed"]
