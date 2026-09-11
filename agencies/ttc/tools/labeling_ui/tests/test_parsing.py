from datetime import date, datetime

from labeling.parsing import (
    dates_spanned,
    duration_bucket,
    epoch_to_local,
    fmt_duration_s,
    local_naive_to_epoch,
    parse_anchor_status,
    parse_candidate_key,
    parse_chunk_duration_s,
    redact_summary,
)


def test_parse_chunk_duration():
    assert parse_chunk_duration_s("chunk of 2312s, anchor status low_margin") == 2312
    assert parse_chunk_duration_s("chunk of 384s, anchor status accepted") == 384
    assert parse_chunk_duration_s("no duration here") is None
    assert parse_chunk_duration_s(None) is None
    assert parse_chunk_duration_s("") is None


def test_parse_anchor_status():
    assert parse_anchor_status("chunk of 2312s, anchor status low_margin") == (
        "low_margin"
    )
    assert parse_anchor_status("chunk of 10s, anchor status accepted") == "accepted"
    assert parse_anchor_status("nothing") is None


def test_redact_summary_hides_verdict():
    red = redact_summary("chunk of 2312s, anchor status low_margin")
    assert "low_margin" not in red
    assert "2312" in red
    assert redact_summary(None) is None


def test_parse_candidate_key_full():
    ck = parse_candidate_key("2023-11-28|ttc-de8294439596|70632|46536174|")
    assert ck.service_date == date(2023, 11, 28)
    assert ck.feed_id == "ttc-de8294439596"
    assert ck.route_id == "70632"
    assert ck.trip_id == "46536174"
    assert ck.polarity is None


def test_parse_candidate_key_partial_and_junk():
    assert parse_candidate_key("") is None
    assert parse_candidate_key(None) is None
    ck = parse_candidate_key("not-a-date|feed")
    assert ck.service_date is None
    assert ck.feed_id == "feed"
    assert ck.trip_id is None
    ck2 = parse_candidate_key("2023-11-28|f|r|t|pol_b")
    assert ck2.polarity == "pol_b"


def test_epoch_convention_round_trip():
    # spike convention: naive Toronto wall time -> epoch
    # (1701202576 == 2023-11-28 20:16:16 UTC == 15:16:16 EST)
    e = local_naive_to_epoch(datetime(2023, 11, 28, 15, 16, 16))
    assert e == 1701202576
    back = epoch_to_local(e)
    assert (back.hour, back.minute, back.second) == (15, 16, 16)


def test_dates_spanned_crosses_midnight():
    # 2023-11-28 23:30 -> 2023-11-29 00:30 local
    s = local_naive_to_epoch(datetime(2023, 11, 28, 23, 30))
    e = local_naive_to_epoch(datetime(2023, 11, 29, 0, 30))
    assert dates_spanned(s, e) == [date(2023, 11, 28), date(2023, 11, 29)]


def test_fmt_duration():
    assert fmt_duration_s(42) == "42s"
    assert fmt_duration_s(2312) == "38m32s"
    assert fmt_duration_s(7200) == "2h00m"
    assert fmt_duration_s(-90) == "-1m30s"
    assert fmt_duration_s(None) == "n/a"


def test_duration_bucket():
    assert duration_bucket(300) == "< 10 min"
    assert duration_bucket(2000) == "30–60 min"
    assert duration_bucket(None) == "unknown"
