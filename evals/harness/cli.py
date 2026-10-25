"""Command-line entry for the live review-quality evaluation.

Run on demand with the relevant provider key in the environment; it spends tokens and is never
part of CI. Example:

    uv run python -m evals.harness.cli --provider anthropic --mode diff-only --repeats 3
"""

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from evals.harness.loader import load_cases
from evals.harness.matcher import MatchSettings
from evals.harness.review import build_review_case
from evals.harness.runner import render_table, run_eval, write_report_json


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="venaroq-eval", description="Measure Venaroq review quality (live; spends tokens)."
    )
    parser.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    parser.add_argument("--mode", choices=["diff-only", "agent"], default="diff-only")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", type=Path, default=Path("evals/cases"))
    parser.add_argument("--line-window", type=int, default=3)
    parser.add_argument("--require-category", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=Path("evals/results"))
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if not cases:
        parser.error(f"no cases found under {args.cases}")
    settings = MatchSettings(line_window=args.line_window, require_category=args.require_category)
    review_case = build_review_case(args.provider, args.mode)
    report = asyncio.run(
        run_eval(
            cases,
            review_case,
            provider=args.provider,
            mode=args.mode,
            repeats=args.repeats,
            settings=settings,
        )
    )
    print(render_table(report))
    args.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.results_dir / f"{args.provider}-{args.mode}-{stamp}.json"
    write_report_json(report, out)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
