import json

from labeling.store import LabelRecord, LabelStore


def make_rec(**kw) -> LabelRecord:
    base = dict(
        case_id="C-test1",
        reviewer="jw",
        label="MATCH_TOP_CANDIDATE",
        selected_candidate_letter="B",
        selected_candidate_id="2023-11-28|feed|r|t|",
        confidence=4,
        rationale=["time_fit_good", "spatial_fit_good"],
        notes="looks right",
        session_id="abc",
        app_version="0.1.0",
    )
    base.update(kw)
    return LabelRecord(**base)


def test_save_and_load_round_trip(tmp_path):
    s = LabelStore(tmp_path / "labels")
    s.save(make_rec())
    got = s.get("C-test1", "jw")
    assert got is not None
    assert got.label == "MATCH_TOP_CANDIDATE"
    assert got.rationale == ["time_fit_good", "spatial_fit_good"]
    assert got.confidence == 4
    assert got.created_at and got.modified_at


def test_resave_upserts_and_keeps_created_at(tmp_path):
    s = LabelStore(tmp_path / "labels")
    first = s.save(make_rec())
    second = s.save(make_rec(label="AMBIGUOUS", notes="on reflection"))
    all_rows = s.load_all()
    assert len(all_rows) == 1
    got = all_rows[("C-test1", "jw")]
    assert got.label == "AMBIGUOUS"
    assert got.created_at == first.created_at

    # audit log has both events
    events = [
        json.loads(line)
        for line in (s.jsonl_path).read_text().splitlines()
    ]
    assert [e["event"] for e in events] == ["save", "resave"]
    assert events[1]["label"] == "AMBIGUOUS"
    assert second.modified_at == events[1]["modified_at"]


def test_two_reviewers_do_not_collide(tmp_path):
    s = LabelStore(tmp_path / "labels")
    s.save(make_rec(reviewer="jw"))
    s.save(make_rec(reviewer="xy", label="NO_SCHEDULED_MATCH"))
    assert len(s.load_all()) == 2
    assert s.get("C-test1", "xy").label == "NO_SCHEDULED_MATCH"
    assert s.get("C-test1", "jw").label == "MATCH_TOP_CANDIDATE"


def test_drafts(tmp_path):
    s = LabelStore(tmp_path / "labels")
    assert s.get_draft("C-test1", "jw") is None
    s.save_draft("C-test1", "jw", {"choice": "Ambiguous", "confidence": 2})
    d = s.get_draft("C-test1", "jw")
    assert d == {"choice": "Ambiguous", "confidence": 2}
    # different reviewer sees no draft
    assert s.get_draft("C-test1", "xy") is None


def test_corrupt_drafts_degrade(tmp_path):
    s = LabelStore(tmp_path / "labels")
    s.drafts_path.write_text("{not json")
    assert s.load_drafts() == {}
    # valid JSON of the wrong shape must degrade too
    for junk in ["[]", "null", '"hi"', '{"C-1::jw": []}', '{"C-1::jw": {"x": 1}}']:
        s.drafts_path.write_text(junk)
        assert s.get_draft("C-1", "jw") is None
        s.save_draft("C-2", "jw", {"choice": "Ambiguous"})  # must not raise
        assert s.get_draft("C-2", "jw") == {"choice": "Ambiguous"}


def test_save_deletes_draft(tmp_path):
    s = LabelStore(tmp_path / "labels")
    s.save_draft("C-test1", "jw", {"choice": "No scheduled match"})
    s.save(make_rec())
    assert s.get_draft("C-test1", "jw") is None, (
        "a stale draft must not shadow the saved record on revisit"
    )


def test_tolerant_confidence_parsing(tmp_path):
    s = LabelStore(tmp_path / "labels")
    s.save(make_rec())
    text = s.csv_path.read_text().replace(",4,", ",3.0,")
    s.csv_path.write_text(text)
    got = s.get("C-test1", "jw")
    assert got is not None and got.confidence == 3
    s.csv_path.write_text(text.replace(",3.0,", ",junk,"))
    got = s.get("C-test1", "jw")
    assert got is not None and got.confidence is None


def test_reveal_events(tmp_path):
    s = LabelStore(tmp_path / "labels")
    assert s.reveals() == set()
    s.record_reveal("C-test1", "jw")
    assert ("C-test1", "jw") in s.reveals()
    # reveal events coexist with save events in the same JSONL
    s.save(make_rec())
    assert ("C-test1", "jw") in s.reveals()
    assert s.get("C-test1", "jw") is not None
