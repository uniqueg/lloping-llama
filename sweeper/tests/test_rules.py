"""Tests of the pure decision logic in :mod:`agent_sweeper.rules`."""

from __future__ import annotations

from typing import Any

import pytest

from agent_sweeper import config, rules
from agent_sweeper.ledger import Entry, Resource
from agent_sweeper.rules import (
    Action,
    Branch,
    BuildOutput,
    CloneFacts,
    DockerResource,
    GitFacts,
    RescueInfo,
    World,
)

NOW = 1_000_000_000.0
H = config.HOUR
D = config.DAY


def safe_git(**kw: Any) -> GitFacts:
    """Return git facts of a clean, fully pushed clone.

    Args:
        **kw: Overrides.

    Returns:
        Git facts.
    """
    base: dict[str, Any] = {"dirty": False, "stash": False, "unpushed": 0}
    base.update(kw)
    return GitFacts(**base)


def clone(name: str = "c", idle: float = 3 * H, **kw: Any) -> CloneFacts:
    """Return clone facts.

    Args:
        name: Clone name.
        idle: Seconds since the last change.
        **kw: Overrides.

    Returns:
        Clone facts.
    """
    base: dict[str, Any] = {
        "name": name,
        "path": f"/r/{name}",
        "kind": "git",
        "last_change": NOW - idle,
        "size": 100,
        "git_size": 10,
        "git": safe_git(),
    }
    base.update(kw)
    return CloneFacts(**base)


def entry(name: str = "c", state: str = "running", **kw: Any) -> Entry:
    """Return a ledger entry.

    Args:
        name: Clone name.
        state: Ledger state.
        **kw: Overrides.

    Returns:
        The entry.
    """
    base: dict[str, Any] = {
        "id": name,
        "clone": name,
        "clone_path": f"/r/{name}",
        "created": NOW - 2 * D,
        "updated": NOW - 2 * D,
        "state": state,
    }
    base.update(kw)
    return Entry(**base)


def kinds(actions: list[Action]) -> list[str]:
    """Return the kinds of non-skip actions.

    Args:
        actions: Actions.

    Returns:
        Their kinds in order.
    """
    return [a.kind for a in actions if a.kind != "skip"]


def world(*clones: CloneFacts, **kw: Any) -> World:
    """Return a world around some clones.

    Args:
        *clones: Clone facts.
        **kw: Overrides.

    Returns:
        The world.
    """
    return World(now=NOW, clones=list(clones), **kw)


class TestGitFacts:
    """Safety of git facts."""

    def test_safe(self) -> None:
        """A clean, pushed clone without stash is safe."""
        assert safe_git().safe
        assert safe_git().unsafe_reason() == ""

    @pytest.mark.parametrize(
        ("kw", "reason"),
        [
            ({"dirty": True}, "dirty"),
            ({"dirty": None}, "status-unknown"),
            ({"stash": True}, "stash"),
            ({"unpushed": 3}, "unpushed=3"),
            ({"unpushed": None}, "push-unverified"),
        ],
    )
    def test_unsafe(self, kw: dict[str, Any], reason: str) -> None:
        """Each kind of unsafe work is named.

        Args:
            kw: Overrides.
            reason: Expected reason.
        """
        facts = safe_git(**kw)
        assert not facts.safe
        assert facts.unsafe_reason() == reason


class TestWorkDone:
    """When the work of a clone counts as finished."""

    def test_ledger_merged(self) -> None:
        """The ledger state merged settles it."""
        assert rules.work_done(clone(ledger=entry(state="merged")))[0]

    def test_no_git_facts(self) -> None:
        """Without git facts the work is not done."""
        assert rules.work_done(clone(git=None)) == (False, "git facts unavailable")

    def test_no_work_branch(self) -> None:
        """A clone with only default branches is not done."""
        assert rules.work_done(clone()) == (False, "no work branch")

    @pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
    def test_final_pr(self, state: str) -> None:
        """Merged or closed PRs finish a branch.

        Args:
            state: PR state.
        """
        facts = clone(git=safe_git(work_branches=[Branch("b", "x", True, False, state)]))
        assert rules.work_done(facts)[0]

    def test_merged_and_deleted(self) -> None:
        """A branch merged into a default branch and deleted is done."""
        facts = clone(git=safe_git(work_branches=[Branch("b", "x", False, True)]))
        assert rules.work_done(facts)[0]

    def test_open(self) -> None:
        """An open PR or a branch still on the remote is not done."""
        branches = [Branch("a", "x", True, True, "OPEN"), Branch("b", "y", True, True)]
        done, why = rules.work_done(clone(git=safe_git(work_branches=branches)))
        assert not done
        assert why == "open: a:OPEN,b:no-pr"


class TestCloneVerdict:
    """Deletion and rescue of whole clones."""

    def test_pinned(self) -> None:
        """Pinned clones are kept."""
        assert rules.clone_verdict(clone(ledger=entry(pinned=True)), NOW)[0] == "keep"

    def test_no_git_facts(self) -> None:
        """Clones whose git facts failed are kept."""
        assert rules.clone_verdict(clone(git=None), NOW) == ("keep", "git facts unavailable")

    def test_nested(self) -> None:
        """Clones with nested repositories or submodules are kept."""
        facts = clone(nested=True, idle=30 * D, ledger=entry(state="merged"))
        assert rules.clone_verdict(facts, NOW) == ("keep", "has nested repositories or submodules")

    def test_linked_worktrees(self) -> None:
        """Main clones with live worktrees are kept."""
        facts = clone(git=safe_git(linked_worktrees=["/r/w"]), idle=30 * D)
        assert rules.clone_verdict(facts, NOW)[0] == "keep"

    def test_recent(self) -> None:
        """Recently changed clones are kept even when merged."""
        facts = clone(idle=1 * H, ledger=entry(state="merged"))
        assert rules.clone_verdict(facts, NOW) == ("keep", "changed 1.0h ago")

    @pytest.mark.parametrize(
        ("bundle", "verdict"), [(False, "delete"), (True, "rescue-and-delete")]
    )
    def test_safe_done(self, monkeypatch: pytest.MonkeyPatch, bundle: bool, verdict: str) -> None:
        """Safe, finished clones are bundled first only if configured.

        Args:
            monkeypatch: Pytest fixture.
            bundle: Value of ``BUNDLE_SAFE_CLONES``.
            verdict: Expected verdict.
        """
        monkeypatch.setattr(config, "BUNDLE_SAFE_CLONES", bundle)
        facts = clone(ledger=entry(state="merged"))
        assert rules.clone_verdict(facts, NOW)[0] == verdict

    def test_bundle_default_off(self) -> None:
        """Safe clones are fully on GitHub, so no bundle by default."""
        assert config.BUNDLE_SAFE_CLONES is False

    def test_safe_fresh_rescue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rescue newer than the last change makes another unnecessary.

        Args:
            monkeypatch: Pytest fixture.
        """
        monkeypatch.setattr(config, "BUNDLE_SAFE_CLONES", True)
        facts = clone(ledger=entry(state="merged"), rescued_at=NOW - H)
        assert rules.clone_verdict(facts, NOW)[0] == "delete"

    def test_safe_old_idle(self) -> None:
        """Safe clones untouched for 14 days go even with open work."""
        verdict, why = rules.clone_verdict(clone(idle=15 * D), NOW)
        assert verdict == "delete"
        assert "idle 15.0d" in why

    def test_safe_open_kept(self) -> None:
        """Safe clones with open work and recent activity are kept."""
        verdict, why = rules.clone_verdict(clone(idle=3 * D), NOW)
        assert verdict == "keep"
        assert why.startswith("pushed; no work branch")

    def test_unsafe_young(self) -> None:
        """Unsafe clones younger than 7 days are kept without rescue."""
        assert rules.clone_verdict(clone(git=safe_git(dirty=True)), NOW)[0] == "keep"

    def test_unsafe_rescue(self) -> None:
        """Unsafe clones untouched for 7 days get a rescue."""
        facts = clone(git=safe_git(unpushed=2), idle=8 * D)
        assert rules.clone_verdict(facts, NOW)[0] == "rescue"

    def test_unsafe_rescued_kept(self) -> None:
        """An up-to-date rescue is not repeated."""
        facts = clone(git=safe_git(stash=True), idle=8 * D, rescued_at=NOW - 7 * D)
        assert rules.clone_verdict(facts, NOW)[0] == "keep"

    def test_unsafe_old(self) -> None:
        """Unsafe clones untouched for 14 days are rescued and deleted."""
        facts = clone(git=safe_git(dirty=True), idle=15 * D, rescued_at=NOW - 20 * D)
        assert rules.clone_verdict(facts, NOW)[0] == "rescue-and-delete"

    @pytest.mark.parametrize(
        ("idle_days", "rescued", "verdict"),
        [
            (6, None, "keep"),
            (8, None, "rescue"),
            (8, 7, "keep"),
            (15, None, "rescue-and-delete"),
            (15, 14, "delete"),
        ],
    )
    def test_offline_unverified(self, idle_days: int, rescued: int | None, verdict: str) -> None:
        """Unverifiable pushes (offline) follow the unsafe timeline.

        Args:
            idle_days: Days since the last change.
            rescued: Days since the newest rescue, None if none.
            verdict: Expected verdict.
        """
        facts = clone(
            git=safe_git(unpushed=None),
            idle=idle_days * D,
            rescued_at=None if rescued is None else NOW - rescued * D,
        )
        assert rules.clone_verdict(facts, NOW)[0] == verdict

    def test_unsafe_old_rescued(self) -> None:
        """With a fresh rescue, unsafe old clones are deleted directly."""
        facts = clone(git=safe_git(dirty=True), idle=15 * D, rescued_at=NOW - 14 * D)
        assert rules.clone_verdict(facts, NOW)[0] == "delete"


class TestPlanClone:
    """Per-clone plans."""

    def test_in_use(self) -> None:
        """In-use clones are only skipped."""
        facts = clone(in_use=True, in_use_reason="pids 1", idle=30 * D)
        assert [a.kind for a in rules.plan_clone(facts, world())] == ["skip"]

    def test_nogit_build_outputs(self) -> None:
        """Directories without git lose only idle build outputs."""
        out = BuildOutput("/r/c/node_modules", 50, NOW - 3 * H)
        facts = clone(kind="nogit", git=None, build_outputs=[out])
        actions = rules.plan_clone(facts, world())
        assert kinds(actions) == ["delete-build-output"]
        assert actions[0].reason == "no git repository: kept"

    @pytest.mark.parametrize("bundle", [False, True])
    def test_delete_with_ledger(self, monkeypatch: pytest.MonkeyPatch, bundle: bool) -> None:
        """Deletion frees the whole clone and marks the entry cleaned.

        Args:
            monkeypatch: Pytest fixture.
            bundle: Value of ``BUNDLE_SAFE_CLONES``.
        """
        monkeypatch.setattr(config, "BUNDLE_SAFE_CLONES", bundle)
        out = BuildOutput("/r/c/node_modules", 50, NOW - 3 * H)
        facts = clone(ledger=entry(state="merged"), build_outputs=[out])
        actions = rules.plan_clone(facts, world())
        expected = ["delete-clone", "set-state"]
        assert kinds(actions) == (["rescue", *expected] if bundle else expected)
        delete = actions[-2]
        assert delete.bytes == 100
        assert delete.extra == {
            "main_repo": None,
            "needs_rescue": bundle,
            "safe": True,
            "fingerprint": "",
            "last_change": NOW - 3 * H,
        }

    def test_delete_cleaned_entry(self) -> None:
        """An entry that is already cleaned is not set again."""
        facts = clone(idle=15 * D, ledger=entry(state="cleaned"), rescued_at=NOW)
        assert kinds(rules.plan_clone(facts, world())) == ["delete-clone"]

    def test_worktree_main_repo(self) -> None:
        """Deleting a linked worktree passes its main clone on."""
        facts = clone(kind="worktree", idle=15 * D, git=safe_git(main_repo="/r/m"))
        delete = [a for a in rules.plan_clone(facts, world()) if a.kind == "delete-clone"]
        assert delete[0].extra["main_repo"] == "/r/m"

    def test_build_output_rules(self) -> None:
        """Build outputs go when idle, are kept when fresh or tracked."""
        outs = [
            BuildOutput("/r/c/old", 1, NOW - 3 * H),
            BuildOutput("/r/c/new", 1, NOW - 10),
            BuildOutput("/r/c/dist", 1, NOW - 3 * H, keep="build output holds tracked files"),
        ]
        facts = clone(idle=3 * H, build_outputs=outs)
        actions = rules.plan_clone(facts, world())
        deleted = [a.target for a in actions if a.kind == "delete-build-output"]
        assert deleted == ["/r/c/old"]
        assert any(a.reason == "build output holds tracked files" for a in actions)

    def test_build_outputs_recent_clone(self) -> None:
        """A recent clone keeps its build outputs."""
        out = BuildOutput("/r/c/node_modules", 1, NOW - 3 * H)
        facts = clone(idle=10 * 60, build_outputs=[out])
        assert kinds(rules.plan_clone(facts, world())) == []

    @pytest.mark.parametrize("state", sorted(config.FINISHED_STATES))
    def test_build_outputs_finished_state(self, state: str) -> None:
        """Finished ledger states remove build outputs at once.

        Args:
            state: Ledger state.
        """
        out = BuildOutput("/r/c/node_modules", 1, NOW - 10)
        facts = clone(idle=10, build_outputs=[out], ledger=entry(state=state))
        actions = [a for a in rules.plan_clone(facts, world()) if a.kind == "delete-build-output"]
        assert actions[0].reason == f"ledger state {state}"

    def test_build_outputs_disk_pressure(self) -> None:
        """Disk pressure removes build outputs regardless of age."""
        out = BuildOutput("/r/c/node_modules", 1, NOW - 10)
        facts = clone(idle=10, build_outputs=[out])
        actions = rules.plan_clone(facts, world(disk_pct=86.0))
        assert [a.reason for a in actions if a.kind == "delete-build-output"] == ["disk at 86%"]


class TestPlan:
    """Whole-sweep plans."""

    def test_ledger_gone(self) -> None:
        """Entries whose clone is gone are marked cleaned after the grace."""
        entries = {
            "gone": entry("gone"),
            "fresh": entry("fresh", created=NOW - 60),
            "done": entry("done", state="cleaned"),
            "elsewhere": entry("elsewhere", clone_path="/other/x"),
        }
        missing = frozenset({"/r/gone", "/r/fresh", "/r/done"})
        actions = rules.plan(World(now=NOW, entries=entries, missing_paths=missing))
        assert [(a.kind, a.clone) for a in actions] == [
            ("skip", "fresh"),
            ("set-state", "gone"),
        ]

    def test_resources(self) -> None:
        """Recorded resources follow the clone's fate."""
        res = [Resource("docker-container", "x"), Resource("port", "1", removed=NOW)]
        entries = {
            "busy": entry("busy", resources=list(res)),
            "active": entry("active", updated=NOW, resources=list(res)),
            "idle": entry("idle", resources=list(res)),
            "merged": entry("merged", state="merged", updated=NOW, resources=list(res)),
            "gone": entry("gone", state="cleaned", resources=list(res)),
            "unknown": entry("unknown", clone_path="/elsewhere", resources=list(res)),
            "deleted": entry("deleted", state="merged", resources=list(res)),
            "nores": entry("nores", clone_path="/r/nores"),
        }
        clones = [
            clone("busy", in_use=True, in_use_reason="pids 1"),
            clone("active", idle=10, git=safe_git(dirty=True)),
            clone("idle", idle=2 * D, git=safe_git(dirty=True)),
            clone("merged", idle=10),
            clone("deleted", idle=3 * H, ledger=entries["deleted"]),
        ]
        w = World(
            now=NOW,
            clones=clones,
            entries=entries,
            missing_paths=frozenset({"/r/gone"}),
        )
        res_actions = {a.clone: a.reason for a in rules.plan(w) if a.target == "docker-container:x"}
        assert res_actions == {
            "busy": "resource: clone in use",
            "active": "resource: still active",
            "idle": "idle 2.0d",
            "merged": "ledger state merged",
            "gone": "clone gone",
            "deleted": "clone deleted",
        }
        unknown = [a for a in rules.plan(w) if a.clone == "unknown"]
        assert unknown[0].reason == "resource: clone path unknown"

    def test_labelled_docker(self) -> None:
        """Labelled docker resources need a ledger entry and follow it."""
        entries = {
            "gone": entry("gone", state="cleaned", resources=[Resource("docker-network", "n")]),
        }
        docker = [
            DockerResource("docker-network", "n", "gone"),
            DockerResource("docker-container", "c1", "gone"),
            DockerResource("docker-container", "c2", "nobody"),
        ]
        w = World(now=NOW, entries=entries, docker=docker, missing_paths=frozenset({"/r/gone"}))
        actions = rules.plan(w)
        pairs = [(a.kind, a.target) for a in actions]
        assert ("remove-resource", "docker-network:n") in pairs
        assert ("remove-resource", "docker-container:c1") in pairs
        assert pairs.count(("remove-resource", "docker-network:n")) == 1
        assert ("skip", "docker-container:c2") in pairs

    def test_labelled_docker_unknown_path(self) -> None:
        """Labelled resources of an entry whose clone is unknown are kept."""
        entries = {"x": entry("x", clone_path="/elsewhere/x")}
        docker = [DockerResource("docker-container", "c", "x")]
        actions = rules.plan(World(now=NOW, entries=entries, docker=docker))
        assert [(a.kind, a.reason) for a in actions] == [("skip", "resource: clone path unknown")]

    def test_rescue_pruning(self) -> None:
        """Rescues older than 90 days are pruned."""
        rescues = [
            RescueInfo("/r/.rescue/a", "a", NOW - 91 * D, 5),
            RescueInfo("/r/.rescue/b", "b", NOW - 10 * D, 5),
        ]
        actions = rules.plan(World(now=NOW, rescues=rescues))
        assert [(a.kind, a.clone, a.bytes) for a in actions] == [("prune-rescue", "a", 5)]

    @pytest.mark.parametrize(
        ("pct", "last", "expected"),
        [
            (93.0, None, True),
            (93.0, NOW - 2 * D, True),
            (93.0, NOW - H, False),
            (91.0, None, False),
        ],
    )
    def test_notify(self, pct: float, last: float | None, expected: bool) -> None:
        """A disk notification goes out at most once per interval.

        Args:
            pct: Disk usage.
            last: Time of the last notification.
            expected: Whether one is planned.
        """
        world = World(now=NOW, disk_pct=pct, last_notify=last, notify="notify-cmd")
        actions = rules.plan(world)
        assert ("notify" in kinds(actions)) is expected
        assert all(a.target == "notify-cmd" for a in actions if a.kind == "notify")

    def test_no_notify_program(self) -> None:
        """Without a notification program nothing is sent, however full the disk."""
        actions = rules.plan(World(now=NOW, disk_pct=99.0))
        assert "notify" not in kinds(actions)

    def test_age_format(self) -> None:
        """Durations format as hours or days."""
        assert rules._age(2 * H) == "2.0h"
        assert rules._age(2 * D) == "2.0d"


def test_unreadable_entry_keeps_clone() -> None:
    """A clone whose ledger entry is unreadable is left alone.

    Regression test: a broken entry fell back to "no entry", which dropped
    its pin and running-window protection.
    """
    c = clone(idle=30 * D)
    actions = rules.plan(world(c, unreadable=frozenset({"c"})))
    assert [(a.kind, a.reason) for a in actions if a.clone == "c"] == [
        ("skip", "ledger entry unreadable")
    ]


def test_trash_pruned_after_keep() -> None:
    """Trash leftovers are pruned once they are old enough."""
    trash = [("/r/.trash/old", NOW - 2 * H), ("/r/.trash/new", NOW)]
    actions = rules.plan(world(trash=trash))
    assert [a.target for a in actions if a.kind == "prune-trash"] == ["/r/.trash/old"]


@pytest.mark.parametrize("secrets", [[".env"], None])
def test_secret_like_files_keep_clone(secrets: list[str] | None) -> None:
    """Clones whose deletion would lose secret-like files are kept.

    Rescues never copy secrets, so an incomplete rescue must not lead to a
    deletion. An unsafe clone due for deletion still gets its rescue.

    Args:
        secrets: Unsaved secret-like files, or None if unknown.
    """
    safe = clone("s", idle=30 * D, git=safe_git(secrets=secrets))
    unsafe = clone("u", idle=30 * D, git=safe_git(unpushed=2, secrets=secrets))
    rescued = clone("r", idle=30 * D, git=safe_git(unpushed=2, secrets=secrets), rescued_at=NOW)
    actions = rules.plan(world(safe, unsafe, rescued))
    by_clone = {c: [(a.kind, a.reason) for a in actions if a.clone == c] for c in "sur"}
    reason = rules.SECRETS_REASON if secrets else rules.SECRETS_UNKNOWN
    assert by_clone["s"] == [("skip", reason)]
    assert [k for k, _ in by_clone["u"]] == ["rescue", "skip"]
    assert by_clone["r"] == [("skip", reason)]
    assert "delete-clone" not in kinds(actions)


def test_bundle_secrets_block_rescue() -> None:
    """Secrets in unpushed commits or the stash block rescue and deletion.

    Regression test: the bundle carried them although rescues must never
    hold secrets.
    """
    due = clone("d", idle=30 * D, git=safe_git(unpushed=2, bundle_secrets=["app.key"]))
    week = clone("w", idle=8 * D, git=safe_git(unpushed=2, bundle_secrets=None))
    safe = clone("s", idle=30 * D, git=safe_git(bundle_secrets=["app.key"]))
    actions = rules.plan(world(due, week, safe))
    assert [(a.kind, a.reason) for a in actions if a.clone == "d"] == [
        ("skip", rules.SECRETS_REASON)
    ]
    assert [(a.kind, a.reason) for a in actions if a.clone == "w"] == [
        ("skip", rules.SECRETS_UNKNOWN)
    ]
    assert kinds([a for a in actions if a.clone == "s"]) == ["delete-clone"]


def test_kept_trash_entries_reported() -> None:
    """Entries left after a failed delete are never pruned, only reported.

    Regression test: they got a prune-trash action on every run.
    """
    actions = rules.plan(world(trash=[(f"/r/.trash/c-1{rules.KEPT_SUFFIX}", NOW - 30 * D)]))
    assert [(a.kind, a.reason) for a in actions if a.target.startswith("/r/.trash")] == [
        ("skip", rules.KEPT_REASON)
    ]
