"""Delay-prior experiments retain historical defaults and explicit units."""

import pytest

from tools.merge_review.gtfs_matching.config import defaults, parameters, validate


def test_delay_prior_parameter_explains_units_and_recomputation():
    spec = next(p for p in parameters() if p["key"] == "score_delay_prior_weight")
    assert "每小时" in spec["label"]
    assert "3600" in spec["description"]
    assert "提前与晚点对称" in spec["description"]
    assert "修改后需新运行" in spec["description"]
    assert spec["type"] == "number"


def test_stronger_delay_prior_is_explicit_and_does_not_rewrite_baseline():
    assert defaults()["score_delay_prior_weight"] == 0.05
    assert (
        validate({"score_delay_prior_weight": 1.0})["score_delay_prior_weight"] == 1.0
    )
    assert (
        validate({"score_delay_prior_weight": 0.05})["score_delay_prior_weight"] == 0.05
    )


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1.0"])
def test_invalid_delay_prior_is_rejected(value):
    with pytest.raises(ValueError):
        validate({"score_delay_prior_weight": value})
