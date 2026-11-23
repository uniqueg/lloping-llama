# Hooks

Event hooks of the plugin go into `hooks/hooks.json`, with their scripts next to it, referenced as
`${CLAUDE_PLUGIN_ROOT}/hooks/<script>`. There are none yet.

Planned: a hook that applies the repository profile from your overlay's `profiles.yaml` to `gh`
and `git` actions (e.g. no merges in repositories where you are a contributor).

Every hook script is checked with shellcheck in CI and gets a full review (adversarial and quality
reviewers) before it is merged, because hooks run on every matching tool call.
