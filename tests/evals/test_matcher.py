"""Deterministic finding<->label matcher behavior."""

import pytest

from evals.harness.cases import FindingLite, Label
from evals.harness.matcher import MatchSettings, match_case


def test_match_within_window() -> None:
    m = match_case(
        [FindingLite(path="a.py", line=12, category="security")],
        [Label(path="a.py", line=10, category="security")],
        MatchSettings(line_window=3),
    )
    assert len(m.matched_labels) == 1
    assert not m.missed_labels and not m.false_positive_findings


def test_outside_window_is_miss_and_false_positive() -> None:
    m = match_case(
        [FindingLite(path="a.py", line=20)],
        [Label(path="a.py", line=10)],
        MatchSettings(line_window=3),
    )
    assert m.missed_labels and m.false_positive_findings


def test_category_requirement_is_optional() -> None:
    findings = [FindingLite(path="a.py", line=10, category="style")]
    labels = [Label(path="a.py", line=10, category="security")]
    assert match_case(findings, labels, MatchSettings(require_category=True)).missed_labels
    assert not match_case(findings, labels, MatchSettings(require_category=False)).missed_labels


def test_unanchored_findings_are_separated() -> None:
    m = match_case(
        [FindingLite(path="a.py", line=None)],
        [Label(path="a.py", line=10)],
        MatchSettings(),
    )
    assert m.unanchored_findings
    assert m.missed_labels and not m.false_positive_findings


def test_matching_is_greedy_one_to_one() -> None:
    m = match_case(
        [FindingLite(path="a.py", line=10), FindingLite(path="a.py", line=10)],
        [Label(path="a.py", line=10)],
        MatchSettings(),
    )
    assert len(m.matched_labels) == 1
    assert len(m.false_positive_findings) == 1


def test_wrong_file_never_matches() -> None:
    m = match_case(
        [FindingLite(path="b.py", line=10)],
        [Label(path="a.py", line=10)],
        MatchSettings(),
    )
    assert m.missed_labels and m.false_positive_findings


def test_matching_is_maximum_not_first_fit() -> None:
    # First-fit would give A the finding at 12, leaving B unmatched; maximum matching credits both.
    labels = [Label(path="a.py", line=10), Label(path="a.py", line=13)]
    findings = [FindingLite(path="a.py", line=12), FindingLite(path="a.py", line=16)]
    m = match_case(findings, labels, MatchSettings(line_window=3))
    assert len(m.matched_labels) == 2
    assert not m.false_positive_findings
    reversed_match = match_case(findings, list(reversed(labels)), MatchSettings(line_window=3))
    assert len(reversed_match.matched_labels) == 2


def test_category_comparison_is_normalized() -> None:
    findings = [FindingLite(path="a.py", line=10, category=" Security ")]
    labels = [Label(path="a.py", line=10, category="security")]
    assert not match_case(findings, labels, MatchSettings(require_category=True)).missed_labels


def test_negative_line_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="line_window"):
        MatchSettings(line_window=-1)
