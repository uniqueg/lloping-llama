# Reference

## Agents

```text
Agent                 Model    Tools                    Checks
--------------------  -------  -----------------------  -----------------------------------------------
adversarial-reviewer  inherit  Read, Grep, Glob, Bash   correctness, failure modes, concurrency,
                                                        security, resource exhaustion, contracts
quality-reviewer      sonnet   Read, Grep, Glob, Bash   duplication, dead code, tests, consistency,
                                                        docs, simplicity, commit hygiene
contract-reviewer     inherit  Read, Grep, Glob, Bash   API and spec style, breaking changes,
                                                        ecosystem conventions, docs coverage,
                                                        PR-body claims vs diff
```

All three take a repository path and a base ref (e.g. `origin/main`); the contract reviewer also
takes a PR number or a draft PR body. They review only `git diff <base>...HEAD`, verify each finding
before reporting it, report at most 10 findings, and never edit, commit, push or post. Their prompts
make them read-only; their `Bash` tool is needed for `git diff` and for running checks. Run them
before every push of code changes: the adversarial reviewer in parallel with the quality reviewer
followed by the contract reviewer. See [Reviewers](reviewers.md).

## Skills

```text
Skill        Call                                     Reads from the overlay
-----------  ---------------------------------------  -------------------------------------------
align-repo   /lloping-llama:align-repo <owner/repo>   profiles.yaml, orgs/<org>/repo-policy.md,
                                                      me/machine.md
```

`align-repo` ships `scripts/export-settings.sh` (read-only export of repository settings, run by the
repository owner) and `scripts/bulk-template.sh` (a resumable, dry-run-by-default skeleton for bulk
changes).

## bin

```text
Executable          Needs      Does
------------------  ---------  --------------------------------------------------------------
claude-statusline   bash, jq   status line: activity lamp, minutes since the last transcript
                               write, model, directory, 5-hour and 7-day usage with time to
                               reset; saves the status JSON to $CLAUDE_STATUS_DIR (default
                               /tmp/claude-<uid>/statusline)
agent-sweep         Linux,     autonomous cleanup of the agent clone root: build outputs,
                    python3    finished clones (after a rescue of unpushed work), recorded
                    >= 3.12,   resources; run hourly by a systemd user timer
                    git
agent-ledger        POSIX,     records which agent owns which clone and resources
                    python3
                    >= 3.12
```

The sweeper's Python package lives in `sweeper/` (standard library only, nothing to install); see
[Agent sweeper](sweeper.md).

Files in `bin/` are on the Bash tool's PATH while the plugin is enabled. Plugins can't set the
status line; see [Getting started](getting-started.md#add-the-status-line). The activity time uses
GNU `find -printf` and is left out where that is missing (e.g. macOS).

## Output styles

```text
Style  Selected as            Does
-----  ---------------------  ---------------------------------------------------------
Terse  lloping-llama:Terse    short terminal replies; full context only for decisions,
                              which go to the to-do board; to-dos as prerequisites and
                              exact steps; one line for background events; tables as
                              fixed-width text in code blocks
```

The style keeps Claude Code's coding instructions (`keep-coding-instructions: true`) and only
changes how Claude writes to you. It isn't applied automatically; select it as described in
[Getting started](getting-started.md#select-the-output-style).

## Hooks and workflows

None yet. See `hooks/README.md` and `workflows/README.md`.

## Templates

```text
Path                                         What
-------------------------------------------  ----------------------------------------------
templates/overlay/                           the overlay structure (see the Overlay guide)
templates/overlay/profiles.yaml              the six profiles and example repositories
templates/overlay/me/people.md               people schema with send: draft | auto
templates/overlay/me/collect-machine/        linux.sh (bash), macos.sh (sh/zsh/bash),
                                             windows.ps1 (PowerShell 5.1 and 7)
templates/overlay/orgs/example-org/          conventions.md, repo-policy.md
templates/overlay/projects/example-project/  state.md, decisions.md, runbooks/, drafts/,
                                             reports/
templates/systemd/                           agent-sweep.service (with placeholders) and
                                             agent-sweep.timer
```
