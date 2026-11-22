"""Pure decision logic of the sweeper.

Collectors (:mod:`agent_sweeper.facts`) turn the machine state into the data
classes below; :func:`plan` turns those facts into a list of
:class:`Action` records without touching anything. The executor
(:mod:`agent_sweeper.execute`) then carries the actions out. Keeping the rules
free of side effects makes every decision testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_sweeper import config
from agent_sweeper.ledger import Entry, Resource

FINAL_PR_STATES = frozenset({"MERGED", "CLOSED"})


#: Skip reason for clones that would lose secret-like files.
SECRETS_REASON = "holds secret-like files; rescue would be incomplete"
#: Suffix of trash entries left after a failed deletion (never pruned).
KEPT_SUFFIX = "-kept"
#: Skip reason for those entries; they are listed for the user.
KEPT_REASON = "kept after failed delete, needs manual review"
#: Skip reason when git could not tell which secret-like files are unsaved.
SECRETS_UNKNOWN = "secret-like files could not be checked"


@dataclass
class BuildOutput:
    """A build output directory inside a clone.

    Attributes:
        path: Absolute path.
        size: Disk usage in bytes.
        mtime: Modification time of the directory itself.
        keep: Why it must be kept ("" if it may go), e.g. git tracks files
            inside it or does not ignore an ambiguously named dir.
    """

    path: str
    size: int
    mtime: float
    keep: str = ""


@dataclass
class Branch:
    """A local work branch (any branch that is not a default branch).

    Attributes:
        name: Branch name.
        tip: Commit SHA of the branch tip.
        on_remote: Whether a remote has a branch of this name now.
        merged: Whether the tip is reachable from a remote default branch.
        pr_state: ``MERGED``, ``CLOSED``, ``OPEN`` or None (no PR known).
    """

    name: str
    tip: str
    on_remote: bool = False
    merged: bool = False
    pr_state: str | None = None


@dataclass
class GitFacts:
    """What git says about a clone.

    Attributes:
        dirty: Uncommitted or untracked changes outside build outputs;
            None if unknown.
        stash: Whether a stash exists.
        unpushed: Commits of HEAD and local branches not reachable from any
            remote ref; None if that could not be verified (offline).
        work_branches: Local branches other than default branches.
        linked_worktrees: Live linked worktrees of this clone.
        main_repo: For a linked worktree, the path of its main clone.
        fingerprint: Hash of local refs and HEAD, re-checked before deletion.
        secrets: Secret-like files that exist only in this clone (never
            rescued, so they block deletion); None if unknown.
        pushed: Local commits verified on a remote; the unpushed count, the
            rescue bundle and the secret scan all exclude exactly these.
        bundle_secrets: Secret-like paths in unpushed commits or the stash
            (a bundle would copy them, so they block rescues); None if
            unknown.
    """

    dirty: bool | None
    stash: bool = False
    unpushed: int | None = None
    work_branches: list[Branch] = field(default_factory=list)
    linked_worktrees: list[str] = field(default_factory=list)
    main_repo: str | None = None
    fingerprint: str = ""
    secrets: list[str] | None = field(default_factory=list)
    bundle_secrets: list[str] | None = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        """Tell whether deleting the clone loses nothing.

        Returns:
            True if the tree is clean, there is no stash and every commit is
            on a remote.
        """
        return self.dirty is False and not self.stash and self.unpushed == 0

    def unsafe_reason(self) -> str:
        """Describe why the work is not safe.

        Returns:
            Comma-separated reasons, e.g. ``dirty,unpushed=3``.
        """
        reasons = []
        if self.dirty is None:
            reasons.append("status-unknown")
        elif self.dirty:
            reasons.append("dirty")
        if self.stash:
            reasons.append("stash")
        if self.unpushed is None:
            reasons.append("push-unverified")
        elif self.unpushed:
            reasons.append(f"unpushed={self.unpushed}")
        return ",".join(reasons)


@dataclass
class CloneFacts:
    """Everything the rules need to know about one clone directory.

    Attributes:
        name: Directory name.
        path: Absolute path.
        kind: ``git``, ``worktree`` (linked worktree) or ``nogit``.
        in_use: Whether a process or container uses the clone.
        in_use_reason: Why it counts as in use.
        last_change: Newest modification time outside build outputs.
        size: Disk usage in bytes.
        git_size: Disk usage of ``.git`` (what a bundle roughly costs).
        build_outputs: Build output directories found.
        git: Git facts; None for ``nogit`` or if git failed.
        ledger: Ledger entry, if any.
        rescued_at: Creation time of the newest complete rescue.
        nested: Whether it holds nested repositories or submodules, whose
            work git facts of the clone do not cover (then it is kept).
    """

    name: str
    path: str
    kind: str
    in_use: bool = False
    in_use_reason: str = ""
    last_change: float = 0.0
    size: int = 0
    git_size: int = 0
    build_outputs: list[BuildOutput] = field(default_factory=list)
    git: GitFacts | None = None
    ledger: Entry | None = None
    rescued_at: float | None = None
    nested: bool = False


@dataclass
class DockerResource:
    """A docker container or network labelled ``agent-ledger=<id>``.

    Attributes:
        kind: ``docker-container`` or ``docker-network``.
        name: Container or network name.
        ledger_id: Value of the label.
    """

    kind: str
    name: str
    ledger_id: str


@dataclass
class RescueInfo:
    """An existing rescue directory.

    Attributes:
        path: Absolute path.
        clone: Clone it was taken from.
        created: Creation time.
        size: Disk usage in bytes.
        complete: Whether its manifest exists (written last); only complete
            rescues protect work, incomplete ones are only pruned.
        fingerprint: Fingerprint of the clone's refs when it was rescued; a
            rescue protects the clone only while the refs are unchanged.
    """

    path: str
    clone: str
    created: float
    size: int = 0
    complete: bool = True
    fingerprint: str = ""


@dataclass
class World:
    """All facts of one sweep.

    Attributes:
        now: Time of the sweep.
        clones: Facts of every clone directory.
        entries: Every ledger entry by clone name.
        docker: Labelled docker resources.
        rescues: Existing rescues.
        disk_pct: Usage of the disk in percent.
        last_notify: Time of the last disk notification, if any.
        notify: Program that sends the disk notification; None sends none.
        missing_paths: Ledger clone paths that do not exist.
        unreadable: Clone names whose ledger entry cannot be read; their
            clones are left alone.
        trash: ``(path, mtime)`` of leftovers in ``<root>/.trash``.
    """

    now: float
    clones: list[CloneFacts] = field(default_factory=list)
    entries: dict[str, Entry] = field(default_factory=dict)
    docker: list[DockerResource] = field(default_factory=list)
    rescues: list[RescueInfo] = field(default_factory=list)
    disk_pct: float = 0.0
    last_notify: float | None = None
    notify: str | None = None
    missing_paths: frozenset[str] = frozenset()
    unreadable: frozenset[str] = frozenset()
    trash: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class Action:
    """One planned step, or a recorded decision to leave something alone.

    Attributes:
        kind: ``delete-build-output``, ``rescue``, ``delete-clone``,
            ``set-state``, ``remove-resource``, ``prune-rescue``, ``notify``
            or ``skip``.
        clone: Clone name the action belongs to ("" if none).
        target: Path or resource the action works on.
        reason: Rule that triggered it.
        bytes: Bytes it frees (estimate).
        extra: Details for the executor (e.g. resource kind).
    """

    kind: str
    clone: str
    target: str
    reason: str
    bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _age(seconds: float) -> str:
    """Format a duration for reasons.

    Args:
        seconds: Duration.

    Returns:
        E.g. ``3.2d`` or ``5.0h``.
    """
    if seconds >= config.DAY:
        return f"{seconds / config.DAY:.1f}d"
    return f"{seconds / config.HOUR:.1f}h"


def work_done(clone: CloneFacts) -> tuple[bool, str]:
    """Tell whether the clone's work is finished (merged or closed).

    Args:
        clone: Clone with git facts.

    Returns:
        Whether it is done, and why (or why not).
    """
    if clone.ledger is not None and clone.ledger.state == "merged":
        return True, "ledger state merged"
    if clone.git is None:
        return False, "git facts unavailable"
    branches = clone.git.work_branches
    if not branches:
        return False, "no work branch"
    open_branches = []
    for branch in branches:
        if branch.pr_state in FINAL_PR_STATES:
            continue
        if branch.merged and not branch.on_remote:
            continue
        open_branches.append(f"{branch.name}:{branch.pr_state or 'no-pr'}")
    if open_branches:
        return False, "open: " + ",".join(open_branches)
    return True, "all PRs merged/closed or branches merged and deleted"


def clone_verdict(clone: CloneFacts, now: float) -> tuple[str, str]:
    """Decide what happens to a whole git clone that is not in use.

    Args:
        clone: Clone of kind ``git`` or ``worktree``.
        now: Time of the sweep.

    Returns:
        ``(verdict, reason)``; the verdict is ``delete``,
        ``rescue-and-delete``, ``rescue`` or ``keep``.
    """
    idle = now - clone.last_change
    if clone.ledger is not None and clone.ledger.pinned:
        return "keep", "pinned in ledger"
    if clone.git is None:
        return "keep", "git facts unavailable"
    if clone.nested:
        return "keep", "has nested repositories or submodules"
    if clone.git.linked_worktrees:
        return "keep", f"has {len(clone.git.linked_worktrees)} linked worktree(s)"
    if idle < config.CLONE_MIN_IDLE:
        return "keep", f"changed {_age(idle)} ago"
    fresh_rescue = clone.rescued_at is not None and clone.rescued_at >= clone.last_change
    if clone.git.safe:
        done, why = work_done(clone)
        if done or idle >= config.DELETE_AFTER:
            why = why if done else f"pushed, idle {_age(idle)}"
            if config.BUNDLE_SAFE_CLONES and not fresh_rescue:
                return "rescue-and-delete", f"safe; {why}"
            return "delete", f"safe; {why}"
        return "keep", f"pushed; {why}; idle {_age(idle)}"
    unsafe = clone.git.unsafe_reason()
    if idle >= config.DELETE_AFTER:
        verdict = "delete" if fresh_rescue else "rescue-and-delete"
        return verdict, f"unsafe ({unsafe}); idle {_age(idle)}"
    if idle >= config.RESCUE_AFTER and not fresh_rescue:
        return "rescue", f"unsafe ({unsafe}); idle {_age(idle)}"
    return "keep", f"unsafe ({unsafe}); idle {_age(idle)}"


def rescue_extra(clone: CloneFacts) -> dict[str, Any]:
    """Return what the executor needs to build and check a rescue.

    Args:
        clone: Clone facts.

    Returns:
        The exclusion set and the unpushed count from planning.
    """
    git = clone.git
    return {"pushed": git.pushed if git else [], "unpushed": git.unpushed if git else None}


def plan_clone(clone: CloneFacts, world: World) -> list[Action]:
    """Plan the actions for one clone directory.

    Args:
        clone: Clone facts.
        world: Facts of the whole sweep (time, disk usage).

    Returns:
        Actions, including ``skip`` records for things left alone.
    """
    if clone.in_use:
        return [Action("skip", clone.name, clone.path, f"in use: {clone.in_use_reason}")]
    if clone.name in world.unreadable:
        return [Action("skip", clone.name, clone.path, "ledger entry unreadable")]
    actions: list[Action] = []
    if clone.kind == "nogit":
        actions.append(Action("skip", clone.name, clone.path, "no git repository: kept"))
    else:
        verdict, reason = clone_verdict(clone, world.now)
        git = clone.git
        # Rescues never hold secrets: files left out would be lost by a
        # deletion, and secrets in commits would end up in the bundle.
        lose = verdict in ("delete", "rescue-and-delete") and git is not None and git.secrets != []
        leak = (
            verdict in ("rescue", "rescue-and-delete")
            and git is not None
            and git.bundle_secrets != []
        )
        if git is not None and (lose or leak):
            if not leak and verdict == "rescue-and-delete" and clone.rescued_at is None:
                actions.append(
                    Action(
                        "rescue",
                        clone.name,
                        clone.path,
                        reason,
                        clone.git_size,
                        rescue_extra(clone),
                    )
                )
            known = (lose and bool(git.secrets)) or (leak and bool(git.bundle_secrets))
            verdict, reason = "keep", SECRETS_REASON if known else SECRETS_UNKNOWN
            actions.append(Action("skip", clone.name, clone.path, reason))
        if verdict in ("rescue", "rescue-and-delete"):
            actions.append(
                Action(
                    "rescue", clone.name, clone.path, reason, clone.git_size, rescue_extra(clone)
                )
            )
        if verdict in ("delete", "rescue-and-delete"):
            main = clone.git.main_repo if clone.git else None
            actions.append(
                Action(
                    "delete-clone",
                    clone.name,
                    clone.path,
                    reason,
                    clone.size,
                    {
                        "main_repo": main,
                        "needs_rescue": verdict == "rescue-and-delete",
                        "safe": bool(clone.git and clone.git.safe),
                        "fingerprint": clone.git.fingerprint if clone.git else "",
                        "last_change": clone.last_change,
                    },
                )
            )
            if clone.ledger is not None and clone.ledger.state != "cleaned":
                after = {"after_delete": clone.name}
                actions.append(
                    Action("set-state", clone.name, "cleaned", "clone deleted", 0, after)
                )
            return actions
        if reason not in (SECRETS_REASON, SECRETS_UNKNOWN):
            actions.append(Action("skip", clone.name, clone.path, f"clone kept: {reason}"))
    state = clone.ledger.state if clone.ledger is not None else None
    pressure = world.disk_pct > config.DISK_SOFT
    for out in clone.build_outputs:
        if out.keep:
            actions.append(Action("skip", clone.name, out.path, out.keep))
            continue
        idle = world.now - max(clone.last_change, out.mtime)
        if state in config.FINISHED_STATES:
            why = f"ledger state {state}"
        elif pressure:
            why = f"disk at {world.disk_pct:.0f}%"
        elif idle >= config.BUILD_IDLE:
            why = f"idle {_age(idle)}"
        else:
            continue
        actions.append(Action("delete-build-output", clone.name, out.path, why, out.size))
    return actions


def _resource_actions(
    entry: Entry,
    resources: list[Resource],
    clone: CloneFacts | None,
    deleted: bool,
    world: World,
) -> list[Action]:
    """Plan the removal of one entry's resources.

    Args:
        entry: Ledger entry the resources belong to.
        resources: Resources not yet removed.
        clone: Facts of the entry's clone, None if the clone is gone.
        deleted: Whether this sweep deletes the clone.
        world: Facts of the whole sweep.

    Returns:
        ``remove-resource`` or ``skip`` actions.
    """
    actions = []
    for res in resources:
        target = f"{res.kind}:{res.name}"
        extra = {"kind": res.kind, "name": res.name, "kubeconfig": res.kubeconfig}
        if clone is not None and clone.in_use:
            actions.append(Action("skip", entry.clone, target, "resource: clone in use"))
            continue
        if deleted:
            why = "clone deleted"
            extra["after_delete"] = entry.clone
        elif clone is None:
            why = "clone gone"
        elif entry.state in ("cleaned", "merged"):
            why = f"ledger state {entry.state}"
        elif world.now - max(clone.last_change, entry.updated) >= config.RESOURCE_IDLE:
            why = f"idle {_age(world.now - max(clone.last_change, entry.updated))}"
        else:
            actions.append(Action("skip", entry.clone, target, "resource: still active"))
            continue
        actions.append(Action("remove-resource", entry.clone, target, why, 0, extra))
    return actions


def plan(world: World) -> list[Action]:
    """Plan a whole sweep.

    Args:
        world: Facts of the sweep.

    Returns:
        Actions in execution order: clones (rescue before deletion), ledger
        updates, resources, rescue pruning, notification.
    """
    actions: list[Action] = []
    by_name = {c.name: c for c in world.clones}
    deleted: set[str] = set()
    for clone in world.clones:
        clone_actions = plan_clone(clone, world)
        if any(a.kind == "delete-clone" for a in clone_actions):
            deleted.add(clone.name)
        actions.extend(clone_actions)

    for name, entry in sorted(world.entries.items()):
        if name in by_name or entry.state == "cleaned":
            continue
        if entry.clone_path not in world.missing_paths:
            continue
        if world.now - entry.created < config.LEDGER_GRACE:
            actions.append(Action("skip", name, entry.clone_path, "ledger: clone not yet created"))
            continue
        actions.append(Action("set-state", name, "cleaned", "ledger: clone directory gone"))

    recorded: set[tuple[str, str, str]] = set()
    pending: list[tuple[Entry, list[Resource]]] = []
    for entry in (e for _, e in sorted(world.entries.items())):
        recorded.update((entry.id, r.kind, r.name) for r in entry.resources)
        open_res = [r for r in entry.resources if r.removed is None]
        if open_res:
            pending.append((entry, open_res))
    by_id = {e.id: e for e in world.entries.values()}
    for dres in world.docker:
        if (dres.ledger_id, dres.kind, dres.name) in recorded:
            continue
        owner = by_id.get(dres.ledger_id)
        if owner is None:
            target = f"{dres.kind}:{dres.name}"
            actions.append(Action("skip", "", target, f"label {dres.ledger_id}: no ledger entry"))
            continue
        pending.append((owner, [Resource(dres.kind, dres.name)]))
    for entry, open_res in pending:
        found = by_name.get(entry.clone)
        if found is None and entry.clone_path not in world.missing_paths:
            reason = "resource: clone path unknown"
            actions.append(Action("skip", entry.clone, entry.clone_path, reason))
            continue
        gone = entry.clone in deleted
        actions.extend(_resource_actions(entry, open_res, found, gone, world))

    for path, mtime in world.trash:
        if path.endswith(KEPT_SUFFIX):
            actions.append(Action("skip", "", path, KEPT_REASON))
        elif world.now - mtime >= config.TRASH_KEEP:
            actions.append(Action("prune-trash", "", path, "leftover of an interrupted deletion"))

    for rescue in world.rescues:
        if world.now - rescue.created >= config.RESCUE_KEEP:
            age = _age(world.now - rescue.created)
            actions.append(
                Action("prune-rescue", rescue.clone, rescue.path, f"rescue {age} old", rescue.size)
            )

    if (
        world.notify
        and world.disk_pct > config.DISK_HARD
        and (world.last_notify is None or world.now - world.last_notify >= config.NOTIFY_INTERVAL)
    ):
        actions.append(Action("notify", "", world.notify, f"disk at {world.disk_pct:.0f}%"))
    return actions
