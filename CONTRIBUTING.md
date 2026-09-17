# Contributing

Navi is an early, standard-library Python project. Contributions that improve evidence quality,
portability, understandable reports and measurable cost are welcome.

## Local checks

```sh
python3 -m unittest discover -s tests -v
```

Use Python 3.9+ on a POSIX system. Offline tests use temporary directories and model stubs; no API key,
Codex login or paid model calls are required. The runtime is under `plugins/navi/scripts` and the user
workflow is in the packaged Navi skill. The GitHub workflow runs the offline suite, not live models.

## Design rules

- Keep reminders advisory: no tool denial, forced continuation or automatic rollback.
- Preserve user-source provenance and explicit amendments. Do not classify user intent with fixed keywords.
- Separate observed actions, unknown attribution, model judgments and agent claims.
- Keep expensive work outside hooks; bound packets, retries, background work and usage.
- Add behavioral tests for authority, concurrency, budget, data-loss or rollback changes.
- Do not replace uncertain evidence with a clean verdict to make a test pass.

## Evaluation

`evals/` contains authored replay cases, not natural execution trajectories or an official benchmark.
Gold labels stay outside model packets. `tools/run_monitoring_eval.py --live` is explicit opt-in and
may make up to seven paid Luna-low model calls; inspect its plan and model availability first. Batching
can fail schema/citation checks. Failures and missing usage must remain visible. Do not run it in CI.
See [fixture sources](evals/SOURCES.md).

`tools/update_local_plugin.py` is a maintainer utility for a matching **personal** marketplace and
requires the host's plugin-creator helpers. It is not the public installation command. It preserves
cache directories still referenced by active sessions. Normal repository consumers should follow
[installation](docs/INSTALL.md).

## Pull requests and reports

Describe the problem, final behavior and relevant checks. Use small synthetic fixtures. Do not attach
real conversations, model tokens/credentials, private source code or raw exported reports. Report
security-sensitive details using [SECURITY.md](SECURITY.md).

Contributions are provided under this repository's MIT license.
