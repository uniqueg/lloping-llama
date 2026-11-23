# Getting started

## Requirements

- Claude Code 2.1.275 or later for the one-step install (older versions: two steps, below)
- `bash` and `jq` for the status line; `gh` and `jq` for the `align-repo` scripts

## Install the plugin

In Claude Code:

```text
/plugin install lloping-llama --marketplace uniqueg/lloping-llama
```

Or in two steps:

```text
/plugin marketplace add uniqueg/lloping-llama
/plugin install lloping-llama@lloping-llama
```

Restart Claude Code. Check that the agents are there with `/agents`; the skills show up as
`/lloping-llama:<name>`.

Update later with `/plugin marketplace update lloping-llama`, then restart.

## Add the status line

Plugins can't set the status line, so add this to `~/.claude/settings.json` (merge it into the
existing object):

```json
"statusLine": {"type": "command", "command": "bash ~/.claude/plugins/marketplaces/lloping-llama/bin/claude-statusline", "refreshInterval": 5}
```

The path is the marketplace's clone, which `/plugin marketplace update` keeps current. If your
Claude Code configuration lives elsewhere (`CLAUDE_CONFIG_DIR`), adjust the path. The script also
saves the status JSON to `/tmp/claude-<uid>/statusline/` (or `$CLAUDE_STATUS_DIR`), so that Claude
can read its own usage and pace its agents.

## Select the output style

The plugin ships the `Terse` output style (see [Reference](reference.md#output-styles)). Plugin
styles are named `<plugin>:<style>`; `/output-style` without an argument lists them. Select it
with:

```text
/output-style lloping-llama:Terse
```

`/output-style` and `/config` (Output style) save the choice for the current project in
`.claude/settings.local.json`. To make it your default everywhere, set it in
`~/.claude/settings.json`:

```json
"outputStyle": "lloping-llama:Terse"
```

The value must match the name in the list exactly; an unknown name falls back to the default
style. Changes apply to new sessions.

## Review a branch

Before you push, ask Claude:

```text
Review this branch against origin/main with the adversarial-reviewer, quality-reviewer and
contract-reviewer agents in parallel, then fix confirmed high and medium findings.
```

The reviewers only read: they never edit, commit or push.

## Set up your overlay

Copy `templates/overlay/` from this repository into a new private repository and follow the
[Overlay guide](overlay.md). Skills such as `align-repo` read your policies from there.
