# Hooks

Event hooks of the plugin go into `hooks/hooks.json`, with their scripts next to it, referenced as
`${CLAUDE_PLUGIN_ROOT}/hooks/<script>`.

```text
Hook          Files                               Docs
------------  ----------------------------------  ---------------------
profile-gate  profile-gate, profile_gate.py       docs/profile-gate.md
```

Every hook script is checked with shellcheck (shell) or ruff, mypy and pytest (Python), and gets a
full review (adversarial and quality reviewers) before it is merged, because hooks run on every
matching tool call.
