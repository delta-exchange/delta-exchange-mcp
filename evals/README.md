# Evals

Scores MCP tool-selection quality, not API correctness: a real Claude agent is pointed at
the server over stdio and given trader prompts; we assert which tools it calls with which
args, plus advisory DeepEval LLM-judge scores (MCPUseMetric, MCPTaskCompletionMetric).
Run when renaming tools or rewriting docstrings. Deterministic asserts are the gate; judge
scores are informational.

Prerequisites: `ANTHROPIC_API_KEY`; testnet `DELTA_API_KEY`/`DELTA_API_SECRET` for
account and trade cases (cases whose tools are unregistered are skipped). All mutations are
forced to `dry_run=true` at the harness boundary; the harness refuses to run against
`india_prod`.

```bash
uv sync --group evals
uv run --group evals python -m evals.run --list
uv run --group evals python -m evals.run --case ticker_basic --no-judge   # cheap smoke
uv run --group evals python -m evals.run                                  # full run
```

A full run costs real tokens (rough order: $10-15 with Sonnet agent + Opus judge). Iterate
with `--case` and `--no-judge`. Models: `--model` / `--judge-model` or `DELTA_EVAL_MODEL` /
`DELTA_EVAL_JUDGE_MODEL`. JSON reports land in `evals/reports/` (gitignored) and record the
model ids, so description regressions aren't confused with model changes.

Never wire this into CI.
