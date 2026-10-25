"""Load and validate evaluation cases from a directory of JSON files."""

from pathlib import Path

from evals.harness.cases import EvalCase


def load_cases(directory: Path) -> list[EvalCase]:
    """Load every ``*.json`` case under ``directory`` (sorted), validating each against the schema.

    Raises ValueError with the offending path on a malformed case, and on duplicate case ids.
    """
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            case = EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"invalid evaluation case {path}: {error}") from error
        if case.id in seen:
            raise ValueError(f"duplicate evaluation case id {case.id!r} in {path}")
        seen.add(case.id)
        cases.append(case)
    return cases
