"""Calibration script: scoring and threshold choice on synthetic rows."""
import pytest

from scripts.jev_eval import calibrate_topic_match as cal

pytestmark = pytest.mark.unit

T = "work/hiring"


def _row(primary, **dist):
    return {"topic": primary, "secondary": set(), "dist": {k.replace("__", "/"): v for k, v in dist.items()}}


def test_matches_follows_the_store_rule():
    row = _row("a/x", a__x=0.6, b__y=0.2, work__hiring=0.05)
    assert cal.matches(row, T, k=3, min_p=0.10)  # rank 3
    assert not cal.matches(row, T, k=2, min_p=0.10)  # rank 3, below min-p
    assert cal.matches(row, T, k=2, min_p=0.05)  # at min-p
    assert not cal.matches(_row("a/x", a__x=1.0, work__hiring=0.0), T, k=5, min_p=0.0)
    assert cal.matches(_row(T), T, k=1, min_p=1.0)  # primary always matches


def test_score_and_choose_reach_the_recall_target():
    rows = [_row("a/x", a__x=0.97, work__hiring=0.03), _row("a/x", a__x=0.9, work__hiring=0.0),
            _row(T, work__hiring=1.0)]
    silver = {tag: set() for tag in cal.SILVER_TAG_TOPIC}
    silver["hiring"] = {0, 1}
    narrow = cal.score(rows, silver, k=1, min_p=0.05)
    assert (narrow["recall"], narrow["matched"]) == (0.0, 1)
    wide = cal.score(rows, silver, k=2, min_p=0.05)
    assert (wide["recall"], wide["matched"], wide["precision"]) == (0.5, 2, 1.0)
    assert cal.choose([narrow, wide], target=0.5) is wide
    assert cal.choose([narrow, wide], target=0.9) is None
