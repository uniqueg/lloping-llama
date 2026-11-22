# lloping-llama

A Claude Code plugin with skills, agents, hooks, workflows and templates for agent-assisted
software and project work: reviewers that check a branch before every push, a skill that aligns
repositories with an organization's policy, a status line that shows your usage limits, and a
template for a private **overlay** that holds everything about you, your organizations and your
projects. The kit stays generic; your overlay makes it yours.

Documentation: <https://lloping-llama.readthedocs.io/>

## Install

In Claude Code (2.1.275 or later):

```text
/plugin install lloping-llama --marketplace uniqueg/lloping-llama
```

Or in two steps, which also works in older versions:

```text
/plugin marketplace add uniqueg/lloping-llama
/plugin install lloping-llama@lloping-llama
```

Then restart Claude Code. Updates: `/plugin marketplace update lloping-llama`.

### Status line

Plugins can't set the status line, so add it to your `~/.claude/settings.json` yourself (needs
`bash` and `jq`):

```json
"statusLine": {"type": "command", "command": "bash ~/.claude/plugins/marketplaces/lloping-llama/bin/claude-statusline", "refreshInterval": 5}
```

## What's inside

```text
.claude-plugin/   plugin.json and marketplace.json
agents/           adversarial-reviewer, quality-reviewer, contract-reviewer
skills/           align-repo
hooks/            event hooks (profile-gate)
workflows/        Workflow tool scripts (none yet)
bin/              claude-statusline (on the Bash tool's PATH while the plugin is enabled)
templates/        overlay/: the structure of your private overlay, with fill-in instructions
docs/             the documentation (MkDocs Material)
```

## Use

- **Before every push**, ask Claude to run the reviewers on your branch, e.g. "review this branch
  against origin/main with the adversarial, quality and contract reviewers". They read the diff,
  verify each finding and report; they never edit, commit or push.
- **Align a repository** with your org's policy: `/lloping-llama:align-repo example-org/example-repo`
  (the policy lives in your overlay, see below).
- **Set up your overlay**: copy `templates/overlay/` into a new private repository, fill it in and
  import its `CLAUDE.md` from `~/.claude/CLAUDE.md`. See the
  [Overlay guide](https://lloping-llama.readthedocs.io/en/latest/overlay/).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Citation

If you use lloping-llama in your work, please cite it using the metadata in
[CITATION.cff](CITATION.cff).

## License

[Apache License 2.0](LICENSE)
