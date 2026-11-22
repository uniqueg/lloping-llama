# Profile gate

`profile-gate` is a `PreToolUse` hook for the Bash tool. It applies the repository profiles of your
overlay's `profiles.yaml` (see [Profiles](profiles.md)) to `gh` and `git` commands: before a command
runs, it finds the actions that change a GitHub repository, works out which repository each one
targets and answers `deny` or `ask` where the profile doesn't allow the action.

It never answers `allow`. An action the profile allows is left to your other permission rules
(settings, auto mode, your global rules), so the gate can only add restrictions.

## Turn it on

The plugin registers the hook (`hooks/hooks.json`); it does nothing until you point it at your
profiles file. Set the variable in your user settings (`~/.claude/settings.json`):

```json
{
  "env": {
    "LLOPING_LLAMA_PROFILES": "~/overlay/profiles.yaml"
  }
}
```

The hook needs `python3` with PyYAML, or [uv](https://docs.astral.sh/uv/), which fetches PyYAML on
the first run. Without either, it asks before every gated action and says why; without `python3`,
it asks before every Bash command. Claude Code lets a command through when a hook times out, so warm
uv's cache once, e.g. `uv run --no-project --script hooks/profile_gate.py </dev/null` in the plugin
folder.

Without the plugin, register the script yourself, next to the `env` entry above:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash /path/to/lloping-llama/hooks/profile-gate",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

## What it checks

```text
Action  Commands
------  -------------------------------------------------------------------------------
push    git push (and git aliases that run it); gh api calls that create, update or
        delete refs (git/refs), rename branches, update a PR branch, or write contents
merge   gh pr merge (also --auto); gh api PUT pulls/<n>/merge and POST merges
```

Everything else passes: other `git` and `gh` commands, reads through `gh api`, and commands without
`git` or `gh`. GraphQL calls (`gh api graphql`) that run a merging or ref-changing mutation (e.g.
`mergePullRequest`, `createRef`), in a `query` field or an `--input` file, always ask, because
they name their target by node ID. So do gated calls to `repositories/<id>/...` routes.

## Verdicts

```text
Profile                    push                         merge
-------------------------  ---------------------------  ------------------------------------------
owner-sole                 -                            -
owner-blessed-maintainer   -                            ask: involve <maintainer>
owner-shared               -                            ask: needs consensus with <co_owners>
maintainer                 -                            ask: ask <owner>
developer                  -                            ask: ask <owner>
contributor                deny: push to your fork      deny: the maintainers merge
unknown repository         ask once per session: record its profile in profiles.yaml
```

`-` means no objection. Where a command holds several gated actions, the strictest verdict counts
(`deny` over `ask`), with the reasons of all actions that led to it. `unknown: deny` in
`profiles.yaml` denies gated actions on unknown repositories instead of asking.

### Allowing an action

`allowed_actions` lifts the verdict for the listed actions (`push`, `merge`), either for one
repository or for every repository of a profile:

```yaml
profiles:
  developer:
    allowed_actions: [merge]     # all developer repositories
repositories:
  - match: other-org/example-service
    profile: maintainer
    owner: carol
    responsibilities: [CI and release workflows, merges]
    allowed_actions: [merge]     # this repository only
```

`responsibilities` is free text for agents and people; the gate reads only `allowed_actions`.

### Unknown repositories

A repository without a matching entry gets one `ask` per session, with a message to record its
profile. Later gated actions on it in the same session pass to your other permission rules. The
sessions asked are kept in `profile-gate-asked.json` under `$LLOPING_LLAMA_STATE_DIR`, else
`$CLAUDE_PLUGIN_DATA`, else `$XDG_STATE_HOME/lloping-llama` (default `~/.local/state`).

## How it finds the repository

```text
Command        Repository
-------------  -----------------------------------------------------------------------------
git push       the push URLs of the remote named in the command (or --repo), else of the
               branch's pushRemote, remote.pushDefault, the branch's remote or origin;
               a URL in the command is used directly; local paths pass
gh pr merge    --repo/-R, else the repository in a PR URL, else GH_REPO, else the remote
               marked by gh repo set-default, else all remotes (the strictest one counts)
gh api         repos/<owner>/<repo>/... in the endpoint; {owner}/{repo} as for gh pr merge
```

`git -C <dir>`, `-c`, `--git-dir` and `--work-tree` are honoured, and so is `cd <dir>` earlier in
the same command (not out of a subshell); after `popd` the directory counts as unknown. Names
are matched case-insensitively as `owner/repo`; remotes outside github.com as `host/owner/repo`,
and, if that has no entry, as `owner/repo`, so that SSH host aliases for GitHub (e.g.
`github-work:owner/repo`) get the repository's profile.

Where the gate can't tell the target of a gated action (an unknown remote, `cd -`, a directory that
doesn't exist yet, unbalanced quotes), it asks.

## Limits

The gate guards against mistakes, not against a determined adversary. It reads the shell syntax that
agents write: chains (`&&`, `||`, `;`, `|`, newlines), subshells, command and process substitution
(also in double quotes), loops and conditionals (`do`, `then`, `{`, `!`, fish's `and`/`or`),
`VAR=value` prefixes, wrappers such as `env`, `sudo`, `timeout` and `command`, `bash -c`, `eval`,
redirections and git aliases. It does
not see through scripts in files, `export` in an earlier command, functions, `xargs` input or gh
aliases (`gh alias set`), and a quoted argument made only of operator characters (e.g. `-o ";"`)
splits the command there.
Heredocs are read as commands, so a heredoc line that looks like `git push` is checked too.

## Development

The hook is `hooks/profile-gate` (a Bash wrapper that picks the interpreter) and
`hooks/profile_gate.py`. Its tests (`tests/hooks/`) use local git repositories and never call `gh`.
See [Contributing](contributing.md) for the checks.
