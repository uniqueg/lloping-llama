"""Carry out planned actions, with last-moment safety checks.

Every destructive step re-checks right before it runs: the target must lie
inside the clone root, the clone must still be unused, and a clone is only
deleted if its rescue (when required) was written and its tree did not get
dirty since the plan.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import tarfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_sweeper import config
from agent_sweeper.config import Config, is_build_output, is_secret
from agent_sweeper.facts import (
    Runner,
    cache_exempt,
    committed_secrets,
    fingerprint,
    history_starts,
    last_change,
    parse_status,
    revs_of,
    secret_files,
    tracked_dirs,
    trash_time,
)
from agent_sweeper.ledger import Ledger
from agent_sweeper.rules import KEPT_SUFFIX, SECRETS_REASON, SECRETS_UNKNOWN, Action
from agent_sweeper.util import Result, run, which, within

#: Callable that tells whether a path (or anything below it) is in use now.
InUse = Callable[[str], bool]
#: Suffix of trash entries that must never be pruned automatically.
KEPT = KEPT_SUFFIX
#: Rescue note when a clone has no unpushed history (no bundle written).
NOTHING_UNPUSHED = "nothing unpushed: no bundle needed"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})  # noqa: S104


@dataclass
class Outcome:
    """Result of one action.

    Attributes:
        action: The action.
        status: ``done``, ``failed``, ``skipped`` or ``planned`` (dry run).
        detail: Error or note.
    """

    action: Action
    status: str
    detail: str = ""

    def record(self) -> dict[str, Any]:
        """Return the JSON log line of this outcome.

        Returns:
            A JSON-serialisable dictionary.
        """
        a = self.action
        return {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "action": a.kind,
            "clone": a.clone,
            "target": a.target,
            "reason": a.reason,
            "bytes": a.bytes,
            "status": self.status,
            "detail": self.detail,
        }


USERINFO = re.compile(r"(\w+://)[^/@\s]+@")


def _count(path: Path, runner: Runner, revs: str) -> int:
    """Count the commits of some revisions.

    Args:
        path: Clone directory.
        runner: Command runner.
        revs: ``--stdin`` revisions.

    Returns:
        The count; -1 if git failed (treated as "something to keep").
    """
    res = runner(
        ["git", "--no-optional-locks", "-C", str(path), "rev-list", "--count", "--stdin"],
        timeout=config.TIMEOUT_GIT_LOCAL,
        stdin=revs,
    )
    return int(res.out.strip()) if res.ok and res.out.strip().isdigit() else -1


#: One refspec restores every start point of a bundle.
RESTORE_REFSPEC = "refs/sweeper/*:refs/rescued/*"


def bundle_ref(name: str) -> str:
    """Map a start point to its ref in a rescue bundle.

    Everything goes under ``refs/sweeper/``, so ``RESTORE_REFSPEC`` brings
    all of it back, HEAD included, and nothing clashes.

    Args:
        name: ``HEAD``, ``stash@{<n>}`` or a ref name.

    Returns:
        ``refs/sweeper/HEAD``, ``refs/sweeper/stash/<n>`` or
        ``refs/sweeper/<ref name>``.
    """
    if name.startswith("stash@{"):
        return f"refs/sweeper/stash/{name[len('stash@{') : -1]}"
    return f"refs/sweeper/{name}"


class RescueError(Exception):
    """A rescue cannot capture all uncommitted work."""


@dataclass
class Executor:
    """Run actions against the file system, git, docker and the ledger.

    Attributes:
        cfg: Run configuration.
        in_use: Re-check of clone use right before a destructive step.
        runner: Command runner.
        rescued: Clones whose rescue succeeded in this run.
        deleted: Clones deleted in this run; actions marked
            ``after_delete`` run only for these.
    """

    cfg: Config
    in_use: InUse
    runner: Runner = run
    rescued: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)

    def execute(self, actions: list[Action]) -> list[Outcome]:
        """Run the actions in order.

        Args:
            actions: Planned actions.

        Returns:
            One outcome per action.
        """
        outcomes = []
        for action in actions:
            if action.kind == "skip":
                outcomes.append(Outcome(action, "skipped"))
                continue
            after = action.extra.get("after_delete")
            if after is not None and after not in self.deleted:
                outcomes.append(Outcome(action, "skipped", "clone was not deleted"))
                continue
            try:
                handler = getattr(self, "do_" + action.kind.replace("-", "_"))
                status, detail = handler(action)
            except Exception as err:  # noqa: BLE001 - one failure must not stop the sweep
                status, detail = "failed", f"{type(err).__name__}: {err}"
            outcomes.append(Outcome(action, status, detail))
        return outcomes

    def _guard(self, action: Action) -> str | None:
        """Check that a path action stays inside the root and the clone is idle.

        Args:
            action: Action whose target is a path.

        Returns:
            Why the action must not run, or None if it may.
        """
        if not within(action.target, self.cfg.root):
            return "target outside clone root"
        if action.clone and self.in_use(str(self.cfg.root / action.clone)):
            return "clone in use now"
        return None

    def do_delete_build_output(self, action: Action) -> tuple[str, str]:
        """Remove a build output directory, keeping the parent's mtime.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        if why := self._guard(action):
            return "skipped", why
        target = Path(action.target)
        if not is_build_output(target.name):
            return "skipped", "not a build output name"
        parent = target.parent
        st = os.stat(parent)
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        os.utime(parent, ns=(st.st_atime_ns, st.st_mtime_ns))
        return "done", ""

    def _rescue_files(self, path: Path) -> tuple[list[str], list[str]]:
        """List uncommitted and untracked files worth keeping.

        Args:
            path: Clone directory.

        Returns:
            Relative paths to archive (without unambiguous caches and
            secrets), and the secret-like paths left out.

        Raises:
            RescueError: If git cannot list the files, or an untracked
                nested repository cannot be archived file by file.
        """
        # Plumbing on purpose: "git diff" refreshes and rewrites the index,
        # which would count as activity in the clone.
        changed = self.runner(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(path),
                "diff-index",
                "--name-only",
                "-z",
                "HEAD",
            ],
            timeout=config.TIMEOUT_GIT_LOCAL,
        )
        untracked = self.runner(
            ["git", "--no-optional-locks", "-C", str(path), "ls-files", "-o",
             "--exclude-standard", "-z"],
            timeout=config.TIMEOUT_GIT_LOCAL,
        )  # fmt: skip
        if not changed.ok:
            # No HEAD yet: every tracked file is uncommitted work.
            changed = self.runner(
                ["git", "--no-optional-locks", "-C", str(path), "ls-files", "-z"],
                timeout=config.TIMEOUT_GIT_LOCAL,
            )
        if not changed.ok or not untracked.ok:
            raise RescueError("could not list uncommitted files")
        tracked = tracked_dirs(path, self.runner)
        if tracked is None:
            raise RescueError("could not list tracked files")
        # Changes to tracked files always count; untracked files only
        # outside untracked caches (a tracked .tools/ holds real work).
        rels = {r for r in changed.out.split("\0") if r}
        rels |= {r for r in untracked.out.split("\0") if r and not cache_exempt(r, tracked)}
        keep: list[str] = []
        excluded: list[str] = []
        for rel in sorted(rels):
            if rel.endswith("/"):
                # git lists an untracked nested repository as "dir/".
                raise RescueError(f"untracked nested repository {rel} cannot be rescued")
            if is_secret(rel):
                excluded.append(rel)
            elif (path / rel).is_file() or (path / rel).is_symlink():
                keep.append(rel)
        return keep, excluded

    def _bundle(
        self, path: Path, dest: Path, starts: list[tuple[str, str]], pushed: list[str]
    ) -> Result:
        """Write ``repo.bundle`` with one named ref per start point.

        Older stash entries have no ref in the clone, and the clone must not
        be written to, so the refs are set in a throwaway bare repository
        that borrows the clone's objects (``objects/info/alternates``).

        Args:
            path: Clone directory.
            dest: Rescue directory.
            starts: Result of :func:`history_starts`.
            pushed: Exclusion set.

        Returns:
            The result of the last git step that ran.
        """
        work = dest / "work.git"
        try:
            step = self.runner(["git", "init", "-q", "--bare", str(work)],
                               timeout=config.TIMEOUT_GIT_LOCAL)  # fmt: skip
            common = self.runner(
                ["git", "--no-optional-locks", "-C", str(path), "rev-parse", "--git-common-dir"],
                timeout=config.TIMEOUT_GIT_LOCAL,
            )
            if not step.ok or not common.ok:
                return step if not step.ok else common
            objects = (path / common.out.strip()).resolve() / "objects"
            (work / "objects" / "info" / "alternates").write_text(f"{objects}\n")
            named = [(bundle_ref(n), s) for n, s in starts]
            step = self.runner(
                ["git", "-C", str(work), "update-ref", "--stdin"],
                timeout=config.TIMEOUT_GIT_LOCAL,
                stdin="".join(f"update {n} {s}\n" for n, s in named),
            )
            if not step.ok:
                return step
            names = [n for n, _ in named]
            return self.runner(
                ["git", "-C", str(work), "bundle", "create", str(dest / "repo.bundle"), "--stdin"],
                timeout=config.TIMEOUT_GIT_BUNDLE,
                stdin="".join(f"{n}\n" for n in names) + "".join(f"^{s}\n" for s in pushed),
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _bundle_incomplete(
        self, action: Action, path: Path, dest: Path, revs: str, pushed: list[str], empty: bool
    ) -> str:
        """Check that a bundle holds all unpushed work and can be restored.

        Args:
            action: The rescue action (``unpushed`` from planning).
            path: Clone directory.
            dest: Rescue directory.
            revs: The bundle's revisions.
            pushed: Its exclusion set.
            empty: Whether no bundle was written.

        Returns:
            Why the bundle is not enough, or "" if it is.
        """
        count = _count(path, self.runner, revs) if revs else 0
        if empty:
            planned = action.extra.get("unpushed") or 0
            return "no bundle but unpushed commits exist" if count or planned else ""
        header = (dest / "repo.bundle").read_bytes().split(b"\n\n", 1)[0].decode()
        prereqs = [line[1:].split()[0] for line in header.splitlines() if line.startswith("-")]
        if prereqs:
            # Every prerequisite must be reachable from a verified pushed commit.
            stdin = "".join(f"{p}\n" for p in prereqs) + "".join(f"^{s}\n" for s in pushed)
            outside = self.runner(
                ["git", "--no-optional-locks", "-C", str(path), "rev-list", "--stdin"],
                timeout=config.TIMEOUT_GIT_LOCAL,
                stdin=stdin,
            )
            if not outside.ok or outside.out.strip():
                return "bundle needs commits that are not verified on a remote"
        return ""

    def do_rescue(self, action: Action) -> tuple[str, str]:
        """Write a git bundle and a tarball of uncommitted files.

        The manifest is written last, so an incomplete rescue never counts.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        if why := self._guard(action):
            return "skipped", why
        path = Path(action.target)
        # The exclusion set from planning; without one, bundle everything
        # (no prerequisites, so always restorable).
        pushed: list[str] = list(action.extra.get("pushed") or [])
        leaked = committed_secrets(path, self.runner, pushed)
        if leaked != []:
            # The bundle would carry them; rescues never hold secrets.
            return "skipped", SECRETS_REASON if leaked else SECRETS_UNKNOWN
        now = time.time()
        stamp = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        base = self.cfg.rescue_dir / f"{action.clone}-{stamp}"
        dest, n = base, 0
        while dest.exists():
            n += 1
            dest = base.with_name(f"{base.name}-{n}")
        self.cfg.rescue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cfg.rescue_dir.chmod(0o700)
        dest.mkdir(mode=0o700)
        dest.chmod(0o700)
        try:
            return self._write_rescue(action, path, dest, now, pushed)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    def _write_rescue(
        self, action: Action, path: Path, dest: Path, now: float, pushed: list[str]
    ) -> tuple[str, str]:
        """Fill a new rescue directory; the caller removes it on errors.

        Args:
            action: The rescue action.
            path: Clone directory.
            dest: Empty rescue directory.
            now: Creation time.
            pushed: Exclusion set of the bundle (see :func:`history_revs`).

        Returns:
            Status and detail.
        """
        starts = history_starts(path, self.runner)
        if starts is None:
            shutil.rmtree(dest, ignore_errors=True)
            return "failed", "could not list refs to bundle"
        revs = revs_of(starts, pushed)
        note = NOTHING_UNPUSHED
        if revs:
            # Only this clone's unpushed history (see facts.history_starts).
            bundle = self._bundle(path, dest, starts, pushed)
            if bundle.ok:
                note = ""
            elif "empty bundle" not in bundle.err:
                shutil.rmtree(dest, ignore_errors=True)
                return "failed", "git bundle: " + bundle.err.strip()[:200]
        if why := self._bundle_incomplete(action, path, dest, revs, pushed, bool(note)):
            shutil.rmtree(dest, ignore_errors=True)
            return "failed", f"rescue incomplete: {why}"
        try:
            files, excluded = self._rescue_files(path)
        except RescueError as err:
            shutil.rmtree(dest, ignore_errors=True)
            return "failed", str(err)
        if files:
            tarball = dest / "uncommitted.tar.gz"
            tarball.touch(mode=0o600)
            with tarfile.open(tarball, "w:gz") as tar:
                for rel in files:
                    tar.add(path / rel, arcname=rel, recursive=False)
        status = self.runner(
            ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain=v1", "-b"],
            timeout=config.TIMEOUT_GIT_LOCAL,
        )
        remotes = self.runner(
            ["git", "--no-optional-locks", "-C", str(path), "remote", "-v"],
            timeout=config.TIMEOUT_GIT_LOCAL,
        )
        url = next((line.split()[1] for line in remotes.out.splitlines() if line.split()[1:]), "")
        url = USERINFO.sub(r"\1", url) or "<remote>"
        restore = [f"git clone {url} {action.clone}"]
        if not note:
            restore.append(
                f"git -C {action.clone} fetch {dest / 'repo.bundle'} '{RESTORE_REFSPEC}'"
            )
        if files:
            restore.append(f"tar -xzf {dest / 'uncommitted.tar.gz'} -C {action.clone}")
        manifest = {
            "clone": action.clone,
            "bundle": not note,
            "path": str(path),
            "created": now,
            "reason": action.reason,
            "files": files,
            "excluded_secrets": excluded,
            "fingerprint": fingerprint(path, self.runner),
            "status": status.out,
            "remotes": USERINFO.sub(r"\1", remotes.out),
            "restore": restore,
        }
        for item in dest.iterdir():
            item.chmod(0o600)
        manifest_file = dest / "manifest.json"
        manifest_file.touch(mode=0o600)
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        self.rescued.add(action.clone)
        return "done", note or str(dest)

    def do_delete_clone(self, action: Action) -> tuple[str, str]:
        """Delete a whole clone after the last checks.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        if why := self._guard(action):
            return "skipped", why
        if action.extra.get("needs_rescue") and action.clone not in self.rescued:
            return "skipped", "rescue missing"
        path = Path(action.target)
        status = self.runner(
            ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain=v1", "-z",
             "--untracked-files=normal"],
            timeout=config.TIMEOUT_GIT_LOCAL,
        )  # fmt: skip
        tracked = tracked_dirs(path, self.runner)
        if action.extra.get("safe") and (not status.ok or parse_status(status.out, tracked)):
            return "skipped", "tree changed since planning"
        planned = action.extra.get("fingerprint")
        if planned is not None and fingerprint(path, self.runner) != planned:
            return "skipped", "refs changed since planning"
        planned_change = action.extra.get("last_change")
        if planned_change is not None and last_change(path, self.runner) > planned_change:
            return "skipped", "clone changed since planning"
        if secret_files(path, self.runner) != []:
            return "skipped", SECRETS_REASON
        main = action.extra.get("main_repo")
        # Rename first, check again, then delete: a process that entered the
        # clone after the last check now shows up under the trash path.
        trash = self.cfg.root / ".trash"
        trash.mkdir(exist_ok=True)
        moved = trash / f"{path.name}-{time.time_ns()}"
        os.rename(path, moved)
        if self.in_use(str(moved)) or self.in_use(str(path)):
            try:
                os.rename(moved, path)
            except OSError:
                os.rename(moved, moved.with_name(moved.name + KEPT))
                return "failed", f"in use after rename; left in {moved}{KEPT}"
            return "skipped", "in use after rename"
        shutil.rmtree(moved)
        if main:
            self.runner(
                ["git", "-C", str(main), "worktree", "prune"], timeout=config.TIMEOUT_GIT_LOCAL
            )
        self.deleted.add(action.clone)
        return "done", ""

    def do_set_state(self, action: Action) -> tuple[str, str]:
        """Set a ledger entry's state.

        Args:
            action: The action (target is the state).

        Returns:
            Status and detail.
        """
        ledger = Ledger(self.cfg.root)
        with ledger.locked():
            entry = ledger.load(action.clone)
            if entry is None:
                return "skipped", "no ledger entry"
            entry.set_state(action.target)
            ledger.save(entry)
        return "done", ""

    def _docker_rm(self, cmd: list[str]) -> tuple[str, str]:
        """Run a docker removal; a missing object counts as removed.

        Args:
            cmd: Docker command.

        Returns:
            Status and detail.
        """
        res = self.runner(cmd, timeout=config.TIMEOUT_DOCKER)
        if res.ok or "No such" in res.err or "not found" in res.err:
            return "done", ""
        return "failed", res.err.strip()[:200]

    def _cluster(self, label: str, name: str, tool: list[str]) -> tuple[str, str]:
        """Delete a kind or k3d cluster, via docker if the tool is missing.

        Args:
            label: Docker label that names the cluster's node containers.
            name: Cluster name.
            tool: Cluster tool command that deletes the cluster.

        Returns:
            Status and detail.
        """
        if which(tool[0]):
            res = self.runner(tool, timeout=config.TIMEOUT_KUBECTL)
            return ("done", "") if res.ok else ("failed", res.err.strip()[:200])
        ids = self.runner(
            ["docker", "ps", "-aq", "--filter", f"label={label}={name}"],
            timeout=config.TIMEOUT_DOCKER,
        )
        if not ids.ok:
            return "failed", ids.err.strip()[:200]
        if not ids.out.split():
            return "done", "no node containers"
        return self._docker_rm(["docker", "rm", "-f", *ids.out.split()])

    def _namespace(self, name: str, kubeconfig: str | None) -> tuple[str, str]:
        """Delete a namespace, but only on a local (loopback) cluster.

        Args:
            name: Namespace.
            kubeconfig: Recorded kubeconfig file.

        Returns:
            Status and detail.
        """
        if not kubeconfig or not Path(kubeconfig).is_file():
            return "done", "released: kubeconfig gone (cluster removed with it)"
        servers = [
            line.split("server:", 1)[1].strip().strip("'\"")
            for line in Path(kubeconfig).read_text().splitlines()
            if line.strip().startswith("server:")
        ]
        text = Path(kubeconfig).read_text()
        context = re.search(r"^current-context:\s*['\"]?(\S+?)['\"]?\s*$", text, re.M)
        local_context = bool(context and context.group(1).startswith(("kind-", "k3d-")))
        hosts = [urllib.parse.urlsplit(s).hostname for s in servers]
        if not servers or not local_context or not all(h in LOOPBACK for h in hosts):
            return "done", "released only: not a local kind/k3d cluster"
        res = self.runner(
            ["kubectl", "--kubeconfig", kubeconfig, "delete", "namespace", name,
             "--wait=false", "--ignore-not-found"],
            timeout=config.TIMEOUT_KUBECTL,
        )  # fmt: skip
        return ("done", "") if res.ok else ("failed", res.err.strip()[:200])

    def do_remove_resource(self, action: Action) -> tuple[str, str]:
        """Remove a recorded or labelled resource and mark it in the ledger.

        Ports are only released in the ledger: the sweeper never kills
        processes.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        kind, name = action.extra["kind"], action.extra["name"]
        if action.clone and self.in_use(str(self.cfg.root / action.clone)):
            return "skipped", "clone in use now"
        if kind == "docker-container":
            status, detail = self._docker_rm(["docker", "rm", "-f", name])
        elif kind == "docker-network":
            status, detail = self._docker_rm(["docker", "network", "rm", name])
        elif kind == "kind-cluster":
            status, detail = self._cluster(
                "io.x-k8s.kind.cluster", name, ["kind", "delete", "cluster", "--name", name]
            )
        elif kind == "k3d-cluster":
            status, detail = self._cluster("k3d.cluster", name, ["k3d", "cluster", "delete", name])
        elif kind == "k8s-namespace":
            status, detail = self._namespace(name, action.extra.get("kubeconfig"))
        else:
            status, detail = "done", "released (processes are never killed)"
        if status == "done":
            ledger = Ledger(self.cfg.root)
            with ledger.locked():
                entry = ledger.load(action.clone)
                if entry is not None:
                    for res in entry.resources:
                        if res.kind == kind and res.name == name and res.removed is None:
                            res.removed = time.time()
                    ledger.save(entry)
        return status, detail

    def do_prune_rescue(self, action: Action) -> tuple[str, str]:
        """Delete an expired rescue.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        if not within(action.target, self.cfg.rescue_dir):
            return "skipped", "target outside rescue dir"
        shutil.rmtree(action.target)
        return "done", ""

    def do_prune_trash(self, action: Action) -> tuple[str, str]:
        """Delete a leftover of an interrupted deletion.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        target = action.target
        if not within(target, self.cfg.root / ".trash") or target.endswith(KEPT):
            return "skipped", "not a prunable trash entry"
        if time.time() - trash_time(Path(target).name) < config.TRASH_KEEP:
            return "skipped", "trash entry too young"
        if self.in_use(target):
            return "skipped", "trash entry in use"
        shutil.rmtree(target)
        return "done", ""

    def do_notify(self, action: Action) -> tuple[str, str]:
        """Send one disk notification with the command in the target.

        The target is split like a shell command line (no shell runs), so
        it may carry arguments, e.g. ``notify-send -u critical``.

        Args:
            action: The action.

        Returns:
            Status and detail.
        """
        res = self.runner(
            [*shlex.split(action.target), f"Agent sweeper: {action.reason}; clean up needed"],
            timeout=config.TIMEOUT_NOTIFY,
        )
        state_file = self.cfg.ledger_dir / "state.json"
        try:
            state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            state = {}
        state["last_notify"] = time.time()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state) + "\n")
        return ("done", "") if res.ok else ("failed", res.err.strip()[:200])
