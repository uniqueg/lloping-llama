# Agent sweeper

Agents leave clones, build outputs and local resources behind. Two commands keep the agent clone
root small without ever asking anything:

- `agent-ledger` records which agent owns which clone and which resources (kind clusters,
  containers, ports, namespaces) it created.
- `agent-sweep` runs hourly from a systemd user timer. It applies fixed rules, makes unclear cases
  safe (keeps them, or rescues the work before deleting) and logs every decision.

Both are in the plugin's `bin/` (on the Bash tool's PATH while the plugin is enabled). They need
Linux (they read `/proc`), Python 3.12 or later and `git`; nothing is installed with pip. `gh`,
`docker`, `kind`, `k3d` and `kubectl` are used when present.

## Safety guarantees

- **Unpushed work is rescued, never lost.** A clone whose work is not all on a remote is deleted
  only after 14 days without changes, and only after a rescue succeeded: a git bundle of its
  unpushed history plus a tarball of its uncommitted and untracked files. If the rescue fails, the
  clone stays.
- **Secrets are never copied.** Secret-like files are left out of every rescue and only listed in
  its manifest. Rescue directories are mode 0700, their files 0600, and credentials in remote URLs
  are stripped from the manifest.
- **Clones with secret-like files are kept.** If a clone holds secret-like files that git does not
  hold unchanged (untracked, ignored or modified), a rescue would be incomplete, so the clone is
  never deleted; it is listed in `last-sweep.txt` for you to look at. Its build outputs are still
  cleaned. The same holds if unpushed commits contain secret-like paths.
- **Deletion goes through `.trash`, with an age.** A clone is first renamed to
  `<root>/.trash/<name>-<ns>`; if anything uses the old or new path after the rename, it is renamed
  back (or kept as `.trash/<name>-<ns>-kept`, never pruned). Other trash leftovers are removed one
  hour after the time in their name, and only if nothing uses them.
- **Clones in use are never touched**: a process has its working directory, root, executable or an
  open file under the clone, a running container mounts it, or its ledger entry is `running` and
  the clone changed in the last 2 hours.
- **Only recorded resources are removed.** Unlabelled, unrecorded containers, clusters and
  namespaces are never touched, ports are only released in the ledger, and no process is killed.
- **Re-checked before every destructive step**: target inside the root, a fresh in-use scan, an
  unchanged fingerprint of HEAD and local refs, no change since planning, and a clean tree for
  safe clones.
- **Offline is safe**: without GitHub, pushes count as unverified, so nothing is deleted before
  14 days and never without a rescue.
- **One sweep at a time** (lock file), timeouts on every external call, and git runs with
  `--no-optional-locks`, `GIT_TERMINAL_PROMPT=0` and SSH `BatchMode`. The sweeper never writes to
  a clone's index and fetches with `gc.auto=0`, so it never makes a clone look active.

## Configuration

```text
Setting                 Where                    Default          Meaning
----------------------  -----------------------  ---------------  -------------------------------------
clone root              $AGENT_CLONES or --root  ~/agent-clones   directory that holds the agent clones
notification program    $AGENT_SWEEP_NOTIFY      (none)           called with one line of text when the
                                                                  disk is over 92 %, at most once a day
gh login                $GH_CONFIG_DIR           gh's default     used for PR and compare lookups
```

The ledger, logs, rescues and trash all live inside the clone root (`.ledger/`, `.rescue/`,
`.trash/`). Thresholds (idle times, disk limits, the list of build output names) are constants in
`sweeper/agent_sweeper/config.py`.

`agent-sweep` options: `--root DIR`, `--dry-run` (plan only, change nothing), `--plan-out FILE`
(dry run: write the plan there), `--cache FILE` (gh cache file), `--no-fetch`, `--offline` (no gh,
no remote calls).

## Install

The timer runs the commands from the plugin's directory, e.g. the marketplace clone at
`~/.claude/plugins/marketplaces/lloping-llama` (kept current by `/plugin marketplace update`), or
any clone of the repository. The steps below are for `bash` or `zsh`; they are safe to re-run.

**1. Render the unit templates and write a dry-run plan.** This enables nothing. The block runs in
a subshell that stops at the first failing step.

```sh
(
set -e
PLUGIN_DIR=~/.claude/plugins/marketplaces/lloping-llama
CLONE_ROOT=~/agent-clones
test -x "$PLUGIN_DIR/bin/agent-sweep"
mkdir -p "$CLONE_ROOT" ~/.config/systemd/user ~/.cache
sed -e "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" -e "s|@CLONE_ROOT@|$CLONE_ROOT|g" \
  "$PLUGIN_DIR/templates/systemd/agent-sweep.service" >~/.config/systemd/user/agent-sweep.service
cp "$PLUGIN_DIR/templates/systemd/agent-sweep.timer" ~/.config/systemd/user/
systemctl --user daemon-reload
systemd-analyze verify --user ~/.config/systemd/user/agent-sweep.service ~/.config/systemd/user/agent-sweep.timer
AGENT_CLONES=$CLONE_ROOT "$PLUGIN_DIR/bin/agent-sweep" --dry-run \
  --cache "$HOME/.cache/agent-sweep-dry-run-gh.json" --plan-out "$HOME/.cache/agent-sweep-plan.txt"
grep -q '; online$' ~/.cache/agent-sweep-plan.txt
sed -n '1,40p' ~/.cache/agent-sweep-plan.txt
)
```

The dry run takes a few minutes on many clones and prints a summary that starts with
`Agent sweeper DRY RUN (nothing changed)`. If the block stops at `grep`, the run was `OFFLINE`
(GitHub unreachable or gh not logged in): fix that and run the block again.

Edit `~/.config/systemd/user/agent-sweep.service` to set `AGENT_SWEEP_NOTIFY`, `GH_CONFIG_DIR` or
the `PATH` (git, gh, docker, kind/k3d and kubectl must be on it), then `systemctl --user
daemon-reload`.

Read the whole plan (`cat ~/.cache/agent-sweep-plan.txt`) and pin every clone an agent will still
reuse (`agent-ledger pin <clone>`; it also works for clones without a ledger entry) **before**
step 2.

**2. Enable the timer.** Real sweeps start now: the first may run at once, then hourly.

```sh
systemctl --user enable --now agent-sweep.timer
systemctl --user list-timers agent-sweep.timer --no-pager
```

Results: `cat ~/agent-clones/.ledger/last-sweep.txt`; unit logs:
`journalctl --user -u agent-sweep.service -n 50 --no-pager`.

### Uninstall

This keeps the ledger, the log, the rescues and any trash leftovers (`.ledger`, `.rescue`,
`.trash` in the clone root); check `.rescue/*/manifest.json` before you delete them by hand.

```sh
systemctl --user disable --now agent-sweep.timer
rm -f ~/.config/systemd/user/agent-sweep.service ~/.config/systemd/user/agent-sweep.timer
systemctl --user daemon-reload
```

## Ledger for sessions and agents

The main session writes an entry before it launches an agent (the clone name is the directory name
in the clone root):

```sh
agent-ledger new example-repo-42 --repo example-org/example-repo --issue 42 --branch fix/42-x -d "fix #42"
```

Agents record what they start, and label the containers they run:

```sh
agent-ledger add-resource example-repo-42 kind-cluster example-repo-42
agent-ledger add-resource example-repo-42 k8s-namespace example --kubeconfig ~/agent-clones/example-repo-42/.kube/config
agent-ledger add-resource example-repo-42 port 8081
docker run --label agent-ledger=example-repo-42 ...
```

State changes: `agent-ledger set-state example-repo-42 ready` (PR ready), `handed-over`, `merged`.
Clones an agent will reuse: `agent-ledger pin example-repo-42` (undo with `--off`).
`agent-ledger show` lists all entries, `agent-ledger show <clone>` prints one.

## Rules

**In use.** See [Safety guarantees](#safety-guarantees). In-use clones are never touched, nor
their resources.

**Last change.** The newest mtime in the clone outside disposable build outputs and `.git` (kept
build output dirs count), plus `.git/HEAD`, `index`, `logs/HEAD`, the stash and all refs except
remote-tracking ones (tags and notes count). The sweeper never counts its own fetches, and restores
the parent's mtime after deleting a build output.

**Build outputs.** `target/ node_modules/ .venv*/ .tox .nox .mypy_cache .pytest_cache .ruff_cache
dist/ build/ site/ .next/ coverage/ htmlcov/ .nyc_output/ playwright-report/ test-results/
ms-playwright/ .uv-cache/ .npm-cache/ .pnpm-store/ .turbo/ .cache/ .tools/ .uvc/ .pc-cache/` at any
depth. Deleted when the clone is idle and they are untouched for 2 hours, at once for the ledger
states `ready`, `handed-over` and `merged`, and regardless of age above 85 % disk use. Kept if git
tracks files inside, if `git ls-files` fails, or if the name is ambiguous (`build`, `dist`, `site`,
`target`, `coverage`, `test-results`, ...) and git does not ignore the dir, as it may hold new
source files (it then also makes the tree dirty). A build output dir with a `.git` entry anywhere
below it (e.g. an editable install under `.venv/src`) is kept and marks the clone as nested, so the
clone is kept too; uv's cache dirs and empty `.git` marker files don't count. In directories
without git only the unambiguous caches go (`node_modules`, `.venv*`, tool caches), never
`build/`, `dist/` or `site/`.

**Safe work.** A clean tree (untracked files inside untracked unambiguous caches don't count; a
dir git tracks is work whatever its name; ignored files are disposable), no stash, and every
commit of HEAD and all local refs (branches, tags, notes) reachable from a ref the remote has now
(`git ls-remote`, HTTPS first for GitHub, then the remote). Real runs `git fetch --prune` first.
Without a successful fetch, a tracking ref counts only if GitHub's compare API confirms that the
branch's current tip still contains it. A local tip inside a PR head that is not local (e.g. after
"Update branch") is checked with GitHub's compare API.

**Work done.** Ledger state `merged`, or every work branch (non-default branch) has a merged or
closed PR, or is merged into a remote default branch and deleted there.

**Delete clone.** Idle at least 2 hours, not pinned, no live linked worktrees, no nested
repositories or submodules (their work is not covered by the checks, so such clones are kept), and
either safe and done, or untouched for 14 days. Safe clones are deleted without a rescue (all their
commits are on a remote; `BUNDLE_SAFE_CLONES = True` adds a bundle first). Unsafe clones always get
a rescue first, unless a complete rescue newer than the last change exists. Linked worktrees go
via `git worktree remove`. Directories without git are never deleted.

**Rescue.** Made for unsafe clones untouched for 7 days and before deleting an unsafe clone. It
goes to `.rescue/<clone>-<date>[-n]/` and holds:

- `repo.bundle`: the clone's **unpushed history only**: every local ref except remote-tracking
  ones (branches, tags, notes, `refs/stash`, ...), every older stash entry and HEAD, each under
  `refs/sweeper/`, minus what is reachable from the commits verified on the remote. No bundle is
  written when nothing is unpushed. Commits reachable only from reflogs (after `reset --hard`,
  rebase leftovers) are not rescued.
- `uncommitted.tar.gz`: uncommitted and untracked files except unambiguous caches and secrets.
- `manifest.json` (written last): what was rescued, the remote URL without credentials, the
  `excluded_secrets` and the restore commands.

Secret-like files match `.env*` except `.env.example`, anything below `.kube/ .ssh/ .gnupg/
.docker/ .aws/`, the names `.netrc .npmrc .pypirc .git-credentials kubeconfig credentials.json`,
the suffixes `.pem .key .p12 .pfx .jks .keystore .kubeconfig` and the prefixes `id_rsa id_ed25519
id_ecdsa`. A rescue protects the clone only while its refs match the fingerprint in the manifest.
A rescue fails (and is removed) if git cannot list the uncommitted files, an untracked nested
repository is found, planning saw unpushed commits but no bundle results, or anything else goes
wrong; a directory without a manifest never counts as a rescue. Rescues older than 90 days are
pruned.

**Resources.** Only ledger-recorded ones, or docker containers and networks labelled
`agent-ledger=<id>` whose id has a ledger entry. Removed when the clone is gone or deleted, the
entry is `cleaned` or `merged`, or the clone and entry are idle for 24 hours. Kind and k3d clusters
go via their tool, else via docker labels. Namespaces only with a recorded kubeconfig whose current
context is `kind-*` or `k3d-*` and whose servers are all loopback hosts, so tunnels to real
clusters are never touched; the kubeconfig is read line by line, and anything unusual counts as not
local (the namespace is then only released in the ledger).

**Ledger.** Entries whose clone dir is gone are set `cleaned` after a 1-hour grace (sessions write
the entry before cloning); deleted clones' entries too. An entry that cannot be read keeps its
clone untouched, since it may have held a pin; an unknown state counts as `running`. Clones without
an entry follow the same rules from git, gh and mtimes.

**Disk.** Above 85 %: the build outputs of every idle clone go. Above 92 %: one notification via
`$AGENT_SWEEP_NOTIFY`, at most once a day, without repository names.

## Outputs

All in `<root>/.ledger/`:

- `sweep.log`: one JSON line per decision (`ts action clone target reason bytes status detail`;
  dry runs do not log), rotated at 5 MB.
- `last-sweep.txt`: a one-screen summary (freed GB, rescues, totals per action and skip reason),
  the clones kept for secret-like files and `.trash/*-kept` entries to review by hand.
- `gh-cache.json`: gh answers (1 hour for open PRs, 30 days for final ones while a PR head is still
  the branch tip, compare results for good).
- `state.json`: the time of the last disk notification.

## Restore a rescue

Rescues hold only unpushed history and never secrets, so a restore starts from the remote (its URL
is in `manifest.json`, which also lists the exact commands):

```sh
R=~/agent-clones/.rescue/<clone>-<date>
D=~/agent-clones/<clone>-restored
git clone <remote-url> "$D"
if [ -f "$R/repo.bundle" ]; then git -C "$D" fetch "$R/repo.bundle" 'refs/sweeper/*:refs/rescued/*'; fi
if [ -f "$R/uncommitted.tar.gz" ]; then tar -xzf "$R/uncommitted.tar.gz" -C "$D"; fi
```

Everything comes back under `refs/rescued/`: HEAD (also a detached one) as `refs/rescued/HEAD`,
branches as `refs/rescued/refs/heads/<name>`, the newest stash entry as `refs/rescued/refs/stash`
and older ones as `refs/rescued/stash/1`, `refs/rescued/stash/2`, ...
(`git -C "$D" stash apply refs/rescued/stash/1`). The bundle needs the commits it builds on, which
were on the remote when the rescue was made; if those were force-pushed away since, the fetch
reports missing prerequisites. Secret-like files were never copied and are gone with the clone
(clones that had any unsaved ones are kept instead).

## Development

```sh
cd sweeper
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```

The suite needs no network, docker or gh: `/proc`, docker, gh and disk usage are injected; git
runs for real on temporary repositories. Coverage must stay at 100 % (CI job `sweeper`).
