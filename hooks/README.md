# Hooks

Event hooks of the plugin go into `hooks/hooks.json`, with their scripts next to it, referenced as
`${CLAUDE_PLUGIN_ROOT}/hooks/<script>`.

```text
Hook          Files                               Docs
------------  ----------------------------------  ---------------------
profile-gate  profile-gate, profile_gate.py       docs/profile-gate.md
```

Every hook script gets a full review (adversarial and quality reviewers) before it is merged,
because hooks run on every matching tool call. Shell scripts are checked with shellcheck in CI;
Python hooks with ruff, mypy and pytest, locally for now (see CONTRIBUTING.md).
