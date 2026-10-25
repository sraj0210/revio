"""Runner end-to-end (fake reviewer, offline) and loader validation of the seed cases."""

from pathlib import Path

import pytest

from evals.harness.cases import ChangeRequestSpec, EvalCase, FindingLite, Label
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

    async def review_case(case: EvalCase) -> list[FindingLite]:
        if case.id == "bug":
            return [FindingLite(path="a.py", line=11, category="security")]
        return []

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
    payload = report_to_dict(report)
    assert payload["recall"] == 1.0
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
