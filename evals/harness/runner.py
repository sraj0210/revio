"""Replay each case through a reviewer, score the findings, and render a report.

The reviewer is injected as an async callable so this orchestration is fully unit-testable offline
with a fake; the live provider-backed reviewer is built separately (``review.py``) and is never
exercised in CI.
"""

import json
from collections.abc import Awaitable, Callable, Sequence

from evals.harness.cases import EvalCase, ReviewOutcome
from evals.harness.matcher import CaseMatch, MatchSettings, match_case
from evals.harness.scorer import EvalReport, RunResult, score

ReviewCase = Callable[[EvalCase], Awaitable[ReviewOutcome]]


async def run_eval(
    cases: Sequence[EvalCase],
    review_case: ReviewCase,
    *,
    provider: str,
    mode: str,
    repeats: int,
    settings: MatchSettings,
) -> EvalReport:
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    results: list[RunResult] = []
    for repeat in range(repeats):
        for case in cases:
            try:
                outcome = await review_case(case)
            except Exception as error:  # recorded as a failed run, never scored as "found nothing"
                print(f"  ! case {case.id}: review failed ({type(error).__name__}: {error})")
                results.append(
                    RunResult(
                        repeat=repeat,
                        case_id=case.id,
                        is_clean=case.is_clean,
                        match=CaseMatch(),
                        error=f"{type(error).__name__}: {error}",
                    )
                )
                continue
            matched = match_case(list(outcome.findings), list(case.labels), settings)
            results.append(
                RunResult(
                    repeat=repeat,
                    case_id=case.id,
                    is_clean=case.is_clean,
                    match=matched,
                    withheld_reason=outcome.withheld_reason,
                )
            )
    return score(results, provider=provider, mode=mode, repeats=repeats, settings=settings)


def report_to_dict(report: EvalReport) -> dict[str, object]:
    return {
        "provider": report.provider,
        "mode": report.mode,
        "repeats": report.repeats,
        "complete": report.complete,
        "matcher": {
            "line_window": report.settings.line_window,
            "require_category": report.settings.require_category,
        },
        "recall": round(report.recall, 4),
        "precision": round(report.precision, 4),
        "recall_mean": round(report.recall_mean, 4),
        "recall_stdev": round(report.recall_stdev, 4),
        "clean_false_alarm_rate": round(report.clean_false_alarm_rate, 4),
        "clean_pass_rate": round(report.clean_pass_rate, 4),
        "recall_by_category": {k: round(v, 4) for k, v in report.recall_by_category.items()},
        "totals": {
            "labels": report.total_labels,
            "matched_labels": report.matched_labels,
            "anchored_findings": report.anchored_findings,
            "false_positive_findings": report.false_positive_findings,
            "clean_runs": report.clean_runs,
            "clean_runs_with_findings": report.clean_runs_with_findings,
            "unanchored_findings": report.unanchored_findings,
            "clean_unanchored_findings": report.clean_unanchored_findings,
            "runs": report.total_runs,
            "withheld_runs": report.withheld_runs,
            "failed_runs": report.failed_runs,
        },
        "withheld_by_reason": report.withheld_by_reason,
        "failures": [
            {"repeat": f.repeat, "case_id": f.case_id, "error": f.error} for f in report.failures
        ],
        "disclaimer": report.disclaimer,
    }


def render_table(report: EvalReport) -> str:
    lines = [
        f"Venaroq review-quality eval — provider={report.provider} mode={report.mode} "
        f"repeats={report.repeats} "
        f"(match: ±{report.settings.line_window} lines, "
        f"category={'on' if report.settings.require_category else 'off'})",
        "-" * 72,
    ]
    if not report.complete:
        lines.append(
            f"  !! INCOMPLETE: {report.failed_runs}/{report.total_runs} runs failed and are "
            "excluded from every metric; do not use this report for comparison."
        )
    lines += [
        f"  recall                 {report.recall:6.1%}  "
        f"(mean {report.recall_mean:.1%} ± {report.recall_stdev:.1%} across repeats)",
        f"  precision              {report.precision:6.1%}  "
        f"({report.matched_labels}/{report.anchored_findings} anchored findings matched a defect)",
        f"  clean false-alarm      {report.clean_false_alarm_rate:6.2f}  findings per clean diff "
        f"(pass rate {report.clean_pass_rate:.1%})",
        f"  unanchored findings    {report.unanchored_findings:6d}  "
        f"(summary-level, not scored; {report.clean_unanchored_findings} on clean diffs)",
    ]
    if report.withheld_runs:
        reasons = ", ".join(f"{k}={v}" for k, v in report.withheld_by_reason.items())
        lines.append(
            f"  withheld runs          {report.withheld_runs:6d}  "
            f"(service would publish no findings: {reasons})"
        )
    if report.recall_by_category:
        lines.append("  recall by category:")
        lines.extend(
            f"      {category:<18} {value:6.1%}"
            for category, value in report.recall_by_category.items()
        )
    lines.append("-" * 72)
    lines.append(f"  NOTE: {report.disclaimer}")
    return "\n".join(lines)


def write_report_json(report: EvalReport, path: object) -> None:
    from pathlib import Path

    Path(str(path)).write_text(json.dumps(report_to_dict(report), indent=2), encoding="utf-8")
