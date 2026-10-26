"""Aggregate per-run matches into interpretable metrics.

Reports recall, precision, clean-diff false-alarm behavior, per-category recall, and run-to-run
variance — deliberately as a tuple of numbers rather than a single distilled score, so the
precision/recall trade-off stays visible. All numbers are meaningless without the matcher settings
and the honest-limitations note, which the report carries alongside them.

Runs whose review *failed* (credentials, rate limits, ambiguous calls) are excluded from every
metric and listed separately; a report with any failed run is marked incomplete. Runs where the
service would have *withheld* findings are scored as what users see (nothing published) but are
also counted by reason, so a silent "found nothing" is visible rather than read as a clean pass.
"""

import statistics
from dataclasses import dataclass, field

from evals.harness.matcher import CaseMatch, MatchSettings

_DISCLAIMER = (
    "Directional only: synthetic fixtures are not the real-world defect distribution. Use these "
    "numbers for provider/mode A-B comparison and regression tracking, not as an absolute accuracy "
    "claim. Trustworthy absolute numbers require a real labeled PR corpus."
)


@dataclass(frozen=True)
class RunResult:
    repeat: int
    case_id: str
    is_clean: bool
    match: CaseMatch
    withheld_reason: str | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class RunFailure:
    repeat: int
    case_id: str
    error: str


@dataclass
class CaseTally:
    """Per-case totals across repeats, so a weak metric can be traced to the cases behind it."""

    runs: int = 0
    labels: int = 0
    matched_labels: int = 0
    false_positive_findings: int = 0
    withheld_runs: int = 0
    failed_runs: int = 0

    @property
    def perfect(self) -> bool:
        return (
            self.matched_labels == self.labels
            and not self.false_positive_findings
            and not self.withheld_runs
            and not self.failed_runs
        )


def _floats() -> list[float]:
    return []


def _catmap() -> dict[str, float]:
    return {}


def _countmap() -> dict[str, int]:
    return {}


def _failures() -> list[RunFailure]:
    return []


def _tallies() -> dict[str, CaseTally]:
    return {}


@dataclass
class EvalReport:
    provider: str
    mode: str
    repeats: int
    settings: MatchSettings
    total_runs: int = 0
    total_labels: int = 0
    matched_labels: int = 0
    anchored_findings: int = 0
    false_positive_findings: int = 0
    unanchored_findings: int = 0
    clean_runs: int = 0
    clean_runs_with_findings: int = 0
    clean_findings: int = 0
    clean_unanchored_findings: int = 0
    withheld_runs: int = 0
    withheld_by_reason: dict[str, int] = field(default_factory=_countmap)
    failures: list[RunFailure] = field(default_factory=_failures)
    recall_by_repeat: list[float] = field(default_factory=_floats)
    recall_by_category: dict[str, float] = field(default_factory=_catmap)
    by_case: dict[str, CaseTally] = field(default_factory=_tallies)
    disclaimer: str = _DISCLAIMER

    @property
    def failed_runs(self) -> int:
        return len(self.failures)

    @property
    def complete(self) -> bool:
        """False when any review failed; such a report must not be used for comparison."""
        return not self.failures and self.total_runs > 0

    @property
    def recall(self) -> float:
        return self.matched_labels / self.total_labels if self.total_labels else 0.0

    @property
    def precision(self) -> float:
        return self.matched_labels / self.anchored_findings if self.anchored_findings else 0.0

    @property
    def clean_false_alarm_rate(self) -> float:
        """Average anchored findings emitted per clean-labeled run (0.0 is ideal)."""
        return self.clean_findings / self.clean_runs if self.clean_runs else 0.0

    @property
    def clean_pass_rate(self) -> float:
        """Fraction of clean runs that emitted no anchored finding (1.0 is ideal)."""
        if not self.clean_runs:
            return 1.0
        return (self.clean_runs - self.clean_runs_with_findings) / self.clean_runs

    @property
    def recall_mean(self) -> float:
        return statistics.fmean(self.recall_by_repeat) if self.recall_by_repeat else 0.0

    @property
    def recall_stdev(self) -> float:
        return statistics.pstdev(self.recall_by_repeat) if len(self.recall_by_repeat) > 1 else 0.0


def score(
    results: list[RunResult], *, provider: str, mode: str, repeats: int, settings: MatchSettings
) -> EvalReport:
    report = EvalReport(provider=provider, mode=mode, repeats=repeats, settings=settings)
    category_total: dict[str, int] = {}
    category_matched: dict[str, int] = {}
    for run in results:
        report.total_runs += 1
        tally = report.by_case.setdefault(run.case_id, CaseTally())
        tally.runs += 1
        if run.error is not None:
            tally.failed_runs += 1
            report.failures.append(RunFailure(run.repeat, run.case_id, run.error))
            continue
        tally.labels += len(run.match.matched_labels) + len(run.match.missed_labels)
        tally.matched_labels += len(run.match.matched_labels)
        tally.false_positive_findings += len(run.match.false_positive_findings)
        if run.withheld_reason is not None:
            tally.withheld_runs += 1
            report.withheld_runs += 1
            report.withheld_by_reason[run.withheld_reason] = (
                report.withheld_by_reason.get(run.withheld_reason, 0) + 1
            )
        matched = run.match
        report.anchored_findings += len(matched.matched_findings) + len(
            matched.false_positive_findings
        )
        report.false_positive_findings += len(matched.false_positive_findings)
        report.unanchored_findings += len(matched.unanchored_findings)
        if run.is_clean:
            report.clean_runs += 1
            found = len(matched.false_positive_findings)
            report.clean_findings += found
            report.clean_unanchored_findings += len(matched.unanchored_findings)
            if found:
                report.clean_runs_with_findings += 1
            continue
        report.total_labels += len(matched.matched_labels) + len(matched.missed_labels)
        report.matched_labels += len(matched.matched_labels)
        for label in matched.matched_labels:
            key = label.category or "uncategorized"
            category_total[key] = category_total.get(key, 0) + 1
            category_matched[key] = category_matched.get(key, 0) + 1
        for label in matched.missed_labels:
            key = label.category or "uncategorized"
            category_total[key] = category_total.get(key, 0) + 1
    report.recall_by_category = {
        key: category_matched.get(key, 0) / total for key, total in sorted(category_total.items())
    }
    report.withheld_by_reason = dict(sorted(report.withheld_by_reason.items()))
    report.recall_by_repeat = _recall_per_repeat(results)
    return report


def _recall_per_repeat(results: list[RunResult]) -> list[float]:
    by_repeat: dict[int, tuple[int, int]] = {}
    for run in results:
        if run.is_clean or run.failed:
            continue
        matched, total = by_repeat.get(run.repeat, (0, 0))
        matched += len(run.match.matched_labels)
        total += len(run.match.matched_labels) + len(run.match.missed_labels)
        by_repeat[run.repeat] = (matched, total)
    return [m / t if t else 0.0 for _, (m, t) in sorted(by_repeat.items())]
