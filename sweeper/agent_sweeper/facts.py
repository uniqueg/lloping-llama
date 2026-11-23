"""Collectors that turn the machine state into :mod:`agent_sweeper.rules` facts.

Every external call has a timeout. Collectors never change a clone: git
runs with ``--no-optional-locks`` and modification times are read before
any git command. Only :func:`git_facts` with ``fetch=True`` (real runs)
updates remote-tracking refs, which the rules never count as activity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from agent_sweeper import config
from agent_sweeper.config import Config, is_build_output, is_secret
from agent_sweeper.ledger import Entry, Ledger
from agent_sweeper.rules import (
    Branch,
    BuildOutput,
    CloneFacts,
    DockerResource,
    GitFacts,
    RescueInfo,
    World,
)
from agent_sweeper.util import Result, disk_percent, du, run

#: Signature of the injectable /proc scanner: returns (path, pid) pairs.
ProcScan = Callable[[], Iterable[tuple[str, int]]]
#: Signature of the injectable command runner (see :func:`agent_sweeper.util.run`).
Runner = Callable[..., Result]

#: Git files whose change counts as activity in a clone.
GIT_ACTIVITY_FILES = ("HEAD", "index", "logs/HEAD", "refs/stash", "logs/refs/stash")
GITHUB_URL = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")
RESCUE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}(?:-\d+)?$")


def _ancestors(proc: Path) -> dict[int, int]:
    """Map every visible pid to its parent pid.

    Args:
        proc: The proc file system root.

    Returns:
        ``{pid: ppid}``.
    """
    parents: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            parents[int(entry.name)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return parents


def scan_proc(proc: Path = Path("/proc"), self_pid: int | None = None) -> list[tuple[str, int]]:
    """List the working directories and open files of all processes.

    The sweeper itself and its descendants are left out.

    Args:
        proc: The proc file system root (injectable for tests).
        self_pid: Pid to exclude with its descendants; defaults to ours.

    Returns:
        ``(path, pid)`` pairs.
    """
    me = os.getpid() if self_pid is None else self_pid
    parents = _ancestors(proc)

    def is_ours(pid: int) -> bool:
        seen = 0
        while pid > 1 and seen < 64:
            if pid == me:
                return True
            pid = parents.get(pid, 0)
            seen += 1
        return False

    found: list[tuple[str, int]] = []
    for pid in parents:
        if is_ours(pid):
            continue
        base = proc / str(pid)
        for link in ("cwd", "root", "exe"):
            try:
                found.append((os.readlink(base / link), pid))
            except OSError:
                pass
        try:
            fds = list((base / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("/"):
                found.append((target, pid))
    return found


def docker_mounts(runner: Runner = run) -> list[tuple[str, int]]:
    """List host paths mounted into running docker containers.

    Args:
        runner: Command runner.

    Returns:
        ``(source path, 0)`` pairs; empty if docker is unavailable.
    """
    ids = runner(["docker", "ps", "-q"], timeout=config.TIMEOUT_DOCKER)
    if not ids.ok or not ids.out.split():
        return []
    fmt = "{{range .Mounts}}{{.Source}}\n{{end}}"
    res = runner(["docker", "inspect", "-f", fmt, *ids.out.split()], timeout=config.TIMEOUT_DOCKER)
    if not res.ok:
        return []
    return [(line, 0) for line in res.out.splitlines() if line.startswith("/")]


def users_by_clone(root: Path, pairs: Iterable[tuple[str, int]]) -> dict[str, set[int]]:
    """Group process paths by the clone directory they lie in.

    Args:
        root: Clone root.
        pairs: ``(path, pid)`` pairs from :func:`scan_proc` and mounts.

    Returns:
        ``{clone name: {pids}}`` (pid 0 stands for a container mount).
    """
    prefixes = {str(root.resolve()) + "/", str(root.absolute()) + "/"}
    users: dict[str, set[int]] = {}
    for path, pid in pairs:
        for prefix in prefixes:
            if path.startswith(prefix):
                name = path[len(prefix) :].split("/", 1)[0]
                if name:
                    users.setdefault(name, set()).add(pid)
                break
    return users


class Tree(NamedTuple):
    """Result of :func:`scan_tree`.

    Attributes:
        newest: Newest modification time outside build outputs and ``.git``.
        size: Bytes outside build outputs (``.git`` included).
        git_size: Bytes of the top-level ``.git``.
        outputs: ``(build dir, mtime)`` pairs.
        nested: Whether a nested repository or a ``.gitmodules`` file exists.
        nested_outputs: Build output dirs with a git repository inside
            (e.g. an editable install under ``.venv/src``); never deleted.
        secrets: Secret-like files outside build outputs (absolute paths).
    """

    newest: float
    size: int
    git_size: int
    outputs: list[tuple[str, float]]
    nested: bool = False
    nested_outputs: list[str] = []  # noqa: RUF012 - NamedTuple default, never mutated
    secrets: list[str] = []  # noqa: RUF012 - NamedTuple default, never mutated


def uv_git_cache(path: str) -> bool:
    """Tell whether a directory is uv's cache of git checkouts.

    Args:
        path: Directory.

    Returns:
        True for ``git-v0`` below ``.uv-cache`` or ``.uvc``, and for any
        ``.cache/uv`` (uv keeps clones of git dependencies there; they are
        downloads, not work).
    """
    parts = path.split("/")
    if parts[-1] == "git-v0" and any(p in (".uv-cache", ".uvc") for p in parts):
        return True
    return parts[-2:] == [".cache", "uv"]


def is_marker(item: os.DirEntry[str]) -> bool:
    """Tell whether a ``.git`` entry is an empty marker file, not a repository.

    uv writes such files into its cache buckets (e.g. ``sdists-v9/.git``).

    Args:
        item: A ``.git`` directory entry.

    Returns:
        True for an empty regular file.
    """
    return item.is_file(follow_symlinks=False) and item.stat(follow_symlinks=False).st_size == 0


def has_git_below(path: str) -> bool:
    """Tell whether a ``.git`` entry exists anywhere below a directory.

    Only names are read, no stat calls, so even large caches are cheap.
    uv's git checkout caches are not searched (see :func:`uv_git_cache`),
    and empty ``.git`` marker files do not count.

    Args:
        path: Directory.

    Returns:
        True at the first ``.git`` file or directory found.
    """
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it as entries:
            for item in entries:
                try:
                    if item.name == ".git" and not is_marker(item):
                        return True
                    if item.is_dir(follow_symlinks=False) and not uv_git_cache(item.path):
                        stack.append(item.path)
                except OSError:
                    continue
    return False


def scan_tree(path: Path, git: bool) -> Tree:
    """Walk a clone once for activity, size, build outputs and nested repos.

    Args:
        path: Clone directory.
        git: Whether it is a git clone (selects the build output names).

    Returns:
        The walk result. ``.git`` counts toward the size, but its files do
        not count as activity (see :func:`git_activity`).
    """
    nested = (path / ".gitmodules").exists()
    nested_outputs: list[str] = []
    newest = 0.0
    size = 0
    git_size = 0
    outputs: list[tuple[str, float]] = []
    secrets: list[str] = []
    root = str(path)
    stack = [root]
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return Tree(0.0, 0, 0, [])
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it as entries:
            for item in entries:
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if item.name == ".git" and current != str(path):
                    nested = True
                if item.is_dir(follow_symlinks=False):
                    if item.name == ".git" and current == str(path):
                        git_size = du(item.path)
                        size += git_size
                        continue
                    if is_build_output(item.name, git):
                        outputs.append((item.path, st.st_mtime))
                        if has_git_below(item.path):
                            nested_outputs.append(item.path)
                        continue
                    size += st.st_blocks * 512
                    newest = max(newest, st.st_mtime)
                    stack.append(item.path)
                else:
                    size += st.st_blocks * 512
                    if not (item.name == ".git" and current == str(path)):
                        newest = max(newest, st.st_mtime)
                    if is_secret(item.path[len(root) + 1 :]):
                        secrets.append(item.path)
    nested = nested or bool(nested_outputs)
    return Tree(newest, size, git_size, outputs, nested, nested_outputs, secrets)


def git_dir_of(path: Path) -> Path | None:
    """Return the git directory of a clone or linked worktree.

    Args:
        path: Clone directory.

    Returns:
        The git directory, or None if there is none (or it is broken).
    """
    dot = path / ".git"
    if dot.is_dir():
        return dot
    try:
        text = dot.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (path / gitdir).resolve()
    return gitdir if gitdir.is_dir() else None


def git_activity(gitdir: Path) -> float:
    """Return the newest change of the git files that mark activity.

    HEAD, index, reflog, stash and every ref except remote-tracking ones
    (fetches by the sweeper must not count).

    Args:
        gitdir: Git directory.

    Returns:
        Newest modification time, 0 if none exists.
    """
    newest = 0.0
    for rel in GIT_ACTIVITY_FILES:
        try:
            newest = max(newest, os.lstat(gitdir / rel).st_mtime)
        except OSError:
            pass
    refs = gitdir / "refs"
    for dirpath, dirs, files in os.walk(refs):
        if dirpath == str(refs):
            dirs[:] = [d for d in dirs if d != "remotes"]
        for name in [*files, "."]:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return newest


def linked_worktrees(gitdir: Path) -> list[str]:
    """List the live linked worktrees of a main clone.

    Args:
        gitdir: Git directory of the main clone.

    Returns:
        Paths of worktrees whose directory still exists.
    """
    live: list[str] = []
    base = gitdir / "worktrees"
    if not base.is_dir():
        return live
    for wt in sorted(base.iterdir()):
        try:
            dotgit = Path((wt / "gitdir").read_text().strip())
        except OSError:
            continue
        if dotgit.exists():
            live.append(str(dotgit.parent))
    return live


def github_repo(url: str) -> str | None:
    """Extract ``owner/name`` from a GitHub remote URL.

    Args:
        url: Remote URL.

    Returns:
        ``owner/name`` or None for other hosts.
    """
    match = GITHUB_URL.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


class GhCache:
    """Thread-safe cache of ``gh pr list`` answers.

    Args:
        path: Cache file.
        runner: Command runner.
        enabled: Whether gh may be called (False when offline).
    """

    def __init__(self, path: Path, runner: Runner = run, enabled: bool = True) -> None:
        """Load the cache.

        Args:
            path: Cache file.
            runner: Command runner.
            enabled: Whether gh may be called.
        """
        self.path = path
        self.runner = runner
        self.enabled = enabled
        self.lock = threading.Lock()
        self.calls = 0
        try:
            self.data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def prs(self, repo: str, branch: str, now: float, tip: str = "") -> list[dict[str, Any]] | None:
        """Return the PRs whose head is ``branch`` in ``repo``.

        A cached answer whose PRs are all final is reused for long, but only
        if one of their heads is the current tip: a new commit on a reused
        branch name may belong to a new PR.

        Args:
            repo: ``owner/name``.
            branch: Head branch name.
            now: Current time.
            tip: Current local tip of the branch ("" to skip that check).

        Returns:
            PR records (``number``, ``state``, ``headRefOid``), or None if
            unknown (gh disabled or failed).
        """
        key = f"{repo}#{branch}"
        with self.lock:
            hit = self.data.get(key)
        if hit is not None:
            prs = hit["prs"]
            final = bool(prs) and all(p.get("state") in ("MERGED", "CLOSED") for p in prs)
            moved = final and bool(tip) and all(p.get("headRefOid") != tip for p in prs)
            ttl = config.GH_CACHE_TTL_FINAL if final else config.GH_CACHE_TTL_OPEN
            if not moved and now - hit["ts"] < ttl:
                return list(prs)
        if not self.enabled:
            return None
        res = self.runner(
            [
                "gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                "--limit", "20", "--json", "number,state,headRefOid",
            ],
            timeout=config.TIMEOUT_GH,
        )  # fmt: skip
        with self.lock:
            self.calls += 1
        if not res.ok:
            return None
        try:
            prs = json.loads(res.out)
        except ValueError:
            return None
        with self.lock:
            self.data[key] = {"ts": now, "prs": prs}
        return list(prs)

    def contains(self, repo: str, base: str, head: str) -> bool | None:
        """Tell whether GitHub has ``base`` as an ancestor of ``head``.

        Answers are immutable, so they are cached for good; failures (e.g.
        ``base`` unknown to GitHub) are not cached.

        Args:
            repo: ``owner/name``.
            base: Commit that must be contained (a local branch tip).
            head: Commit on GitHub (a PR head).

        Returns:
            True if ``head`` contains ``base``, False if not, None if unknown.
        """
        key = f"compare:{repo}:{base}...{head}"
        with self.lock:
            hit = self.data.get(key)
        if hit is not None:
            return bool(hit["contains"])
        if not self.enabled:
            return None
        res = self.runner(
            ["gh", "api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".status"],
            timeout=config.TIMEOUT_GH,
        )
        with self.lock:
            self.calls += 1
        if not res.ok:
            return None
        contains = res.out.strip() in ("ahead", "identical")
        with self.lock:
            self.data[key] = {"ts": 0, "contains": contains}
        return contains

    def save(self) -> None:
        """Write the cache atomically; errors are ignored."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            with self.lock:
                tmp.write_text(json.dumps(self.data, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError:
            pass


def pick_pr(prs: list[dict[str, Any]] | None, tip: str) -> dict[str, Any] | None:
    """Choose the PR that describes a branch tip.

    Args:
        prs: PRs with this head branch name.
        tip: Branch tip SHA.

    Returns:
        The PR whose head is the tip, else the newest PR, or None.
    """
    if not prs:
        return None
    exact = [p for p in prs if p.get("headRefOid") == tip]
    return exact[0] if exact else max(prs, key=lambda p: int(p.get("number", 0)))


def _git(path: Path, runner: Runner, *args: str, stdin: str | None = None) -> Result:
    """Run a local git command in a clone.

    Args:
        path: Clone directory.
        runner: Command runner.
        *args: Git arguments.
        stdin: Standard input.

    Returns:
        The result.
    """
    return runner(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        timeout=config.TIMEOUT_GIT_LOCAL,
        stdin=stdin,
    )


def tracked_dirs(path: Path, runner: Runner) -> set[str] | None:
    """List every directory that holds files git tracks.

    Args:
        path: Clone directory.
        runner: Command runner.

    Returns:
        Relative directory paths (all ancestors included), or None if git
        cannot tell.
    """
    files = _git(path, runner, "ls-files", "-z")
    if not files.ok:
        return None
    tracked: set[str] = set()
    for rel in files.out.split("\0"):
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            tracked.add("/".join(parts[:i]))
    return tracked


def cache_exempt(rel: str, tracked: set[str] | frozenset[str] | None) -> bool:
    """Tell whether an untracked path lies in an untracked cache directory.

    Only untracked cache dirs (``node_modules``, ``.venv``, ``.cache``, ...)
    are disposable: a dir git tracks, whatever its name, holds real work.

    Args:
        rel: Untracked path relative to the clone.
        tracked: Result of :func:`tracked_dirs`; None means unknown, and
            then nothing is exempt.

    Returns:
        True if some directory component is a cache name and that
        directory is not tracked.
    """
    if tracked is None:
        return False
    parts = rel.split("/")[:-1]  # directory components only ("x/" ends in "")
    return any(
        is_build_output(part, git=False) and "/".join(parts[: k + 1]) not in tracked
        for k, part in enumerate(parts)
    )


def parse_status(out: str, tracked: set[str] | frozenset[str] | None = frozenset()) -> bool:
    """Tell whether ``git status --porcelain -z`` output shows real changes.

    Changes to tracked files always count. Untracked paths count, except
    inside untracked unambiguous caches such as ``node_modules`` or
    ``.venv`` (see :func:`cache_exempt`); an untracked ``build/`` or
    ``site/`` may hold new source files.

    Args:
        out: Output of ``git status --porcelain=v1 -z``.
        tracked: Tracked directories (see :func:`tracked_dirs`).

    Returns:
        True if anything outside those caches changed.
    """
    items = out.split("\0")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        code, rel = item[:2], item[3:]
        if code[0] in "RC":
            i += 1  # the next item is the rename source
        if code == "??" and cache_exempt(rel, tracked):
            continue
        return True
    return False


def git_facts(
    path: Path,
    gitdir: Path,
    runner: Runner,
    gh: GhCache | None,
    now: float,
    fetch: bool,
    online: bool,
) -> GitFacts:
    """Collect the git facts of one clone.

    Args:
        path: Clone directory.
        gitdir: Its git directory.
        runner: Command runner.
        gh: gh cache, or None to skip PR lookups.
        now: Current time.
        fetch: Run ``git fetch --prune`` first.
        online: Whether remotes can be reached.

    Returns:
        The facts; unknown values stay None (and count as unsafe).
    """
    status = _git(path, runner, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    dirty = parse_status(status.out, tracked_dirs(path, runner)) if status.ok else None
    stash = _git(path, runner, "rev-parse", "--verify", "-q", "refs/stash").ok
    heads_res = _git(path, runner, "for-each-ref", "--format=%(refname:short) %(objectname)",
                     "refs/heads")  # fmt: skip
    local: dict[str, str] = {}
    for line in heads_res.out.splitlines():
        name, _, sha = line.partition(" ")
        local[name] = sha
    main_repo = None
    if (path / ".git").is_file():
        common = gitdir / "commondir"
        try:
            main_repo = str((gitdir / common.read_text().strip()).resolve().parent)
        except OSError:
            main_repo = str(gitdir.parent.parent.parent)
        worktrees: list[str] = []
    else:
        worktrees = linked_worktrees(gitdir)

    remotes = [r for r in _git(path, runner, "remote").out.split() if r]
    remote_shas: set[str] = set()
    remote_heads: set[str] = set()
    default_names = set(config.DEFAULT_BRANCHES)
    default_shas: set[str] = set()
    reached = 0
    gh_repos: list[str] = []
    for remote in remotes:
        url = _git(path, runner, "remote", "get-url", remote).out.strip()
        repo = github_repo(url)
        if repo and repo not in gh_repos:
            gh_repos.append(repo)
        if not online:
            continue
        fetched = (
            fetch
            and runner(
                [
                    "git",
                    "-C",
                    str(path),
                    "-c",
                    "gc.auto=0",
                    "-c",
                    "maintenance.auto=false",
                    "fetch",
                    "--prune",
                    "--quiet",
                    remote,
                ],
                timeout=config.TIMEOUT_GIT_REMOTE,
            ).ok
        )
        # Public GitHub repositories answer over HTTPS without SSH keys, so
        # the sweeper works without an SSH agent; the remote is the fallback.
        targets = [f"https://github.com/{repo}.git", remote] if repo else [remote]
        for target in targets:
            res = runner(
                ["git", "-C", str(path), "ls-remote", "--symref", target],
                timeout=config.TIMEOUT_GIT_REMOTE,
            )
            if res.ok:
                break
        if not res.ok:
            continue
        reached += 1
        heads: dict[str, str] = {}
        for line in res.out.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                default_names.add(line[len("ref: refs/heads/") : -len("\tHEAD")])
                continue
            sha, _, ref = line.partition("\t")
            remote_shas.add(sha)
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/") :]] = sha
        remote_heads.update(heads)
        if not fetched and repo and gh is not None:
            # Without a fetch the remote tips may be missing locally. A
            # tracking ref stands in for its branch only if GitHub confirms
            # that the current tip still contains it (not force-pushed or
            # recreated).
            fmt = "--format=%(refname:strip=3) %(objectname)"
            tracking = _git(path, runner, "for-each-ref", fmt, f"refs/remotes/{remote}")
            for line in tracking.out.splitlines():
                name, _, sha = line.partition(" ")
                tip = heads.get(name)
                if tip and sha != tip and gh.contains(repo, sha, tip):
                    remote_shas.add(sha)
        default_shas.update(sha for name, sha in heads.items() if name in default_names)

    present: set[str] = set()
    if remote_shas:
        check = _git(path, runner, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                     stdin="\n".join(sorted(remote_shas)) + "\n")  # fmt: skip
        present = {line.split()[0] for line in check.out.splitlines() if line.endswith(" commit")}

    def covered(tip: str, shas: set[str]) -> bool:
        stdin = "".join(f"^{sha}\n" for sha in sorted(shas))
        res = _git(path, runner, "rev-list", "--count", tip, "--stdin", stdin=stdin)
        return res.ok and res.out.strip() == "0"

    branches: list[Branch] = []
    verified: set[str] = set()
    for name, tip in sorted(local.items()):
        if name in default_names:
            continue
        merged = any(
            _git(path, runner, "merge-base", "--is-ancestor", tip, sha).ok
            for sha in sorted(default_shas & present)
        )
        pr_state = None
        for repo in gh_repos if gh is not None else []:
            assert gh is not None
            pr = pick_pr(gh.prs(repo, name, now, tip), tip)
            if pr is None or not pr.get("state"):
                continue
            pr_state = str(pr["state"])
            head = str(pr.get("headRefOid") or "")
            # A PR head that is not local (e.g. someone updated the branch
            # on GitHub) may still contain the local tip: ask GitHub.
            if (
                head
                and head not in present
                and not covered(tip, present)
                and gh.contains(repo, tip, head)
            ):
                verified.add(tip)
            break
        branches.append(Branch(name, tip, name in remote_heads, merged, pr_state))

    unpushed: int | None = None
    starts = history_starts(path, runner)
    if starts is not None and not (remotes and not reached):
        # Every local ref counts (branches, tags, notes, all stash entries,
        # ...); the rescue bundle uses exactly the same revisions.
        revs = revs_of(starts, present | verified)
        count = _git(path, runner, "rev-list", "--count", "--stdin", stdin=revs)
        tags = unpushed_tags(path, runner, starts, remote_shas)
        if not revs:
            unpushed = 0
        elif count.ok and count.out.strip().isdigit() and tags is not None:
            unpushed = int(count.out.strip()) + tags
    facts = GitFacts(dirty, stash, unpushed, branches, worktrees, main_repo)
    facts.pushed = sorted(present | verified)
    facts.bundle_secrets = committed_secrets(path, runner, facts.pushed)
    return facts


def history_starts(path: Path, runner: Runner) -> list[tuple[str, str]] | None:
    """Return the start points of this clone's own history.

    The single source for the unpushed count, the rescue bundle, the secret
    scan and the fingerprint: every local ref except remote-tracking ones
    (branches, tags, notes, ``refs/stash``, ``refs/rescued/*``, ...), every
    older stash entry (named ``stash@{<n>}``, as they live only in the
    stash reflog; not a ref name, so no clash with real refs) and HEAD
    (never other worktrees' HEADs). Other reflog entries are not included.

    Args:
        path: Clone directory.
        runner: Command runner.

    Returns:
        ``(name, sha)`` pairs, or None if git could not tell.
    """
    refs = _git(path, runner, "for-each-ref", "--format=%(refname) %(objectname)")
    if not refs.ok:
        return None
    starts = [
        (ref, sha)
        for ref, _, sha in (line.partition(" ") for line in refs.out.splitlines())
        if not ref.startswith("refs/remotes/")
    ]
    if any(ref == "refs/stash" for ref, _ in starts):
        log = _git(path, runner, "log", "-g", "--format=%H", "refs/stash")
        if not log.ok:
            return None
        starts += [(f"stash@{{{n}}}", sha) for n, sha in enumerate(log.out.split()[1:], 1)]
    head = _git(path, runner, "rev-parse", "-q", "--verify", "HEAD")
    if head.ok:
        starts.append(("HEAD", head.out.strip()))
    return starts


def revs_of(starts: list[tuple[str, str]], pushed: Iterable[str]) -> str:
    """Render start points and exclusions as ``--stdin`` input.

    Args:
        starts: Result of :func:`history_starts`.
        pushed: Local commits verified on a remote (ls-remote or gh).

    Returns:
        The lines; "" if there is no start point.
    """
    if not starts:
        return ""
    return "".join(f"{sha}\n" for _, sha in starts) + "".join(f"^{s}\n" for s in sorted(pushed))


def history_revs(path: Path, runner: Runner, pushed: Iterable[str]) -> str | None:
    """Return this clone's own history minus what is verified pushed.

    Args:
        path: Clone directory.
        runner: Command runner.
        pushed: Local commits verified on a remote (ls-remote or gh).

    Returns:
        Lines for ``git rev-list --stdin``; "" if there is no start point,
        None if git could not tell.
    """
    starts = history_starts(path, runner)
    return None if starts is None else revs_of(starts, pushed)


def unpushed_tags(
    path: Path, runner: Runner, starts: list[tuple[str, str]], remote: set[str]
) -> int | None:
    """Count tag objects among the start points that no remote lists.

    An annotated tag on a pushed commit adds no unpushed commit, but the
    tag object itself exists only here.

    Args:
        path: Clone directory.
        runner: Command runner.
        starts: Result of :func:`history_starts`.
        remote: Every SHA the remotes advertise (``git ls-remote``).

    Returns:
        The count, or None if git could not tell.
    """
    shas = sorted({sha for _, sha in starts} - remote)
    if not shas:
        return 0
    check = _git(path, runner, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                 stdin="".join(f"{s}\n" for s in shas))  # fmt: skip
    if not check.ok:
        return None
    return sum(1 for line in check.out.splitlines() if line.endswith(" tag"))


def committed_secrets(path: Path, runner: Runner, pushed: Iterable[str]) -> list[str] | None:
    """List secret-like paths in the history a rescue bundle would carry.

    A clone with any is neither rescued nor deleted.

    Args:
        path: Clone directory.
        runner: Command runner.
        pushed: The exclusion set of :func:`history_revs`.

    Returns:
        Sorted secret-like paths, or None if git could not tell.
    """
    revs = history_revs(path, runner, pushed)
    if not revs:
        return None if revs is None else []
    objects = _git(path, runner, "rev-list", "--objects", "--stdin", stdin=revs)
    if not objects.ok:
        return None
    paths = {line.partition(" ")[2] for line in objects.out.splitlines()}
    return sorted(p for p in paths if p and is_secret(p))


def list_rescues(rescue_dir: Path) -> list[RescueInfo]:
    """List the existing rescue directories.

    Args:
        rescue_dir: The ``.rescue`` directory.

    Returns:
        Rescue records; the manifest gives clone and time. Without a
        manifest (an interrupted rescue) the directory name and mtime do,
        and the record is marked incomplete.
    """
    found: list[RescueInfo] = []
    if not rescue_dir.is_dir():
        return found
    for item in sorted(rescue_dir.iterdir()):
        if not item.is_dir() or item.is_symlink():
            continue
        complete = True
        try:
            manifest = json.loads((item / "manifest.json").read_text())
            clone, created = str(manifest["clone"]), float(manifest["created"])
            print_ = str(manifest.get("fingerprint", ""))
        except (OSError, ValueError, KeyError, TypeError):
            clone, created = RESCUE_SUFFIX.sub("", item.name), item.stat().st_mtime
            complete, print_ = False, ""
        found.append(RescueInfo(str(item), clone, created, du(item), complete, print_))
    return found


def list_trash(trash: Path) -> list[tuple[str, float]]:
    """List leftovers of interrupted deletions.

    Args:
        trash: The ``.trash`` directory.

    Returns:
        ``(path, time of the deletion)`` pairs; the time comes from the
        ``-<ns>`` suffix (a rename keeps the dir's mtime), and entries
        without one get +inf, so they are never pruned.
    """
    if not trash.is_dir():
        return []
    return [(str(p), trash_time(p.name)) for p in sorted(trash.iterdir())]


def trash_time(name: str) -> float:
    """Read the deletion time from a trash entry name.

    Args:
        name: ``<clone>-<ns>`` (optionally followed by ``-kept``).

    Returns:
        Seconds since the epoch, or +inf if the name has no time.
    """
    stem = name.removesuffix("-kept")
    _, _, ns = stem.rpartition("-")
    return int(ns) / 1e9 if ns.isdigit() else float("inf")


def labelled_docker(runner: Runner = run) -> list[DockerResource]:
    """List docker containers and networks labelled ``agent-ledger``.

    Args:
        runner: Command runner.

    Returns:
        Labelled resources; empty if docker is unavailable.
    """
    label = config.LEDGER_LABEL
    found: list[DockerResource] = []
    queries = (
        ("docker-container", ["docker", "ps", "-a"], "{{.Names}}"),
        ("docker-network", ["docker", "network", "ls"], "{{.Name}}"),
    )
    for kind, cmd, name_fmt in queries:
        fmt = f'{name_fmt}\t{{{{.Label "{label}"}}}}'
        res = runner(
            [*cmd, "--filter", f"label={label}", "--format", fmt], timeout=config.TIMEOUT_DOCKER
        )
        if not res.ok:
            continue
        for line in res.out.splitlines():
            name, _, ledger_id = line.partition("\t")
            if name and ledger_id:
                found.append(DockerResource(kind, name, ledger_id))
    return found


def probe_online(host: str = "github.com", port: int = 443) -> bool:
    """Tell whether GitHub is reachable.

    Args:
        host: Host to connect to.
        port: TCP port.

    Returns:
        True if a TCP connection succeeds within the probe timeout.
    """
    try:
        with socket.create_connection((host, port), timeout=config.TIMEOUT_PROBE):
            return True
    except OSError:
        return False


def clone_facts(
    path: Path,
    entry: Entry | None,
    users: set[int],
    rescues: list[RescueInfo],
    runner: Runner,
    gh: GhCache | None,
    now: float,
    fetch: bool,
    online: bool,
) -> CloneFacts:
    """Collect the facts of one clone directory.

    Args:
        path: Clone directory.
        entry: Its ledger entry, if any.
        users: Pids using it (0 = a container mount).
        rescues: Existing rescues of all clones.
        runner: Command runner.
        gh: gh cache, or None.
        now: Current time.
        fetch: Fetch before checking reachability.
        online: Whether remotes can be reached.

    Returns:
        The facts.
    """
    gitdir = git_dir_of(path)
    if gitdir is None:
        kind = "nogit"
    elif (path / ".git").is_file():
        kind = "worktree"
    else:
        kind = "git"
    tree = scan_tree(path, kind != "nogit")
    size, git_size, raw_outputs = tree.size, tree.git_size, tree.outputs
    newest, nested, keep, secret_like = activity(path, tree, runner)
    in_use_reason = ""
    if users:
        pids = sorted(users)
        in_use_reason = "pids " + ",".join(str(p) for p in pids[:5])
    elif entry is not None and entry.state == "running" and now - newest < config.RUNNING_WINDOW:
        in_use_reason = f"ledger running, changed within {config.RUNNING_WINDOW / config.HOUR:g}h"
    git = None
    if gitdir is not None and not in_use_reason:
        git = git_facts(path, gitdir, runner, gh, now, fetch, online)
        git.fingerprint = fingerprint(path, runner)
        git.secrets = unsaved_secrets(path, secret_like, runner)
    outputs = [
        BuildOutput(out_path, du(out_path), mtime, keep.get(out_path, ""))
        for out_path, mtime in raw_outputs
    ]
    size += sum(o.size for o in outputs)
    current = git.fingerprint if git else ""
    mine = [
        r.created
        for r in rescues
        if r.clone == path.name and r.complete and (not r.fingerprint or r.fingerprint == current)
    ]
    return CloneFacts(
        name=path.name,
        path=str(path),
        kind=kind,
        in_use=bool(in_use_reason),
        in_use_reason=in_use_reason,
        last_change=newest,
        size=size,
        git_size=git_size,
        build_outputs=outputs,
        git=git,
        ledger=entry,
        rescued_at=max(mine) if mine else None,
        nested=nested,
    )


def build_output_guards(path: Path, candidates: list[str], runner: Runner) -> dict[str, str]:
    """Find build output dirs of a git clone that must be kept.

    A dir is kept if git tracks files inside it, if git cannot tell (then
    all are kept), or if its name is ambiguous (``build``, ``site``, ...)
    and git does not ignore it, as it may then hold new source files.

    Args:
        path: Clone directory.
        candidates: Absolute paths of build output dirs.
        runner: Command runner.

    Returns:
        ``{path: reason}`` for the dirs to keep.
    """
    if not candidates:
        return {}
    tracked = tracked_dirs(path, runner)
    if tracked is None:
        return dict.fromkeys(candidates, "git ls-files failed: build output kept")
    rels = {c: os.path.relpath(c, path) for c in candidates}
    check = _git(path, runner, "check-ignore", "-z", "--stdin",
                 stdin="".join(f"{r}/\0" for r in rels.values()))  # fmt: skip
    ignored = {r.rstrip("/") for r in check.out.split("\0") if r}
    keep: dict[str, str] = {}
    for cand, rel in rels.items():
        if rel in tracked:
            keep[cand] = "build output holds tracked files"
        elif rel not in ignored and not is_build_output(Path(cand).name, git=False):
            keep[cand] = "build output not ignored by git (may hold source)"
    return keep


def fingerprint(path: Path, runner: Runner) -> str:
    """Hash the start points of a clone (local refs, every stash entry, HEAD).

    The executor compares it right before a deletion, so a commit, stash or
    checkout after planning cancels the deletion.

    Args:
        path: Clone directory.
        runner: Command runner.

    Returns:
        A hex digest ("" if git failed).
    """
    starts = history_starts(path, runner)
    if starts is None:
        return ""
    lines = [f"{name} {sha}" for name, sha in starts]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def activity(
    path: Path, tree: Tree, runner: Runner
) -> tuple[float, bool, dict[str, str], list[str]]:
    """Combine a walk with git activity and the build output guards.

    Build output dirs that must be kept (tracked or not ignored) may hold
    real work, so their mtimes and nested repositories count as well.

    Args:
        path: Clone directory.
        tree: Result of :func:`scan_tree` for the clone.
        runner: Command runner.

    Returns:
        ``(last change, nested repositories found, {kept dir: reason},
        secret-like files including those in kept dirs)``.
    """
    holding = dict.fromkeys(tree.nested_outputs, "build output holds a git repository")
    gitdir = git_dir_of(path)
    if gitdir is None:
        return tree.newest, tree.nested, holding, list(tree.secrets)
    newest = max(tree.newest, git_activity(gitdir))
    nested = tree.nested
    keep = {**build_output_guards(path, [p for p, _ in tree.outputs], runner), **holding}
    secrets = list(tree.secrets)
    for kept in keep:
        sub = scan_tree(Path(kept), True)
        nested = nested or sub.nested or os.path.lexists(os.path.join(kept, ".git"))
        newest = max(newest, sub.newest, *(m for _, m in sub.outputs))
        secrets += sub.secrets
    return newest, nested, keep, secrets


def unsaved_secrets(path: Path, candidates: list[str], runner: Runner) -> list[str] | None:
    """Keep the secret-like files that deleting the clone would lose.

    A file that git tracks unchanged is safe (it is in the commits); every
    other one (untracked, ignored or modified) exists only here.

    Args:
        path: Clone directory.
        candidates: Absolute paths of secret-like files.
        runner: Command runner.

    Returns:
        Paths relative to the clone, or None if git could not tell.
    """
    rels = sorted(os.path.relpath(c, path) for c in candidates)
    if not rels:
        return []
    tracked = _git(path, runner, "ls-files", "-z", "--", *rels)
    if not tracked.ok:
        return None
    changed = _git(path, runner, "diff-index", "--name-only", "-z", "HEAD", "--", *rels)
    modified = set(changed.out.split("\0")) if changed.ok else set(rels)
    clean = set(tracked.out.split("\0")) - modified
    return [r for r in rels if r not in clean]


def secret_files(path: Path, runner: Runner) -> list[str] | None:
    """List the secret-like files a deletion of the clone would lose.

    Args:
        path: Clone directory.
        runner: Command runner.

    Returns:
        Paths relative to the clone, or None if git could not tell.
    """
    return unsaved_secrets(path, activity(path, scan_tree(path, True), runner)[3], runner)


def last_change(path: Path, runner: Runner) -> float:
    """Return the activity time of a clone as the rules see it.

    Args:
        path: Clone directory.
        runner: Command runner.

    Returns:
        Newest change outside disposable build outputs, including git
        activity files and kept build output dirs.
    """
    tree = scan_tree(path, git_dir_of(path) is not None)
    return activity(path, tree, runner)[0]


def read_state(cfg: Config) -> dict[str, Any]:
    """Read the sweeper's own state file.

    Args:
        cfg: Run configuration.

    Returns:
        The state (e.g. ``last_notify``); empty if missing.
    """
    try:
        data: dict[str, Any] = json.loads((cfg.ledger_dir / "state.json").read_text())
    except (OSError, ValueError):
        data = {}
    return data


def collect(
    cfg: Config,
    now: float | None = None,
    proc_scan: ProcScan | None = None,
    runner: Runner = run,
    online: bool | None = None,
    disk_pct: float | None = None,
) -> tuple[World, GhCache]:
    """Collect all facts of one sweep.

    Args:
        cfg: Run configuration.
        now: Time of the sweep; defaults to the current time.
        proc_scan: Process scanner; defaults to :func:`scan_proc` plus
            docker mounts.
        runner: Command runner.
        online: Override the GitHub reachability probe.
        disk_pct: Override the disk usage.

    Returns:
        The world and the gh cache (to save afterwards).
    """
    ts = time.time() if now is None else now
    root = cfg.root
    ledger = Ledger(root)
    entries = ledger.load_all()
    is_online = probe_online() if online is None else online
    gh = GhCache(cfg.gh_cache, runner, enabled=is_online)
    pairs = list(proc_scan()) if proc_scan else [*scan_proc(), *docker_mounts(runner)]
    users = users_by_clone(root, pairs)
    rescues = list_rescues(cfg.rescue_dir)
    dirs = sorted(
        p
        for p in root.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    )

    def one(path: Path) -> CloneFacts:
        return clone_facts(
            path,
            entries.get(path.name),
            users.get(path.name, set()),
            rescues,
            runner,
            gh,
            ts,
            cfg.fetch and not cfg.dry_run,
            is_online,
        )

    with ThreadPoolExecutor(max_workers=config.WORKERS) as pool:
        clones = list(pool.map(one, dirs))
    present = {str(p) for p in dirs}
    missing = frozenset(
        e.clone_path
        for e in entries.values()
        if e.clone_path and e.clone_path not in present and not Path(e.clone_path).exists()
    )
    world = World(
        now=ts,
        clones=clones,
        entries=entries,
        docker=labelled_docker(runner),
        rescues=rescues,
        disk_pct=disk_percent(cfg.disk_path) if disk_pct is None else disk_pct,
        last_notify=read_state(cfg).get("last_notify"),
        notify=cfg.notify,
        missing_paths=missing,
        unreadable=frozenset(ledger.unreadable),
        trash=list_trash(cfg.root / ".trash"),
    )
    return world, gh
