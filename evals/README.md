# Evals

This harness scores MCP tool selection, not Delta API correctness. It connects through
`server/discover`, requires MCP 2026, gives a real Claude agent trader prompts, and checks
which tools it calls with which arguments. DeepEval judge scores are advisory. The
deterministic checks are the gate.

The server must return the same tool list before and after account connection or trading
approval. A missing required tool fails the case. It does not become a skipped case.

The unit contract also checks the two authorization states that the paid runner cannot
combine in one call. A disconnected server must still discover all account and trading
tools. A selected real trading call without consent must return `input_required` before
the server sends any `POST`, `PUT`, or `DELETE` request. Model-driven mutation calls stay
separate and always receive `dry_run=true` from the runner.

Set `ANTHROPIC_API_KEY` to run an agent. Account-data cases also need a complete testnet
`DELTA_API_KEY` and `DELTA_API_SECRET` pair if the response content matters. Trading cases
do not need trading consent because the harness forces `dry_run=true` at the call boundary.
The harness uses a temporary settings path, does not export `DELTA_MCP_MODE`, and refuses
to run against production. It does not migrate or change the user's normal MCP settings.

```bash
uv sync --group evals
uv run --group evals python -m evals.run --list
uv run --group evals python -m evals.run --case ticker_basic --no-judge   # cheap smoke
uv run --group evals python -m evals.run                                  # full run
```

A full run costs real tokens. Start with one case and `--no-judge`. Select models with
`--model`, `--judge-model`, `DELTA_EVAL_MODEL`, or `DELTA_EVAL_JUDGE_MODEL`. JSON reports
land in `evals/reports/` and record the model IDs.

Never wire this into CI.
