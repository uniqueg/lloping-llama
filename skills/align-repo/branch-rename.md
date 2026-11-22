# Renaming long-lived branches

Example: a repository moves from `staging` (integration), an old `dev` and `master` to the model
`main + dev`: old `dev` → `dev-legacy`, `staging` → `dev` (default), `master` → `main`. Renames are UI
steps (Settings → Branches, or the branch list) by a repository admin. Agents prepare, check and clean
up around them.

## What follows the branch, what stays with the name

```text
Follows the branch (GitHub moves it)          Stays with the name (fix by hand)
--------------------------------------------  ------------------------------------------------------
open PRs' base (incl. from forks)             rulesets (ref patterns like refs/heads/dev)
draft releases' target                        environment deployment-branch policies
classic branch protection rules               workflows: on.push.branches, if: github.ref == ...
web URLs (tree/blob redirect)                 dependabot.yml target-branch (better: remove it)
the default-branch setting (if renamed)       image tags named after branches (old tags stay stale)
                                              badges and links with ?branch= or blob/<name>/
                                              docs hosting, coverage and app hosting branches
                                              contributors' local clones and open worktrees
                                              raw.githubusercontent.com URLs, scripts, docs
```

Consequence: a PR based on the old `dev` follows it to `dev-legacy` and must be retargeted to the new
`dev`; a PR based on `staging` follows it to the new `dev` and needs nothing.

## Order

A name can't be reused while a branch holds it, so free names first:

1. **Before:** list open PRs with their base
   (`gh pr list --repo <r> --state open --json number,baseRefName,headRefName`), rulesets and
   environment policies (export), workflow and `dependabot.yml` references
   (`grep -rnE 'staging|master|branches:|target-branch' .github`), badges and doc links. Tell the
   maintainer; agree on a time with no merges running.
2. **Rename old `dev` → `dev-legacy`** (frees `dev`). Then **`staging` → `dev`**. Then
   **`master` → `main`** (if `main` exists already: decide which one is canonical first). UI:
   `https://github.com/<r>/branches` → ⋯ next to the branch → Rename branch → new name → Rename branch.
   Expect GitHub's banner listing the retargeted PRs.
3. **Default branch** → `dev` (Settings → General → Default branch → ⇄), if it isn't already.
4. **Retarget PRs** that followed old `dev` to `dev-legacy` and belong on `dev`. Check each PR's state
   first: a base change on a closed PR reopens it. Only open PRs:
   `gh pr edit <n> --repo <r> --base dev`. Expect merge conflicts on PRs built on the old history: tell
   their authors; don't rebase other people's branches.
5. **Rulesets and environments** now apply to whatever holds the name. Check that the rulesets cover
   the new branches and that environment branch policies list only current names.
6. **Repository files:** one PR on the new `dev` updating workflow triggers, `dependabot.yml` (remove
   `target-branch`), badges, docs links, `edit_uri`, docs hosting and coverage notes.
7. **External services (UI):** docs hosting, coverage and app hosting default branches.
8. **Contributors' clones:** post the commands in the PR or send them to the maintainer (POSIX shell;
   adapt the three lines to the actual renames):

   ```sh
   # Run inside each clone (cd there first); stops at the first error.
   # Order matters: the old local dev must move away before staging becomes dev.
   set -e
   mv_branch() {
     if git show-ref -q --verify "refs/heads/$1"; then
       git branch -m "$1" "$2" && git branch -u "origin/$2" "$2"
     else
       echo "no local $1: skipped"
     fi
   }
   git fetch origin --prune
   mv_branch dev dev-legacy
   mv_branch staging dev
   mv_branch master main
   git remote set-head origin -a
   git branch -vv
   # Expect: dev [origin/dev], dev-legacy [origin/dev-legacy], main [origin/main];
   # "skipped" lines for branches you never had locally.
   # "fatal: ... already exists": a local branch with the new name is in the way;
   # rename or delete it, then run the block again.
   ```

   Worktrees: `git worktree list`; a worktree on a renamed branch follows the local rename.
9. **Verify:** `git ls-remote --heads origin`, default branch, open PRs' bases, a CI run on `dev`, and
   that Dependabot's next PR targets `dev`.
