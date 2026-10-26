# Measuring review quality

The evaluation harness under `evals/` measures what the test suite cannot: how good the AI reviews
actually are. It replays a set of labeled cases through the real review pipeline and reports **recall**
(labeled defects caught), **precision**, **clean-diff false-alarm rate**, per-category recall, and
run-to-run **variance** — compared across provider (Anthropic/OpenAI) and mode (diff-only/agent).

It is a **live, on-demand tool**: measuring review quality requires real model output, so the runner
spends tokens and talks to a provider. It is **never part of CI**. Only the harness's own
loader/matcher/scorer logic is unit-tested offline (`tests/evals/`).

## Run it

Set the relevant provider credentials in the environment (same variables the service uses), then:

```bash
# Anthropic, diff-only, 3 repetitions
VENAROQ_ANTHROPIC_ENABLED=true VENAROQ_ANTHROPIC_API_KEY=sk-... \
  uv run python -m evals.harness.cli --provider anthropic --mode diff-only --repeats 3

# OpenAI, diff-only
VENAROQ_OPENAI_ENABLED=true VENAROQ_OPENAI_API_KEY=sk-... \
  uv run python -m evals.harness.cli --provider openai --mode diff-only

# Anthropic, agent mode (uses each case's repository_files as bounded context)
VENAROQ_ANTHROPIC_ENABLED=true VENAROQ_ANTHROPIC_API_KEY=sk-... \
  uv run python -m evals.harness.cli --provider anthropic --mode agent
```

Flags: `--repeats N` (variance), `--line-window N` (match tolerance, default 3),
`--require-category` (stricter matching; categories are compared case- and whitespace-insensitively), `--cases DIR`, `--results-dir DIR`. Each run prints a
summary table and writes a JSON report to `evals/results/`.

## Read the results

- **recall** — fraction of labeled defects the reviewer caught. Higher = misses fewer bugs.
- **precision** — fraction of line-anchored findings that matched a real labeled defect. Lower = more
  noise. (Summary-level findings with no line are reported separately and never counted as false
  positives, so general prose doesn't distort this.)
- **clean false-alarm** — average findings emitted on diffs labeled clean (0.0 is ideal).
- **recall by category** — correctness / security / reliability / maintainability.
- **variance** — spread of recall across repetitions; AI output is nondeterministic, so this is itself
  a quality signal.
- **unanchored findings** — summary-level findings with no line. Not scored, but counted (and counted
  separately on clean diffs) so summary noise stays visible.
- **withheld runs** — runs where the service would publish *no* findings (partial input, findings
  overflow, agent loop incomplete, unusable model output), broken down by reason. These score as what
  users see — nothing — so a high count here explains low recall.
- **INCOMPLETE** — any review that *failed* (bad credentials, rate limit, ambiguous provider call) is
  excluded from every metric and listed under `failures`. The report is marked `"complete": false`
  and the CLI exits non-zero; do not use an incomplete report for comparison.

Scores reflect **what the service would publish**, not raw model output: the harness applies the
same publish rules as `ReviewGenerationService` (diff limits, token-admission reduction, the
summary-confidence threshold, the findings cap, and withholding findings from partial results).
It bypasses only the durable queue, persistence, and the GitHub publish path.

The classic comparison is **diff-only vs agent** on the context-dependent cases, and **Anthropic vs
OpenAI** head-to-head on the same set.

## Honest limitations (also printed in every report)

The seed cases are **synthetic fixtures**, not the real-world defect distribution. The numbers are
**directional** — trustworthy for A/B comparison (provider/mode) and regression tracking, not as an
absolute "accuracy" claim. Matching is one-to-one and maximum (order-independent), but line-window matching can still over- or under-credit, so results are only
interpretable alongside the matcher settings shown in the report. Trustworthy *absolute* numbers
require a real labeled PR corpus — the planned fast-follow.

## Add cases

Drop a JSON file in `evals/cases/` following the schema in `evals/harness/cases.py` (`EvalCase`): a
diff (`diff_files`), optional `repository_files` for agent-mode context, and `labels` (expected
defects) — or an empty `labels` list for a clean case. The current set is a **starter**; grow it,
especially with more context-dependent and clean cases, which most sharply separate diff-only from
agent mode and expose false positives.
