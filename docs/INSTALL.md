# Install Navi

## Tested host and requirements

Navi is an experimental **local Codex plugin**, tested on macOS with
`codex-cli 0.154.0-alpha.6.2`. Python 3.9+, Git, and a plugin/hook-capable Codex host are required.
Linux is a portability/CI target, not a completed native-host acceptance claim. Windows is unsupported
because the runtime uses POSIX locking and filesystem operations.

Independent evaluations launch `codex exec` with the user's existing login. The default review model
is `gpt-5.6-luna` with high reasoning; history interpretation defaults to medium. These model identifiers
may not be available to every account. Choose a supported model explicitly rather than silently falling
back. The plugin does not add a separate service or copy login credentials.

## Repository marketplace

```sh
codex plugin marketplace add https://github.com/davinh47/Navi.git
codex plugin add navi@navi
```

The first `navi` is the plugin name; the second is this repository's marketplace name.
The repository includes `.agents/plugins/marketplace.json`, pointing to `./plugins/navi`.
The package uses the supported `.codex-plugin/plugin.json` compatibility layout.

Review and enable the seven hooks in your host's plugin/hook settings. Start a new local session after
installation. An already-running session may still hold the previous plugin path; do not delete its
cache directory. For an existing conversation, restart the desktop host, reopen that conversation,
and have the agent verify the loaded version before relying on monitoring.

If remote marketplace installation is unavailable, clone and register the local repository:

```sh
git clone https://github.com/davinh47/Navi.git
cd Navi
codex plugin marketplace add "$PWD"
codex plugin add navi@navi
```

These are alternative registrations of the same marketplace, not two plugins to enable together.
Installation never grants permission to collect all workspace content. See [usage](USAGE.md) for opt-in.

## Verify and troubleshoot

Ask the executing agent to check:

1. The actual installed plugin path/version and hook trust state.
2. The global recorder switch and exact-workspace capture setting.
3. Task action capture, configured watched files/activity mode, and supervisor enablement.
4. Actual review model, profile, any explicit cumulative ceiling, used reservations, and `runtime.eligible`.
5. Bootstrap/history integration readiness and `context_sync` freshness for the current task.

A loaded skill, saved configuration, or old event log alone does not prove active supervision.
Missing Python, an unavailable model, untrusted hooks, or unsupported transcript records must be reported
as a gap; none should be described as a successful independent check.

If another `navi@personal` development installation is already enabled, disable one copy before using
this repository version. Marketplace data directories are distinct; this release does not automatically
migrate state from another marketplace or reset an existing conversation's budget.

Request the fixed `report.py --format brief` summary to inspect these conditions. Eligible means ready
to consider an evaluation, not that a review passed. A running review and an exhausted explicit budget
are different states. See [report troubleshooting](REPORTING.md#troubleshooting-status).

## Updates and removal

```sh
codex plugin marketplace upgrade navi
codex plugin add navi@navi
```

Reload the host/session after updating so its skill and hook paths are refreshed. Existing conversation
counters and explicitly set ceilings remain; updating does not authorize a budget reset or remove a
ceiling. Ask the agent to verify the loaded report command if it still paraphrases status instead of
including the fixed summary. Disable the plugin in the host to stop its hooks. The local
pause command in [usage](USAGE.md) stops recording without changing Codex permissions. Uninstalling a
plugin is not a promise to erase exported reports or host conversation history.

## Official references

- [Plugin packaging and repository marketplaces](https://developers.openai.com/plugins/build/plugins)
- [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands)
- [Hooks and trust](https://learn.chatgpt.com/docs/hooks)

Checked during public release preparation; host behavior can change.
