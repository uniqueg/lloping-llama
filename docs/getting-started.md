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

Then tell the plugin's hooks where the overlay is, in `~/.claude/settings.json`:

```json
{
  "env": {
    "LLOPING_LLAMA_OVERLAY": "/home/you/path/to/agent-overlay"
  }
}
```

Without it, the Slack send guard blocks every Slack send (drafts still work); see
[Messages](messages.md).
