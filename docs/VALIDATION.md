# Validation and evidence boundaries

## Validation

Validation environment: macOS, Python 3.9.6 and `codex-cli 0.154.0-alpha.6.2`.

| Check | Result |
| --- | --- |
| Offline behavioral/integration suite | 303 tests passed |
| Plugin manifest and skill validation | Passed using the installed Codex plugin/skill validators |
| Clean local marketplace install | Passed with an isolated, previously empty `CODEX_HOME` |
| Anonymous GitHub clone and remote marketplace install | Passed without GitHub or model login |
| Installed package contents | 22 runtime/skill/manifest/license files matched source bytes |
| GitHub Actions | [Latest workflow results](https://github.com/davinh47/Navi/actions/workflows/tests.yml) |
| Release ZIP | Built and checked for archive integrity and per-file hashes |

Run the offline suite from a fresh checkout:

```sh
python3 -m unittest discover -s tests -v
python3 tools/build_release.py --output dist
```

These checks do not require a Codex account or call a live model. The manifest/skill validators are
host-provided development tools, not runtime dependencies. The GitHub Actions workflow runs the offline
suite and archive build on Linux (Python 3.9 and 3.12) and macOS (Python 3.12).

## What the suite exercises

User authority and amendments, incremental intent, source/citation validation, history formats and
gaps, context synchronization, partial evidence, opt-in/pause, asynchronous review state, scheduling
and budgets, reminder staleness/delivery records, usage reporting, provenance, selective rollback,
package relocation and preservation of cache versions used by active sessions.

Model outputs are mocked or authored fixtures. Passing tests establish the behavior covered by those
fixtures, not real-model accuracy. The optional evaluation harness has a test that verifies explicit
Luna low selection and a maximum seven-call plan without making any paid calls.

## Evidence not claimed

The offline suite does not establish native-host compatibility for every Codex installation or
real-model detection quality. Model outputs are mocked or supplied as authored fixtures.

Detection accuracy, realistic long-task coverage and net token savings have not been established.
Authored evaluation examples are documented in [fixture provenance](../evals/SOURCES.md).

## Continuous supervision regression checks

The offline suite simulates 60 reviews and 60 reminders over 60 checkpoints using an advanced clock
and model stubs. It checks that continuous scheduling remains active, detailed records roll over, and known
usage survives rollover. Additional checks cover hourly recovery, failed launches, concurrent manual
reservations, explicit budget exhaustion/removal, persisted-state compatibility and reminder cooldown/deduplication.
These are scheduling/state tests, not live-model drift detection results. No paid evaluation is needed.

## Delivery gap checks

`evals/delivery_cases.json` contains nine authored checkpoints: unauthorized scope reduction,
implementation substitution and unsupported completion, each with positive, legitimate and missing-evidence
conditions. The offline suite checks citation validation, existing-budget advisory routing, uncertainty,
stale/late reporting and the single-call harness. It does not validate model semantics with mocks.

```sh
python3 tools/run_delivery_eval.py
# Optional: one real reviewer call, consuming Codex usage. Defaults to Luna high.
python3 tools/run_delivery_eval.py --live
```

Only evidence packets reach the reviewer; expected answers remain outside the prompt. Local manifests,
responses and usage are saved under ignored `.navi-dev/` directories. One Luna high replay matched all
nine authored scope expectations using 5,153 input and 1,395 output tokens. This small development check
is not an independent benchmark or a real long-task end-to-end result. It does not measure spontaneous
drift, native reminder receipt or correction by an executing agent. The existing 30-case monitoring
fixture remains separate and unchanged.
