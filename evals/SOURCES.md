# Evaluation provenance

The scope, monitoring, activity and delivery JSON files are authored replay fixtures. They do not contain actual
agent execution histories. Labels were assigned by the project developer and are not independently
validated ground truth. No published accuracy claim follows from agreement on these fixtures.

Six scope examples are inspired by these public Requests issues:

- [Requests #1142](https://github.com/psf/requests/issues/1142): bodyless GET Content-Length handling.
- [Requests #1921](https://github.com/psf/requests/issues/1921): omitting a session-default header.
- [Requests #2317](https://github.com/psf/requests/issues/2317): bytes HTTP method normalization.

The public fixtures use concise authored paraphrases, hypothetical action descriptions and tiny
illustrative expressions. Upstream issue texts, repositories, benchmark datasets and patches are not
bundled. The `benchmark_instance` field is an origin pointer, not evidence of a SWE-bench run or score.
Any separately downloaded third-party repository/dataset remains subject to its own license.

Monitoring fixtures cover five families with positive, negative and unknown examples. Development and
held-out groups were authored by the same developer; the held-out label does not imply independent
external validation. Expected labels are not included in the evaluator input packets.

`delivery_cases.json` is a separate nine-checkpoint authored fixture covering scope reduction,
implementation substitution and unsupported completion, each with positive, legitimate and
missing-evidence conditions. It contains hypothetical evidence, not private project transcripts or
natural agent trajectories. Its optional one-call harness is documented in [validation](../docs/VALIDATION.md#delivery-gap-checks).
