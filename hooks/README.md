# Hooks

Event hooks of the plugin go into `hooks/hooks.json`, with their scripts next to it, referenced as
`${CLAUDE_PLUGIN_ROOT}/hooks/<script>`.

```text
slack_send_guard.py   PreToolUse on slack_send_message and slack_schedule_message: blocks
                      sends to channels and groups, to persons not marked `send: auto` in
                      the people file, and auto-sent messages without the auto-send note
```

The guard needs `LLOPING_LLAMA_OVERLAY` (or `LLOPING_LLAMA_PEOPLE`) in the environment; see the
docs page "Messages".

Planned: a hook that applies the repository profile from your overlay's `profiles.yaml` to `gh`
and `git` actions (e.g. no merges in repositories where you are a contributor).

Every hook script is checked in CI (shell scripts with shellcheck; Python scripts with ruff,
mypy and pytest at 100% coverage, `tests/test_*.py`) and gets a full review (adversarial and quality
reviewers) before it is merged, because hooks run on every matching tool call.
