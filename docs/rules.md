# Rules and levels

The kit ships the generic part of a set of global agent rules and four autonomy levels. Neither is
a plugin component: Claude Code doesn't load them by itself, so you import the rules and pass a
level when you start a session.

## Global rules

`rules/global.md` holds rules that apply to every project: hard limits (sensitive data, secrets,
default branches, posting in your name), how agents report, how they work with you, conventions for
Python and GitHub Actions, delegation to subagents, issues and PRs, release notes, releases, the
coding workflow and learnings. One rule per line, so a diff shows exactly which rule changed.

The parts that name people, organizations, hosts or local paths don't belong in the kit. Keep them
in your overlay, in the same sections (e.g. `me/rules.md`), and import both from your user memory
file, kit first:

```text
@~/path/to/lloping-llama/rules/global.md
@~/path/to/agent-overlay/me/rules.md
```

Use absolute (`~/…`) paths: if your user memory file is a symlink, relative imports may resolve
from the link's folder. The more specific layer wins (see [Overlay guide](overlay.md)).

Check that a new session loads them with `/memory`, which lists the imported files.

## Autonomy levels

A level says how much Claude may do on its own in a session. Each level is a folder in
`rules/levels/<name>/` with two files:

- `settings.json`: the permission mode and the `allow`, `ask` and `deny` rules that differ between
  levels.
- `rules.md`: the level's rules, appended to the system prompt, so they survive compaction and
  override the global rules where they differ.

```text
Level       Your role                                   Mode         Merges
----------  ------------------------------------------  -----------  ------------------------------
auto        none until the end                          auto         own PRs in repos you administer
reviewed    you review each PR before it is merged      auto         after a human review of the PR
supervised  you also approve key steps and each merge   acceptEdits  after a review, and it asks
external    you review the branch and open the PR       auto         none; pushes to the fork only
```

Start a session at a level:

```sh
level=supervised
claude --settings ~/path/to/lloping-llama/rules/levels/$level/settings.json \
  --append-system-prompt-file ~/path/to/lloping-llama/rules/levels/$level/rules.md
```

A shell function that wraps `claude` and takes a `--level` flag (default `supervised`) saves the
typing. Keep the permissions that are the same at every level in your user settings; Claude Code
combines the lists of all settings sources.

The `reviewed` and `supervised` rules name a human-review gate (a `level-gate.sh` hook and a
`claude-approve` command that records your approval of one PR). The kit doesn't ship them yet; until
it does, the rules still apply, but nothing enforces the gate.
