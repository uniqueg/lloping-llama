# Rules

The kit ships the generic part of a set of global agent rules. It is not a plugin component:
Claude Code doesn't load it by itself, so you import it from your user memory file.

## Global rules

`rules/global.md` holds rules that apply to every project: profiles (who may do what where), hard
limits (sensitive data, secrets, default branches, posting in your name), how agents report, how
they work with you, conventions for Python and GitHub Actions, delegation to subagents, issues and
PRs, release notes, releases, the coding workflow and learnings. One rule per line, so a diff shows
exactly which rule changed.

The parts that name people, organizations, hosts or local paths don't belong in the kit. Keep them
in your overlay, in the same sections (e.g. `me/rules.md`). Import both from your user memory
file, kit first, or import only your overlay's `CLAUDE.md`, which already imports both (the
overlay template does); not both ways, or the rules load twice:

```text
@~/path/to/lloping-llama/rules/global.md
@~/path/to/agent-overlay/me/rules.md
```

Use absolute (`~/…`) paths: if your user memory file is a symlink, relative imports may resolve
from the link's folder. The more specific layer wins (see [Overlay guide](overlay.md)).

The kit's file refers to things your overlay defines: the profiles file, step 1 of the coding
workflow (where you and agents work: clone folder), the work folder for checks and drafts,
`machine.md`, the people file (who may get auto-sent messages), the scheduled job for recurring
board sweeps, the learnings list, and your organization's labels (e.g. the `status:` labels). Put
them in your `me/rules.md` (or org files) under the same headings.

Check that a new session loads them with `/memory`, which lists the imported files.

## Rights: profiles, not session levels

What an agent may do depends on the repository, not on the session: each repository gets a
profile in your overlay's `profiles.yaml` (see [Profiles](profiles.md)), and the
[profile gate](profile-gate.md) hook applies it to pushes and merges. The global rules tie posting
in your name, merges and Dependabot updates to the same profiles, and give a repository that isn't
listed `contributor` rights until you record it.

Sessions therefore start without a level flag. Pick the permission mode in your user settings
(e.g. `"defaultMode": "auto"`); the gate can only add `ask` and `deny` on top of it.

!!! warning "The rules need a base deny list"

    The gate covers pushes and merges only. Before you allow `Bash(gh api *)` or switch to the
    mode `auto`, deny in your user settings at least: `gh release create/delete/delete-asset/upload`,
    `gh auth`, `gh secret`, `gh repo edit/delete`, risky `gh api` calls (patterns with `merge`,
    `DELETE`, `delete`, `protection`, `rulesets`, `collaborators`, `secrets`, `hooks`,
    `environments`, `--input`, and `=@` file paths outside temp folders), force-pushes (`--force`,
    `-f`, `+refspec`, `--mirror`) and reading your secret files; and put edits to your agent config
    (rules, agents, hooks, skills, settings) under `ask`. The kit doesn't ship this base yet.

## Reporting and output style

The rules don't prescribe how much Claude says in the terminal: it reports as usual. Set the
verbosity with `/config` (output style), e.g. the kit's `Terse` style.
