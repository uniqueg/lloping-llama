"""Tests of :mod:`agent_sweeper.execute`."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_sweeper import config, execute, facts
from agent_sweeper.config import Config
from agent_sweeper.execute import Executor, Outcome
from agent_sweeper.facts import fingerprint, last_change
from agent_sweeper.ledger import Entry, Ledger, Resource
from agent_sweeper.rules import SECRETS_REASON, SECRETS_UNKNOWN, Action
from agent_sweeper.util import Result

from .conftest import FakeRunner, git, pushed

OK = Result(0, "", "")
FAIL = Result(1, "", "boom")


def ex(root: Path, busy: set[str] | None = None, runner: Any = None) -> Executor:
    """Return an executor for a fixture root.

    Args:
        root: Clone root.
        busy: Clones that count as in use.
        runner: Command runner.

    Returns:
        The executor.
    """
    used = busy or set()
    return Executor(Config(root=root), lambda p: Path(p).name in used, runner or FakeRunner())


def one(executor: Executor, action: Action) -> Outcome:
    """Run one action.

    Args:
        executor: Executor.
        action: Action.

    Returns:
        Its outcome.
    """
    return executor.execute([action])[0]


def test_is_secret() -> None:
    """Env files and keys are secrets, the example file is not."""
    assert config.is_secret("a/.env")
    assert config.is_secret(".env.local")
    assert config.is_secret("k/id.pem")
    assert not config.is_secret(".env.example")
    assert not config.is_secret("src/env.py")
    for name in (
        ".kube/config",
        "a/.npmrc",
        "deploy/kubeconfig",
        "c.p12",
        "id_rsa.pub",
        ".envrc",
        "kubeconfig.yaml",
        "kind/kubeconfig.yml",
        ".config/gh/hosts.yml",
        "x/.config/gcloud/credentials.db",
        "secrets.yaml",
        "infra/prod.tfvars",
        "service-account.json",
        ".dev.vars",
        ".SSH/id",
        ".KUBE/config",
        "a/.Env",
        ".azure/accessTokens.json",
        ".gcloud/x",
        ".config/hub",
        "h/.config/hub/x",
        ".config/rclone/rclone.conf",
        "terraform.tfstate",
        "infra/terraform.tfstate.backup",
        "infra/terraform.tfvars.json",
        "prod.auto.tfvars.json",
        "kubeconfig",
        "kubeconfig-kind",
        "kubeconfig_dev.json",
        "secrets.yml",
        "secrets.json",
    ):
        assert config.is_secret(name), name
    for name in (
        "auth/token.py",
        "src/secrets_manager.py",
        "tests/test_credentials.py",
        ".config/x",
        ".config/ghostty/config",
        "gh/.config",
        "src/config/gh/x.py",
        "pkg/cluster/internal/kubeconfig/kubeconfig.go",
        "pkg/kubeconfig_test.go",
        "src/kubeconfig.py",
        "KubeConfigLoader.java",
        "charts/app/templates/secrets.yaml",
        "gh/hosts.yml",
        "docs/kubernetes.md",
    ):
        assert not config.is_secret(name), name


def test_outcome_record() -> None:
    """Outcomes log as JSON-ready records."""
    rec = Outcome(Action("skip", "c", "/t", "why", 3), "skipped").record()
    assert rec["action"] == "skip" and rec["bytes"] == 3 and "dry_run" not in rec


def test_execute_skip_and_crash(root: Path) -> None:
    """Skips pass through; a crashing handler fails only its action.

    Args:
        root: Clone root.
    """
    out = ex(root).execute(
        [
            Action("skip", "c", "/t", "why"),
            Action("prune-rescue", "c", str(root / ".rescue" / "missing"), "old"),
        ]
    )
    assert [o.status for o in out] == ["skipped", "failed"]
    assert "FileNotFoundError" in out[1].detail


def test_delete_build_output(root: Path) -> None:
    """Build outputs go, the parent's mtime stays; guards hold.

    Args:
        root: Clone root.
    """
    clone = root / "c"
    (clone / "node_modules" / "p").mkdir(parents=True)
    (clone / ".venv").symlink_to("/usr")
    (clone / "src").mkdir()
    os.utime(clone, (1000, 1000))
    executor = ex(root, busy={"busy"})
    act = Action("delete-build-output", "c", str(clone / "node_modules"), "idle")
    assert one(executor, act).status == "done"
    assert not (clone / "node_modules").exists()
    assert os.stat(clone).st_mtime == 1000
    assert one(executor, act).status == "done"  # idempotent
    link = Action("delete-build-output", "c", str(clone / ".venv"), "idle")
    assert one(executor, link).status == "done"
    assert Path("/usr").exists() and not (clone / ".venv").is_symlink()
    src = Action("delete-build-output", "c", str(clone / "src"), "idle")
    assert one(executor, src).detail == "not a build output name"
    out = Action("delete-build-output", "c", "/usr/node_modules", "idle")
    assert one(executor, out).detail == "target outside clone root"
    busy = Action("delete-build-output", "busy", str(root / "busy" / "dist"), "idle")
    assert one(executor, busy).detail == "clone in use now"


def test_rescue(make_clone: Callable[[str], Path], root: Path, remote: Path) -> None:
    """Rescues bundle unpushed history and tar changes without secrets or outputs.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        remote: Bare remote.
    """
    clone = make_clone("c")
    (clone / "README.md").write_text("changed\n")
    (clone / "new.txt").write_text("new")
    (clone / ".env").write_text("SECRET=1")
    (clone / ".kube").mkdir()
    (clone / ".kube" / "config").write_text("token")
    (clone / ".tox").mkdir()
    (clone / ".tox" / "log").write_text("cache")
    (clone / "auth").mkdir()
    (clone / "auth" / "token.py").write_text("code")
    git(clone, "remote", "add", "tok", "https://x-access-token:SECRET@github.com/o/r.git")
    (clone / "build").mkdir()
    (clone / "build" / "x").write_text("x")
    git(clone, "stash", "-q", "-u")
    git(clone, "stash", "pop", "-q")
    git(clone, "stash", "-q")
    (clone / "README.md").write_text("changed again\n")
    (clone / ".gitignore").unlink()
    executor = ex(root)
    act = Action("rescue", "c", str(clone), "unsafe")
    out = one(executor, act)
    assert out.status == "done", out.detail
    dest = Path(out.detail)
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["clone"] == "c"
    with tarfile.open(dest / "uncommitted.tar.gz") as tar:
        # Rescues never contain secrets (the clone is kept instead).
        assert sorted(tar.getnames()) == ["README.md", "auth/token.py", "build/x", "new.txt"]
    assert manifest["excluded_secrets"] == [".env", ".kube/config"]
    assert (dest.stat().st_mode & 0o777) == 0o700
    assert ((root / ".rescue").stat().st_mode & 0o777) == 0o700
    assert all((f.stat().st_mode & 0o777) == 0o600 for f in dest.iterdir())
    assert len(manifest["fingerprint"]) == 64
    assert (
        "SECRET" not in json.dumps(manifest) and "https://github.com/o/r.git" in manifest["remotes"]
    )
    assert "c" in executor.rescued
    restored = root / "restored"
    git(root, "clone", "-q", str(remote), str(restored))  # the documented restore
    git(restored, "fetch", "-q", str(dest / "repo.bundle"), execute.RESTORE_REFSPEC)
    assert "refs/rescued/refs/stash" in git(restored, "for-each-ref")
    assert manifest["bundle"] and manifest["restore"][0].startswith("git clone ")
    assert "refs/stash" in git(dest, "bundle", "list-heads", "repo.bundle")
    second = one(executor, act)
    assert second.status == "done" and Path(second.detail) != dest
    git(clone, "checkout", "-q", ".")
    (clone / "new.txt").unlink()
    shutil.rmtree(clone / "build")
    shutil.rmtree(clone / "auth")
    third = one(executor, act)
    assert third.status == "done" and third.detail.endswith("-2")
    assert not (Path(third.detail) / "uncommitted.tar.gz").exists()


def test_rescue_edge_cases(root: Path) -> None:
    """Empty repos, broken bundles and busy clones are handled.

    Args:
        root: Clone root.
    """
    empty = root / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    (empty / "a.txt").write_text("a")
    git(empty, "add", "a.txt")
    (empty / "b.txt").write_text("b")
    out = one(ex(root), Action("rescue", "empty", str(empty), "unsafe"))
    assert out.status == "done" and out.detail == execute.NOTHING_UNPUSHED
    tars = list((root / ".rescue").glob("empty-*/uncommitted.tar.gz"))
    with tarfile.open(tars[0]) as tar:
        assert sorted(tar.getnames()) == ["a.txt", "b.txt"]

    plain = root / "plain"
    plain.mkdir()
    unknown = one(ex(root), Action("rescue", "plain", str(plain), "unsafe"))
    assert (unknown.status, unknown.detail) == ("skipped", SECRETS_UNKNOWN)
    no_refs: dict[object, Result] = {
        ("git", "--no-optional-locks", "-C", str(plain), "for-each-ref"): Result(0, "", "")
    }
    executor = ex(root, busy={"busy"}, runner=FakeRunner(no_refs))
    out = one(executor, Action("rescue", "plain", str(plain), "unsafe"))
    assert out.status == "failed"
    assert not list((root / ".rescue").glob("plain-*")), "failed rescue left a directory"
    delete = Action("delete-clone", "plain", str(plain), "x", extra={"needs_rescue": True})
    assert one(executor, delete).detail == "rescue missing"
    out = one(executor, Action("rescue", "busy", str(root / "busy"), "unsafe"))
    assert out.status == "skipped"


def test_rescue_crash_cleans_up(
    make_clone: Callable[[str], Path], root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rescue that crashes half-way leaves no directory that could count.

    Regression test: a manifest-less directory once counted as a rescue.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    clone = make_clone("c")
    (clone / "new.txt").write_text("new")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("agent_sweeper.execute.tarfile.open", boom)
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe"))
    assert out.status == "failed" and "disk full" in out.detail
    assert list((root / ".rescue").iterdir()) == []


def test_unknown_action(root: Path) -> None:
    """An unknown action kind fails alone instead of stopping the sweep.

    Args:
        root: Clone root.
    """
    out = ex(root).execute([Action("bogus", "c", "/t", "x"), Action("skip", "c", "/t", "y")])
    assert [o.status for o in out] == ["failed", "skipped"]


def test_delete_clone(make_clone: Callable[[str], Path], root: Path) -> None:
    """Clones are deleted only when the last checks pass.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    executor = ex(root, busy={"busy"})
    act = Action("delete-clone", "c", str(clone), "safe", extra={"safe": True})
    rescue_first = Action(
        "delete-clone", "c", str(clone), "safe", extra={"safe": True, "needs_rescue": True}
    )
    assert one(executor, rescue_first).detail == "rescue missing"
    (clone / "README.md").write_text("changed\n")
    assert one(executor, act).detail == "tree changed since planning"
    unsafe = Action("delete-clone", "c", str(clone), "unsafe", extra={"safe": False})
    assert one(executor, unsafe).status == "done"
    assert not clone.exists()
    busy = Action("delete-clone", "busy", str(root / "busy"), "x")
    assert one(executor, busy).detail == "clone in use now"


def test_delete_worktree(make_clone: Callable[[str], Path], root: Path) -> None:
    """Linked worktrees are removed through their main clone.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    main = make_clone("main")
    wt = root / "wt"
    git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    act = Action(
        "delete-clone", "wt", str(wt), "safe", extra={"safe": True, "main_repo": str(main)}
    )
    assert one(ex(root), act).status == "done"
    assert not wt.exists()
    assert "wt" not in git(main, "worktree", "list")
    runner = FakeRunner({("git", "-C", str(main), "worktree", "remove"): FAIL})
    git(main, "worktree", "add", "-q", "-b", "side2", str(wt))
    assert one(ex(root, runner=runner), act).status == "done"
    assert not wt.exists()


def test_delete_clone_rechecks(make_clone: Callable[[str], Path], root: Path) -> None:
    """Commits or changes after planning cancel the deletion.

    Regression test: only the tree was re-checked before.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    planned = {"safe": True, "fingerprint": fingerprint(clone, FakeRunner())}
    git(clone, "stash", "-q")  # nothing to stash: refs unchanged
    git(clone, "commit", "--allow-empty", "-qm", "late work")
    act = Action("delete-clone", "c", str(clone), "safe", extra=planned)
    assert one(ex(root), act).detail == "refs changed since planning"
    planned = {"safe": True, "last_change": last_change(clone, FakeRunner()) - 100}
    act = Action("delete-clone", "c", str(clone), "safe", extra=planned)
    assert one(ex(root), act).detail == "clone changed since planning"
    assert clone.exists()


def test_rescue_nested_repo(make_clone: Callable[[str], Path], root: Path) -> None:
    """An untracked nested repository makes the rescue fail.

    Regression test: its commits were silently left out of the rescue.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    (clone / "vendor" / "dep").mkdir(parents=True)
    git(clone / "vendor" / "dep", "init", "-q")
    git(clone / "vendor" / "dep", "commit", "--allow-empty", "-qm", "patched")
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe"))
    assert out.status == "failed" and "nested repository vendor/" in out.detail
    assert list((root / ".rescue").iterdir()) == []


def test_after_delete(make_clone: Callable[[str], Path], root: Path) -> None:
    """Follow-up actions run only if their clone was really deleted.

    Regression test: a skipped deletion still cleaned the ledger entry.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    Ledger(root).save(Entry(id="c", clone="c"))
    delete = Action("delete-clone", "c", str(clone), "x", extra={"needs_rescue": True})
    after = Action("set-state", "c", "cleaned", "clone deleted", extra={"after_delete": "c"})
    out = ex(root).execute([delete, after])
    assert [o.status for o in out] == ["skipped", "skipped"]
    entry = Ledger(root).load("c")
    assert entry is not None and entry.state == "running"
    delete.extra = {"safe": True}
    out = ex(root).execute([delete, after])
    assert [o.status for o in out] == ["done", "done"]


def test_rescue_listing_fails(make_clone: Callable[[str], Path], root: Path) -> None:
    """A rescue fails, and leaves nothing, if git cannot list the files.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    runner = FakeRunner({("git", "--no-optional-locks", "-C", str(clone), "ls-files"): FAIL})
    out = one(ex(root, runner=runner), Action("rescue", "c", str(clone), "unsafe"))
    assert (out.status, out.detail) == ("failed", "could not list uncommitted files")
    assert list((root / ".rescue").iterdir()) == []


def test_set_state(root: Path) -> None:
    """Ledger states are set; missing entries are skipped.

    Args:
        root: Clone root.
    """
    ledger = Ledger(root)
    ledger.save(Entry(id="c", clone="c"))
    assert one(ex(root), Action("set-state", "c", "cleaned", "gone")).status == "done"
    loaded = ledger.load("c")
    assert loaded is not None and loaded.state == "cleaned"
    assert one(ex(root), Action("set-state", "x", "cleaned", "gone")).status == "skipped"


def res_action(kind: str, name: str, clone: str = "c", kubeconfig: str | None = None) -> Action:
    """Return a remove-resource action.

    Args:
        kind: Resource kind.
        name: Resource name.
        clone: Owning clone.
        kubeconfig: Kubeconfig path.

    Returns:
        The action.
    """
    extra = {"kind": kind, "name": name, "kubeconfig": kubeconfig}
    return Action("remove-resource", clone, f"{kind}:{name}", "gone", extra=extra)


def test_remove_docker(root: Path) -> None:
    """Docker removals mark the ledger; missing objects count as removed.

    Args:
        root: Clone root.
    """
    ledger = Ledger(root)
    ledger.save(
        Entry(
            id="c",
            clone="c",
            resources=[Resource("docker-container", "x"), Resource("docker-network", "n")],
        )
    )
    runner = FakeRunner(
        {
            ("docker", "rm"): OK,
            ("docker", "network", "rm"): Result(1, "", "Error: No such network: n"),
        }
    )
    executor = ex(root, runner=runner)
    assert one(executor, res_action("docker-container", "x")).status == "done"
    assert one(executor, res_action("docker-network", "n")).status == "done"
    entry = ledger.load("c")
    assert entry is not None and all(r.removed for r in entry.resources)
    failing = ex(root, runner=FakeRunner({"docker": FAIL}))
    assert one(failing, res_action("docker-container", "y")).status == "failed"
    assert one(ex(root, busy={"c"}), res_action("port", "1")).detail == "clone in use now"
    port = one(ex(root), res_action("port", "8080", clone="nobody"))
    assert port.status == "done" and "never killed" in port.detail


def test_remove_clusters(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clusters go via their tool, or via docker labels without it.

    Args:
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(execute, "which", lambda p: True)
    runner = FakeRunner({"kind": OK, "k3d": FAIL})
    assert one(ex(root, runner=runner), res_action("kind-cluster", "k")).status == "done"
    assert one(ex(root, runner=runner), res_action("k3d-cluster", "k")).status == "failed"
    monkeypatch.setattr(execute, "which", lambda p: False)
    cases: list[tuple[dict[object, Result], str]] = [
        ({("docker", "ps"): FAIL}, "failed"),
        ({("docker", "ps"): OK}, "done"),
        ({("docker", "ps"): Result(0, "id1\nid2\n", ""), ("docker", "rm"): OK}, "done"),
    ]
    for answers, status in cases:
        got = one(ex(root, runner=FakeRunner(dict(answers))), res_action("kind-cluster", "k"))
        assert got.status == status


def test_remove_namespace(root: Path, tmp_path: Path) -> None:
    """Namespaces are deleted only on loopback clusters.

    Args:
        root: Clone root.
        tmp_path: Pytest fixture.
    """
    executor = ex(root, runner=FakeRunner({"kubectl": OK}))
    got = one(executor, res_action("k8s-namespace", "ns"))
    assert got.status == "done" and "released" in got.detail
    remote = tmp_path / "remote.yaml"
    released = "released only: not a local kind/k3d cluster"
    cases = [
        "current-context: kind-x\nclusters:\n- cluster:\n    server: https://cluster.example:6443\n",
        "current-context: kind-x\n- cluster:\n    server: https://localhost.corp.example:6443\n",
        "current-context: remote\n- cluster:\n    server: https://127.0.0.1:6443\n",
        "current-context: kind-x\n",
    ]
    for text in cases:
        remote.write_text(text)
        got = one(executor, res_action("k8s-namespace", "ns", kubeconfig=str(remote)))
        assert got.detail == released, text
    local = tmp_path / "local.yaml"
    local.write_text(
        "current-context: 'k3d-t'\nclusters:\n- cluster:\n    server: \"https://127.0.0.1:41234\"\n"
    )
    got = one(executor, res_action("k8s-namespace", "ns", kubeconfig=str(local)))
    assert got.status == "done" and got.detail == ""
    failing = ex(root, runner=FakeRunner({"kubectl": FAIL}))
    assert one(failing, res_action("k8s-namespace", "ns", kubeconfig=str(local))).status == "failed"


def test_prune_and_notify(root: Path) -> None:
    """Rescues are pruned inside the rescue dir; notifications are recorded.

    Args:
        root: Clone root.
    """
    rescue = root / ".rescue" / "a-2026-01-01"
    rescue.mkdir(parents=True)
    executor = ex(root, runner=FakeRunner({"notify-cmd": OK}))
    assert one(executor, Action("prune-rescue", "a", str(rescue), "old")).status == "done"
    assert not rescue.exists()
    outside = Action("prune-rescue", "a", str(root / "c"), "old")
    assert one(executor, outside).detail == "target outside rescue dir"
    assert one(executor, Action("notify", "", "notify-cmd", "disk at 93%")).status == "done"
    state = json.loads((root / ".ledger" / "state.json").read_text())
    assert state["last_notify"] > 0
    (root / ".ledger" / "state.json").write_text("garbage")
    failing = ex(root, runner=FakeRunner({"notify-cmd": FAIL}))
    assert one(failing, Action("notify", "", "notify-cmd", "disk")).status == "failed"
    with_args = FakeRunner({"notify-cmd": OK})
    assert (
        one(ex(root, runner=with_args), Action("notify", "", "notify-cmd -u x", "d")).status
        == "done"
    )
    assert with_args.calls[-1][:3] == ["notify-cmd", "-u", "x"]
    for bad in ('notify-cmd "x', "   "):
        (root / ".ledger" / "state.json").unlink()
        out = one(ex(root, runner=FakeRunner({})), Action("notify", "", bad, "d"))
        assert (out.status, out.detail) == ("failed", "invalid notification command")
        assert (root / ".ledger" / "state.json").exists()


def test_delete_clone_in_use_after_rename(
    make_clone: Callable[[str], Path], root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that enters the clone after the check stops the deletion.

    Regression test: the in-use check and rmtree were not atomic.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    clone = make_clone("c")
    act = Action("delete-clone", "c", str(clone), "unsafe", extra={"safe": False})
    checks: list[str] = []

    def late(path: str) -> bool:
        checks.append(path)
        return ".trash" in path  # idle at the guard, busy after the rename

    executor = Executor(Config(root=root), late, FakeRunner())
    assert one(executor, act).detail == "in use after rename"
    assert clone.is_dir() and "c" not in executor.deleted
    assert any(".trash" in p for p in checks)

    real = os.rename

    def no_way_back(src: object, dst: object) -> None:
        if Path(str(dst)) == clone:
            raise OSError("taken")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("agent_sweeper.execute.os.rename", no_way_back)
    out = one(executor, act)
    assert out.status == "failed" and out.detail.endswith(execute.KEPT)
    assert not clone.exists()
    assert [p.name.endswith(execute.KEPT) for p in (root / ".trash").iterdir()] == [True]


def test_prune_trash(root: Path) -> None:
    """Only unpinned, idle entries inside the trash are pruned.

    Args:
        root: Clone root.
    """
    trash = root / ".trash"
    (trash / "c-1").mkdir(parents=True)
    (trash / "busy-1").mkdir()
    (trash / f"c-2{execute.KEPT}").mkdir()
    executor = ex(root, busy={"busy-1"})
    assert one(executor, Action("prune-trash", "", str(trash / "c-1"), "x")).status == "done"
    assert not (trash / "c-1").exists()
    assert one(executor, Action("prune-trash", "", str(trash / "busy-1"), "x")).detail == (
        "trash entry in use"
    )
    kept = Action("prune-trash", "", str(trash / f"c-2{execute.KEPT}"), "x")
    assert one(executor, kept).detail == "not a prunable trash entry"
    outside = Action("prune-trash", "", str(root / "c"), "x")
    assert one(executor, outside).detail == "not a prunable trash entry"


def test_delete_clone_rechecks_secrets(make_clone: Callable[[str], Path], root: Path) -> None:
    """A secret-like file that appeared after planning stops the deletion.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    (clone / ".env").write_text("x")
    act = Action("delete-clone", "c", str(clone), "unsafe", extra={"safe": False})
    out = one(ex(root), act)
    assert (out.status, out.detail) == ("skipped", SECRETS_REASON)
    assert clone.is_dir()


def test_prune_trash_age_from_name(root: Path) -> None:
    """The age of a trash entry comes from its name, not its mtime.

    Regression test: a rename keeps the clone's old mtime, so a deletion
    that crashed a second ago looked hours old.

    Args:
        root: Clone root.
    """
    trash = root / ".trash"
    fresh = trash / f"c-{time.time_ns()}"
    fresh.mkdir(parents=True)
    os.utime(fresh, (0, 0))
    out = one(ex(root), Action("prune-trash", "", str(fresh), "x"))
    assert (out.status, out.detail) == ("skipped", "trash entry too young")
    assert fresh.exists()


def test_tracked_cache_named_dirs_are_work(make_clone: Callable[[str], Path], root: Path) -> None:
    """Tracked dirs with cache names (e.g. .tools) are rescued like source.

    Regression test: changes and new files in a tracked .tools/ were left out
    of the rescue and did not make the tree dirty.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    (clone / ".tools").mkdir()
    (clone / ".tools" / "run.sh").write_text("v1\n")
    git(clone, "add", ".tools/run.sh")
    git(clone, "commit", "-qm", "tools")
    (clone / ".tools" / "run.sh").write_text("v2\n")
    (clone / ".tools" / "new.sh").write_text("new\n")
    (clone / ".tools" / "node_modules").mkdir()
    (clone / ".tools" / "node_modules" / "dep.js").write_text("x")
    (clone / "node_modules").mkdir()
    (clone / "node_modules" / "dep.js").write_text("x")
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe"))
    assert out.status == "done", out.detail
    with tarfile.open(Path(out.detail) / "uncommitted.tar.gz") as tar:
        assert sorted(tar.getnames()) == [".tools/new.sh", ".tools/run.sh"]
    git(clone, "checkout", "-q", ".")
    act = Action("delete-clone", "c", str(clone), "safe", extra={"safe": True})
    assert one(ex(root), act).detail == "tree changed since planning"  # .tools/new.sh
    ls_fail: dict[object, Result] = {
        ("git", "--no-optional-locks", "-C", str(clone), "ls-files", "-z"): Result(1, "", "x")
    }
    out = one(ex(root, runner=FakeRunner(ls_fail)), Action("rescue", "c", str(clone), "unsafe"))
    assert (out.status, out.detail) == ("failed", "could not list tracked files")


def test_rescue_refuses_committed_secrets(make_clone: Callable[[str], Path], root: Path) -> None:
    """No bundle is written when unpushed commits or the stash hold secrets.

    Regression test: the bundle copied secrets that the tarball left out.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    (clone / "deploy.pem").write_text("x")
    git(clone, "add", "deploy.pem")
    git(clone, "commit", "-qm", "oops")
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe"))
    assert (out.status, out.detail) == ("skipped", SECRETS_REASON)
    assert not (root / ".rescue").exists()


def test_rescue_bundles_only_this_worktree(make_clone: Callable[[str], Path], root: Path) -> None:
    """A linked worktree's bundle never carries the main worktree's HEAD.

    Regression test: ``git bundle create --all`` also bundled other
    worktrees' HEADs, which the secret scan did not cover.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    main = make_clone("main")
    git(main, "worktree", "add", "-q", "-b", "side", str(root / "wt"))
    wt = root / "wt"
    git(wt, "commit", "--allow-empty", "-qm", "side work")
    git(main, "checkout", "-q", "--detach")
    (main / ".env").write_text("x")
    git(main, "add", "-f", ".env")
    git(main, "commit", "-qm", "detached secret")
    assert facts.committed_secrets(main, FakeRunner(), pushed(main)) == [".env"]
    assert facts.committed_secrets(wt, FakeRunner(), pushed(wt)) == []
    out = one(ex(root), Action("rescue", "wt", str(wt), "unsafe"))
    assert out.status == "done", out.detail
    bundle = Path(out.detail) / "repo.bundle"
    heads = git(wt, "bundle", "list-heads", str(bundle))
    assert "refs/heads/side" in heads
    check = root / "check"
    git(root, "clone", "-q", str(main / ".git"), str(check))
    git(check, "fetch", "-q", str(bundle), execute.RESTORE_REFSPEC)
    assert ".env" not in git(check, "rev-list", "--objects", "refs/rescued/refs/heads/side")


def test_rescue_bundle_outcomes(
    make_clone: Callable[[str], Path], root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fully pushed clones need no bundle; git failures fail the rescue.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    clone = make_clone("c")
    planned = {"pushed": pushed(clone), "unpushed": 0}
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe", extra=planned))
    assert (out.status, out.detail) == ("done", execute.NOTHING_UNPUSHED)
    git(clone, "commit", "--allow-empty", "-qm", "local")
    prefix = ("git", "--no-optional-locks", "-C", str(clone))
    for word in ("init", "--git-common-dir", "update-ref", "bundle"):
        runner = FailOn(word)
        out = one(ex(root, runner=runner), Action("rescue", "c", str(clone), "unsafe"))
        assert (out.status, out.detail) == ("failed", "git bundle: disk full"), word
    assert not list((root / ".rescue").glob("*/work.git"))
    monkeypatch.setattr(execute, "committed_secrets", lambda path, runner, pushed: [])
    no_refs: dict[object, Result] = {(*prefix, "for-each-ref"): Result(1, "", "x")}
    out = one(ex(root, runner=FakeRunner(no_refs)), Action("rescue", "c", str(clone), "unsafe"))
    assert (out.status, out.detail) == ("failed", "could not list refs to bundle")


def test_bundle_uses_planned_exclusions(make_clone: Callable[[str], Path], root: Path) -> None:
    """A stale tracking ref never hides unpushed commits from the bundle.

    Regression test: the bundle excluded everything reachable from local
    remote-tracking refs, so a commit they wrongly claimed as pushed was
    neither bundled nor counted, and the clone was deleted.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    real = pushed(clone)
    git(clone, "commit", "--allow-empty", "-qm", "never pushed")
    local = git(clone, "rev-parse", "HEAD").strip()
    git(clone, "update-ref", "refs/remotes/origin/main", local)  # stale: claims pushed
    gh = facts.GhCache(clone / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    gf = facts.git_facts(clone, clone / ".git", FakeRunner(), gh, 0.0, False, True)
    assert gf.unpushed == 1 and local not in gf.pushed
    extra = {"pushed": gf.pushed, "unpushed": gf.unpushed}
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe", extra=extra))
    assert out.status == "done", out.detail
    heads = git(clone, "bundle", "list-heads", str(Path(out.detail) / "repo.bundle"))
    assert local in heads
    # The planned count is the invariant: unpushed work but no bundle = failure.
    claim = {"pushed": [local], "unpushed": 1}
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe", extra=claim))
    assert out.detail == "rescue incomplete: no bundle but unpushed commits exist"
    prefix = ("git", "--no-optional-locks", "-C", str(clone))
    check_fails: dict[object, Result] = {(*prefix, "rev-list", "--stdin"): Result(1, "", "x")}
    extra_real = {"pushed": real, "unpushed": 1}
    out = one(
        ex(root, runner=FakeRunner(check_fails)),
        Action("rescue", "c", str(clone), "unsafe", extra=extra_real),
    )
    assert out.detail == "rescue incomplete: bundle needs commits that are not verified on a remote"
    assert len(list((root / ".rescue").iterdir())) == 1  # failed ones are removed


def test_bundle_starts_from_every_local_ref(make_clone: Callable[[str], Path], root: Path) -> None:
    """Commits only on unusual local refs are counted and bundled alike.

    Regression test: the unpushed count used all local refs, the bundle
    only branches, tags, HEAD and the stash.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    git(clone, "commit", "--allow-empty", "-qm", "only here")
    sha = git(clone, "rev-parse", "HEAD").strip()
    git(clone, "update-ref", "refs/rescued/heads/x", sha)
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    gh = facts.GhCache(clone / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    gf = facts.git_facts(clone, clone / ".git", FakeRunner(), gh, 0.0, False, True)
    assert gf.unpushed == 1
    extra = {"pushed": gf.pushed, "unpushed": gf.unpushed}
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe", extra=extra))
    assert out.status == "done", out.detail
    heads = git(clone, "bundle", "list-heads", str(Path(out.detail) / "repo.bundle"))
    assert f"{sha} refs/sweeper/refs/rescued/heads/x" in heads


class FailOn(FakeRunner):
    """Runner that fails every command containing one word."""

    def __init__(self, word: str) -> None:
        """Remember the word.

        Args:
            word: Command argument that makes the command fail.
        """
        super().__init__()
        self.word = word

    def __call__(self, cmd: Any, timeout: float, **kwargs: Any) -> Result:
        """Fail matching commands, delegate the rest.

        Args:
            cmd: Command.
            timeout: Timeout.
            **kwargs: Passed on.

        Returns:
            The result.
        """
        if self.word in cmd:
            return Result(1, "", "disk full")
        return super().__call__(cmd, timeout, **kwargs)


def test_rescue_every_stash_entry(
    make_clone: Callable[[str], Path], root: Path, remote: Path
) -> None:
    """All stash entries are counted, bundled and restorable.

    Regression test: only stash@{0} was rescued; older entries live only in
    the stash reflog.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        remote: Bare remote.
    """
    clone = make_clone("c")
    for n in range(3):
        (clone / f"f{n}.txt").write_text(str(n))
        git(clone, "add", f"f{n}.txt")
        git(clone, "stash", "-q")
    shas = git(clone, "log", "-g", "--format=%H", "refs/stash").split()
    assert len(shas) == 3
    gh = facts.GhCache(clone / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    gf = facts.git_facts(clone, clone / ".git", FakeRunner(), gh, 0.0, False, True)
    assert gf.unpushed == 6  # each stash entry is a commit plus its index commit
    before = fingerprint(clone, FakeRunner())
    extra = {"pushed": gf.pushed, "unpushed": gf.unpushed}
    out = one(ex(root), Action("rescue", "c", str(clone), "unsafe", extra=extra))
    assert out.status == "done", out.detail
    restored = root / "restored"
    git(root, "clone", "-q", str(remote), str(restored))
    git(restored, "fetch", "-q", str(Path(out.detail) / "repo.bundle"), execute.RESTORE_REFSPEC)
    got = [git(restored, "rev-parse", f"refs/rescued/{r}").strip()
           for r in ("refs/stash", "stash/1", "stash/2")]  # fmt: skip
    assert got == shas
    git(clone, "stash", "drop", "-q", "stash@{2}")
    assert fingerprint(clone, FakeRunner()) != before
    prefix = ("git", "--no-optional-locks", "-C", str(clone))
    no_log: dict[object, Result] = {(*prefix, "log", "-g"): Result(1, "", "x")}
    assert facts.history_starts(clone, FakeRunner(no_log)) is None


def test_unpushed_annotated_tag(make_clone: Callable[[str], Path]) -> None:
    """A local annotated tag on a pushed commit counts as unpushed.

    Args:
        make_clone: Clone factory.
    """
    clone = make_clone("c")
    git(clone, "tag", "-a", "-m", "local only", "v9")
    gh = facts.GhCache(clone / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    assert facts.git_facts(clone, clone / ".git", FakeRunner(), gh, 0.0, False, True).unpushed == 1
    git(clone, "push", "-q", "origin", "v9")
    assert facts.git_facts(clone, clone / ".git", FakeRunner(), gh, 0.0, False, True).unpushed == 0
    starts = facts.history_starts(clone, FakeRunner())
    assert starts is not None
    prefix = ("git", "--no-optional-locks", "-C", str(clone))
    broken: dict[object, Result] = {(*prefix, "cat-file"): Result(1, "", "x")}
    assert facts.unpushed_tags(clone, FakeRunner(broken), starts, set()) is None


def test_unpushed_with_unborn_head(make_clone: Callable[[str], Path]) -> None:
    """Refs other than branches count even when HEAD is unborn.

    Regression test: an unborn HEAD without branches short-cut to 0.

    Args:
        make_clone: Clone factory.
    """
    clone = make_clone("c")
    git(clone, "commit", "--allow-empty", "-qm", "only on a side ref")
    git(clone, "update-ref", "refs/rescued/heads/x", "HEAD")
    git(clone, "checkout", "-q", "--orphan", "fresh")
    git(clone, "branch", "-q", "-D", "main")
    gh = facts.GhCache(clone / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    assert facts.git_facts(clone, clone / ".git", FakeRunner(), gh, 0.0, False, True).unpushed == 1


def test_restore_detached_head_and_real_stash_ref(
    make_clone: Callable[[str], Path], root: Path, remote: Path
) -> None:
    """Detached-HEAD work comes back, and a real refs/stash-1 does not clash.

    Regression test: the restore refspec missed the bundle's HEAD, and older
    stash entries were named like real refs.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        remote: Bare remote.
    """
    clone = make_clone("c")
    git(clone, "checkout", "-q", "--detach")
    git(clone, "commit", "--allow-empty", "-qm", "detached work")
    head = git(clone, "rev-parse", "HEAD").strip()
    git(clone, "update-ref", "refs/stash-1", head)
    for n in range(2):
        (clone / f"s{n}.txt").write_text(str(n))
        git(clone, "add", f"s{n}.txt")
        git(clone, "stash", "-q")
    stashes = git(clone, "log", "-g", "--format=%H", "refs/stash").split()
    out = one(
        ex(root), Action("rescue", "c", str(clone), "unsafe", extra={"pushed": pushed(clone)})
    )
    assert out.status == "done", out.detail
    manifest = json.loads((Path(out.detail) / "manifest.json").read_text())
    assert execute.RESTORE_REFSPEC in manifest["restore"][1]
    restored = root / "restored"
    git(root, "clone", "-q", str(remote), str(restored))
    git(restored, "fetch", "-q", str(Path(out.detail) / "repo.bundle"), execute.RESTORE_REFSPEC)
    names = ("HEAD", "refs/stash-1", "refs/stash", "stash/1")
    got = [git(restored, "rev-parse", f"refs/rescued/{n}").strip() for n in names]
    assert got == [head, head, *stashes]
