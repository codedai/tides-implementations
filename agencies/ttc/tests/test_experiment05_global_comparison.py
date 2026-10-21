"""Streaming comparison checks only; no experimental scoring or data changes."""

import pytest

from tests.experiment05_compare_global import column_hashes
from tools.experiment05.io import write_rows


def test_column_hashes_ignore_other_modes_and_batch_boundaries(tmp_path):
    rows = [
        {"match_mode": "global", "s": "naïve", "t": 0.0, "n": None},
        {"match_mode": "early", "s": "ignored", "t": 99.0, "n": None},
        {"match_mode": "global", "s": "a\x00b", "t": -0.0, "n": None},
    ]
    all_modes, global_only = tmp_path / "all.parquet", tmp_path / "global.parquet"
    write_rows(all_modes, rows)
    write_rows(global_only, [r for r in rows if r["match_mode"] == "global"])
    expected = column_hashes(global_only, batch_size=1)
    assert expected["rows"] == 2
    for batch_size in (1, 2, 4096):
        assert column_hashes(all_modes, batch_size=batch_size) == expected


@pytest.mark.parametrize(
    "left,right", [(None, "None"), (True, 1), (1, 1.0), (0.0, -0.0)]
)
def test_scalar_types_and_signed_zero_are_not_conflated(tmp_path, left, right):
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    write_rows(a, [{"match_mode": "global", "value": left}])
    write_rows(b, [{"match_mode": "global", "value": right}])
    assert column_hashes(a) != column_hashes(b)


def test_length_delimiting_and_row_order_are_preserved(tmp_path):
    paths = [tmp_path / f"{i}.parquet" for i in range(3)]
    for path, values in zip(
        paths, (["ab", "c"], ["a", "bc"], ["c", "ab"]), strict=True
    ):
        write_rows(path, [{"match_mode": "global", "value": v} for v in values])
    assert (
        len({column_hashes(path)["sha256_by_column"]["value"] for path in paths}) == 3
    )
