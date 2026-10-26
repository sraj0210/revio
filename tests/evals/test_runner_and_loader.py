"""Runner end-to-end (fake reviewer, offline) and loader validation of the seed cases."""

from pathlib import Path

import pytest

from evals.harness.cases import ChangeRequestSpec, EvalCase, FindingLite, Label, ReviewOutcome
from evals.harness.loader import load_cases
from evals.harness.matcher import MatchSettings
from evals.harness.runner import render_table, report_to_dict, run_eval

CASES_DIR = Path(__file__).resolve().parents[2] / "evals" / "cases"


@pytest.mark.asyncio
async def test_run_eval_end_to_end_with_a_fake_reviewer() -> None:
    bug = EvalCase(
        id="bug",
        change_request=ChangeRequestSpec(),
        labels=(Label(path="a.py", line=10, category="security"),),
    )
    clean = EvalCase(id="clean")

    async def review_case(case: EvalCase) -> ReviewOutcome:
        if case.id == "bug":
            return ReviewOutcome(findings=(FindingLite(path="a.py", line=11, category="security"),))
        return ReviewOutcome()

    report = await run_eval(
        [bug, clean],
        review_case,
        provider="anthropic",
        mode="diff-only",
        repeats=2,
        settings=MatchSettings(),
    )
    assert report.recall == 1.0
    assert report.clean_pass_rate == 1.0
    assert report.complete
    payload = report_to_dict(report)
    assert payload["recall"] == 1.0
    assert payload["complete"] is True
    assert "disclaimer" in payload
    assert "recall" in render_table(report)


def test_seed_cases_load_and_cover_the_axes() -> None:
    cases = load_cases(CASES_DIR)
    assert len(cases) >= 10
    assert len({c.id for c in cases}) == len(cases)
    assert any(c.is_clean for c in cases)
    assert any(not c.is_clean for c in cases)
    assert any(c.requires_context and c.repository_files for c in cases)
    categories = {label.category for c in cases for label in c.labels}
    assert {"security", "correctness", "reliability"} <= categories


def test_malformed_case_raises(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid evaluation case"):
        load_cases(tmp_path)


def test_duplicate_case_id_raises(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"id": "dup"}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"id": "dup"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(tmp_path)


@pytest.mark.asyncio
async def test_failed_reviews_are_excluded_and_mark_the_report_incomplete() -> None:
    clean = EvalCase(id="clean")
    bug = EvalCase(id="bug", labels=(Label(path="a.py", line=10),))

    async def review_case(case: EvalCase) -> ReviewOutcome:
        raise RuntimeError("401 invalid api key")

    report = await run_eval(
        [clean, bug],
        review_case,
        provider="anthropic",
        mode="diff-only",
        repeats=1,
        settings=MatchSettings(),
    )
    assert not report.complete
    assert report.failed_runs == 2
    # Failures never score: no clean "pass", no missed labels.
    assert report.clean_runs == 0
    assert report.total_labels == 0
    payload = report_to_dict(report)
    assert payload["complete"] is False
    assert "INCOMPLETE" in render_table(report)


@pytest.mark.asyncio
async def test_withheld_runs_are_counted_by_reason() -> None:
    async def review_case(case: EvalCase) -> ReviewOutcome:
        return ReviewOutcome(withheld_reason="agent_incomplete")

    report = await run_eval(
        [EvalCase(id="clean")],
        review_case,
        provider="anthropic",
        mode="agent",
        repeats=2,
        settings=MatchSettings(),
    )
    assert report.complete
    assert report.withheld_runs == 2
    assert report.withheld_by_reason == {"agent_incomplete": 2}
    assert "withheld runs" in render_table(report)


@pytest.mark.asyncio
async def test_zero_repeats_is_rejected() -> None:
    async def review_case(case: EvalCase) -> ReviewOutcome:
        return ReviewOutcome()

    with pytest.raises(ValueError, match="repeats"):
        await run_eval(
            [EvalCase(id="c")],
            review_case,
            provider="x",
            mode="y",
            repeats=0,
            settings=MatchSettings(),
        )


def test_context_fixtures_agree_with_their_repository_files() -> None:
    """Diff line numbers and labels must point at the same lines agent mode reads via get_file."""
    for case in load_cases(CASES_DIR):
        for diff_file in case.diff_files:
            content = case.repository_files.get(diff_file.new_path or "")
            if content is None:
                continue
            file_lines = content.splitlines()
            for line in diff_file.lines:
                if line.side == "old" or line.new_line is None:
                    continue
                assert file_lines[line.new_line - 1] == line.content, (case.id, line.new_line)
        for label in case.labels:
            new_lines = {
                line.new_line
                for diff_file in case.diff_files
                if diff_file.new_path == label.path
                for line in diff_file.lines
                if line.new_line is not None
            }
            assert label.line in new_lines, (case.id, label.line)
