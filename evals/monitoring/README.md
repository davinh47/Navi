# Monitoring fixtures

`cases.json` contains 30 authored cases: five concern families with explicit problems, legitimate work, and insufficient evidence. The `development` and `held_out` groups were written by the same developer; neither is an independently authored benchmark. Both groups are available in the repository and should not be treated as unseen validation data.

`tools/build_monitoring_cases.py` regenerates these fixtures. The additional 12 scope cases live in `evals/scope_cases.json`. See [sources and provenance](../SOURCES.md).

## Input and labels

Only `packet` is sent to the reviewer. Expected answers, split names, families and label rationale stay outside model input. Scope and implementation risk are scored separately. The local validator downgrades unsupported clean verdicts when coverage is partial, so results measure the combined model and validation pipeline.

The cases test explicit boundaries, including phase expansion, unrelated work, goal displacement, rationale turned into UI copy, excessive fallbacks and narrow hardcoding. They do not measure spontaneous drift during realistic long-running agent work, general architecture quality, or token savings.

## Optional paid evaluation

```sh
python3 tools/run_monitoring_eval.py --live
python3 tools/run_monitoring_eval.py --replay /absolute/saved/run
```

A live run makes up to seven Luna low calls, with at most 12 cases in each batch. It presents 57 cases including repetitions (42 distinct cases). This development harness uses its own bounded batch workflow; it does not change the installed plugin's task budget or default Luna high reviewer.

The harness records input hashes, available usage, failed calls, disagreements and repeated-run variation. It neither retries nor escalates models automatically. An invalid batch is not silently repaired. Live evaluations are excluded from CI and require a compatible Codex account.

These fixtures do not establish detection accuracy. Offline checks and their evidence boundaries are documented in [validation](../../docs/VALIDATION.md).
