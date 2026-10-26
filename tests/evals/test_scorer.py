"""Metric aggregation: recall, precision, clean-diff behavior, per-category, variance."""

from evals.harness.cases import FindingLite, Label
from evals.harness.matcher import MatchSettings, match_case
from evals.harness.scorer import RunResult, score


def _run(
    repeat: int,
    case_id: str,
    is_clean: bool,
    findings: list[FindingLite],
    labels: list[Label],
) -> RunResult:
    return RunResult(
        repeat=repeat,
        case_id=case_id,
        is_clean=is_clean,
        match=match_case(findings, labels, MatchSettings()),
    )


def test_recall_precision_and_clean_metrics() -> None:
    results = [
        _run(
            0,
            "hit",
            False,
            [FindingLite(path="a.py", line=10, category="security")],
            [Label(path="a.py", line=10, category="security")],
        ),
        _run(0, "miss", False, [], [Label(path="b.py", line=5, category="correctness")]),
        _run(0, "clean", True, [FindingLite(path="z.py", line=1)], []),
    ]
    report = score(
        results, provider="anthropic", mode="diff-only", repeats=1, settings=MatchSettings()
    )
    assert report.total_labels == 2
    assert report.matched_labels == 1
    assert report.recall == 0.5
    # anchored findings: 1 matched (hit) + 1 false positive (clean) = 2 -> precision 1/2.
    assert report.precision == 0.5
    assert report.clean_runs == 1
    assert report.clean_false_alarm_rate == 1.0
    assert report.clean_pass_rate == 0.0
    assert report.recall_by_category == {"correctness": 0.0, "security": 1.0}


def test_variance_across_repeats() -> None:
    results = [
        _run(0, "c", False, [FindingLite(path="a.py", line=10)], [Label(path="a.py", line=10)]),
        _run(1, "c", False, [], [Label(path="a.py", line=10)]),
    ]
    report = score(results, provider="x", mode="y", repeats=2, settings=MatchSettings())
    assert report.recall_by_repeat == [1.0, 0.0]
    assert report.recall_mean == 0.5
    assert report.recall_stdev == 0.5


def test_unanchored_findings_are_counted_not_dropped() -> None:
    results = [_run(0, "clean", True, [FindingLite(path="z.py", line=None)], [])]
    report = score(results, provider="x", mode="y", repeats=1, settings=MatchSettings())
    assert report.unanchored_findings == 1
    assert report.clean_unanchored_findings == 1
    # Still not a false positive / false alarm by definition.
    assert report.clean_false_alarm_rate == 0.0
