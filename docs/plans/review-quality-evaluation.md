# Review-quality evaluation harness

## Status

Draft for review. This document does not authorize implementation.

This is a **measurement tool**, not a product feature. It does not change the review service, the
durable pipeline, publishing, or any shipped behavior. Its only job is to answer the question we
currently cannot: **how good are Venaroq's reviews?** Everything built so far validates plumbing and
safety; nothing measures whether the findings are accurate or useful. This harness closes that gap.

## 1. Goal

Produce repeatable, numeric answers to: for a known set of code changes, **what fraction of real
defects does Venaroq catch (recall)**, **how much noise does it emit (false-positive rate /
precision)**, and **how do those change across provider (Anthropic vs OpenAI) and mode (diff-only vs
agent)?** Output a human-readable report plus machine-readable results, run on demand (not in CI).

## 2. The central honesty constraint

Measuring review *quality* requires **real model output** — it cannot be done with the mocked
responses the existing test suite uses. So this harness is a **live, on-demand, token-spending tool**,
separate from the deterministic CI suite. Its own scoring/matching logic is unit-tested offline; the
live evaluation is an explicit command an operator runs with real provider keys.

## 3. Decisions requiring approval

### Decision 1 — Dataset: curated synthetic fixtures first (the main decision)

Proposed: start with a **curated, version-controlled fixture set** under `evals/cases/`. Each case is
a realistic diff plus ground-truth labels — either "contains defect(s) of category C at path P around
line L" or "clean (no finding expected)". This is offline, reproducible, extensible, needs no external
data or scraping, and mirrors how the rest of the project is validated. We seed an initial ~30–50
cases spanning the review categories (correctness, security, reliability, maintainability), a mix of
**local** bugs (visible in the diff) and **context-dependent** bugs (only detectable with surrounding
file content — these specifically measure diff-only vs agent mode), plus clean diffs to measure false
alarms.

Rejected for v1: scraping real merged PRs / public bug datasets (SWE-bench/Defects4J-style). More
realistic, but heavy to curate/label, licensing-variable, and mostly not shaped like our review
contract. **Recommended as a fast-follow** once the harness exists, because synthetic fixtures over-
represent "clean, obvious" bugs and under-represent messy real-world distribution (stated plainly in
§7). Confirm whether synthetic-first is acceptable or you want real-PR data in v1.

### Decision 2 — What counts as a "catch" (the finding↔label matcher)

Proposed: a finding **matches** a labeled defect when it is on the **same file** and its line falls
within a **±N-line window** (default 3) of the label, optionally requiring category overlap. A labeled
defect with no matching finding is a **miss** (hurts recall); a finding matching no label — or any
finding on a clean-labeled diff — is a **false positive**. The matcher is deterministic and unit-
tested; the window/category strictness is configurable and reported alongside results so numbers are
interpretable, not a single opaque score.

### Decision 3 — Metrics

Proposed, reported per-run and aggregated, with per-category breakdown:
- **Recall** = matched labeled defects / total labeled defects
- **Precision** = matched findings / total findings
- **False-alarm rate on clean diffs** = findings on clean-labeled cases / clean cases
- **Variance across repetitions** (see Decision 4)

No single "score" is distilled; the tuple is reported so trade-offs (precision vs recall) stay visible.

### Decision 4 — Nondeterminism handling

Proposed: run each case **K times** (default 3) per configuration and report mean + spread, since AI
output varies run to run. This quantifies how stable the reviews are (itself a quality signal).

### Decision 5 — Comparison axes

Proposed: the runner takes `--provider {anthropic,openai}` and `--mode {diff-only,agent}` and reuses
the existing alias/profile selection and the real review pipeline (adapters + finding normalization),
**bypassing** the durable queue and GitHub publish path (it builds a `ReviewRequest` from a fixture
and calls the reviewer directly). This yields clean A/B numbers across the four combinations.

### Decision 6 — Placement and isolation

Proposed: live under `evals/` at the repo root (cases, runner CLI, scorer) — **not** inside the
shipped `src/venaroq` package. The live eval is a manual CLI (`uv run venaroq-eval ...` or a script);
it is **never** in CI. Only the pure scorer/matcher/loader logic gets offline unit tests in the normal
suite.

## 4. Scope and files

New (indicative):
- `evals/cases/**` — fixture cases (diff + labels) as JSON/YAML; a documented schema.
- `evals/harness/loader.py` — load + validate cases.
- `evals/harness/matcher.py` — deterministic finding↔label matching (Decision 2).
- `evals/harness/scorer.py` — metrics aggregation (Decision 3, 4).
- `evals/harness/runner.py` + a CLI entry — build `ReviewRequest` per case, invoke the selected real
  reviewer, collect findings, score, emit a JSON result + a human summary table.
- `tests/evals/**` — offline unit tests for loader/matcher/scorer against synthetic findings (no
  network).
- `docs/runbooks/review-quality-evaluation.md` — how to run it, read results, and add cases.

Not touched: the review service, adapters' behavior, durable pipeline, publishing, config defaults.
(The runner imports the adapters read-only to invoke them; it adds no production code paths.)

## 5. How it runs

```
uv run venaroq-eval --provider anthropic --mode diff-only --repeats 3 --cases evals/cases
```
Requires the relevant provider key (real calls, spends tokens). Emits `evals/results/<timestamp>.json`
and prints a table: recall / precision / clean-false-alarm / variance, overall and per category, with
the matcher settings used.

## 6. Out of scope

Real-PR / public-dataset ingestion (fast-follow, Decision 1); any change to review behavior; CI
integration of the live eval; auto-tuning prompts/thresholds from results (that's a later, separate
effort informed by this data); grading subjective "helpfulness" of prose beyond the match-based
metrics.

## 7. Honest limitations (must be stated in the report output too)

- **Synthetic fixtures are not the real-world distribution.** v1 numbers are **directional** — useful
  for A/B comparison (provider/mode) and regression tracking, not an absolute "accuracy" claim.
  Trustworthy absolute numbers need a real labeled PR corpus (fast-follow).
- Line-window matching can over- or under-credit; results are only interpretable with the matcher
  settings shown.
- Small K hides tail variance; large K costs tokens. Defaults favor cost.

## 8. Acceptance / completion

- A documented fixture schema and an initial curated case set (local + context-dependent + clean).
- A deterministic, unit-tested scorer/matcher.
- A live runner producing recall/precision/clean-false-alarm/variance, per-category, across the four
  provider×mode combinations, with matcher settings and limitations printed alongside.
- A runbook. No change to shipped behavior; the normal CI suite stays offline and deterministic.
