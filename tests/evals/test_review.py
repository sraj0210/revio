"""Offline checks of the live reviewer's production-parity pieces (no provider is called)."""

import pytest

from evals.harness.review import FixtureContentPort, published_outcome
from venaroq.config.review import ReviewSettings
from venaroq.domain.models import Finding, ReviewResult


def _finding(line: int, confidence: float) -> Finding:
    return Finding(
        category="correctness",
        title="t",
        explanation="e",
        confidence=confidence,
        path="a.py",
        line=line,
    )


def test_low_confidence_findings_are_dropped_like_production() -> None:
    settings = ReviewSettings()
    result = ReviewResult(summary="s", findings=(_finding(1, 0.9), _finding(2, 0.1)))
    outcome = published_outcome(result, settings, token_reduced=False)
    assert [f.line for f in outcome.findings] == [1]
    assert outcome.withheld_reason is None


def test_overflow_withholds_all_findings() -> None:
    settings = ReviewSettings(review_max_findings=1)
    result = ReviewResult(summary="s", findings=(_finding(1, 0.9), _finding(2, 0.9)))
    outcome = published_outcome(result, settings, token_reduced=False)
    assert outcome.findings == ()
    assert outcome.withheld_reason == "findings_truncated"


def test_partial_agent_result_and_token_reduction_withhold_findings() -> None:
    settings = ReviewSettings()
    partial = ReviewResult(summary="s", findings=(_finding(1, 0.9),), partial=True)
    assert published_outcome(partial, settings, token_reduced=False).withheld_reason == (
        "agent_incomplete"
    )
    full = ReviewResult(summary="s", findings=(_finding(1, 0.9),))
    assert published_outcome(full, settings, token_reduced=True).withheld_reason == (
        "input_truncated"
    )


@pytest.mark.asyncio
async def test_fixture_tree_lists_direct_children_like_github() -> None:
    port = FixtureContentPort({"app/handler.py": "x", "app/sub/deep.py": "y", "README.md": "z"})
    root = await port.get_tree(None, "", "head")  # type: ignore[arg-type]
    assert [(e.path, e.entry_type) for e in root.items] == [
        ("README.md", "blob"),
        ("app", "tree"),
    ]
    app = await port.get_tree(None, "app", "head")  # type: ignore[arg-type]
    assert [(e.path, e.entry_type) for e in app.items] == [
        ("app/handler.py", "blob"),
        ("app/sub", "tree"),
    ]
