"""Tests of the collectors in :mod:`agent_sweeper.facts`."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_sweeper import config, facts
from agent_sweeper.config import Config
from agent_sweeper.ledger import Entry, Ledger
from agent_sweeper.rules import GitFacts, RescueInfo
from agent_sweeper.util import Result

from .conftest import FakeRunner, git, pushed

OK = Result(0, "", "")
FAIL = Result(1, "", "boom")


def fake_proc(tmp_path: Path, clone: Path) -> Path:
    """Build a fake /proc tree.

    Args:
        tmp_path: Pytest fixture.
        clone: A clone path that process 100 uses.

    Returns:
        The fake proc root.
    """
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    for pid, ppid in ((100, 1), (200, 999), (201, 200), (400, 1)):
        base = proc / str(pid)
        base.mkdir()
        (base / "stat").write_text(f"{pid} (my prog) S {ppid} 0 0\n")
        (base / "cwd").symlink_to(clone if pid != 400 else "/")
    (proc / "300").mkdir()  # no stat: ignored
    fd = proc / "100" / "fd"
    fd.mkdir()
    (fd / "3").symlink_to(clone / "file")
    (fd / "4").symlink_to("socket:[1]")
    (fd / "5").write_text("not a link")
    (proc / "201" / "fd").mkdir()
    (proc / "201" / "fd" / "3").symlink_to(clone / "ours")
    return proc


def test_scan_proc(tmp_path: Path) -> None:
    """Cwd and open files are found; our own process tree is skipped.

    Args:
        tmp_path: Pytest fixture.
    """
    clone = tmp_path / "root" / "c"
    found = facts.scan_proc(fake_proc(tmp_path, clone), self_pid=200)
    assert (str(clone), 100) in found
    assert (str(clone / "file"), 100) in found
    assert all(pid not in (200, 201) for _, pid in found)
    assert ("/", 400) in found
    assert not any(p.startswith("socket") for p, _ in found)
    assert any(pid == os.getpid() for _, pid in facts.scan_proc(self_pid=-1))


def test_users_by_clone(tmp_path: Path) -> None:
    """Paths group by the clone directly below the root.

    Args:
        tmp_path: Pytest fixture.
    """
    root = tmp_path / "root"
    pairs = [(f"{root}/a/x/y", 1), (f"{root}/a", 2), (f"{root}/", 3), ("/elsewhere", 4)]
    assert facts.users_by_clone(root, pairs) == {"a": {1, 2}}


def test_users_by_clone_exact_dir(tmp_path: Path) -> None:
    """A process whose cwd is the clone itself counts.

    Args:
        tmp_path: Pytest fixture.
    """
    root = tmp_path / "root"
    pairs = [(f"{root}/a/", 7)]
    assert facts.users_by_clone(root, pairs) == {"a": {7}}


def test_docker_mounts() -> None:
    """Mount sources of running containers are listed."""
    assert facts.docker_mounts(FakeRunner({"docker": FAIL})) == []
    assert facts.docker_mounts(FakeRunner({("docker", "ps"): OK})) == []
    runner = FakeRunner({("docker", "ps"): Result(0, "abc\n", ""), ("docker", "inspect"): FAIL})
    assert facts.docker_mounts(runner) == []
    runner = FakeRunner(
        {
            ("docker", "ps"): Result(0, "abc\n", ""),
            ("docker", "inspect"): Result(0, "/r/a\nvolume\n", ""),
        }
    )
    assert facts.docker_mounts(runner) == [("/r/a", 0)]


def test_scan_tree(tmp_path: Path) -> None:
    """The walk skips build outputs and .git for activity, not for size.

    Args:
        tmp_path: Pytest fixture.
    """
    clone = tmp_path / "c"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "objects").write_bytes(b"x" * 8192)
    (clone / "src").mkdir()
    (clone / "src" / "a.py").write_text("x")
    (clone / "node_modules" / "p").mkdir(parents=True)
    (clone / "pkg" / "dist").mkdir(parents=True)
    (clone / "link").symlink_to("/usr")
    locked = clone / "locked"
    locked.mkdir()
    old = time.time() - 10 * config.DAY
    for path in (clone / "src" / "a.py", clone / "src", clone / "pkg", locked, clone):
        os.utime(path, (old, old))
    os.utime(clone / "link", (old, old), follow_symlinks=False)
    os.utime(clone / "node_modules", (old + 50, old + 50))
    locked.chmod(0)
    try:
        newest, size, git_size, outputs, nested, _, _ = facts.scan_tree(clone, git=True)
    finally:
        locked.chmod(0o755)
    assert newest == pytest.approx(old, abs=1)
    assert git_size >= 8192
    assert size >= git_size
    assert sorted(Path(p).name for p, _ in outputs) == ["dist", "node_modules"]
    assert not nested
    outputs = facts.scan_tree(clone, git=False).outputs
    assert [Path(p).name for p, _ in outputs] == ["node_modules"]
    assert facts.scan_tree(tmp_path / "missing", git=True) == facts.Tree(0.0, 0, 0, [])
    (clone / "src" / "vendored").mkdir()
    (clone / "src" / "vendored" / ".git").mkdir()
    assert facts.scan_tree(clone, git=True).nested
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".gitmodules").write_text("")
    assert facts.scan_tree(sub, git=True).nested


def test_scan_tree_vanishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries that vanish during the walk are skipped.

    Args:
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """

    class Gone:
        name = "x"
        path = str(tmp_path / "x")

        def stat(self, follow_symlinks: bool = True) -> os.stat_result:
            raise OSError("gone")

    class It:
        def __enter__(self) -> list[Gone]:
            return [Gone()]

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("agent_sweeper.facts.os.scandir", lambda p: It())
    assert facts.scan_tree(tmp_path, git=True)[3] == []


def test_git_dir_of(tmp_path: Path) -> None:
    """Git directories are found for clones and worktrees, not broken ones.

    Args:
        tmp_path: Pytest fixture.
    """
    clone = tmp_path / "c"
    (clone / ".git").mkdir(parents=True)
    assert facts.git_dir_of(clone) == clone / ".git"
    wt = tmp_path / "w"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {clone / '.git'}\n")
    assert facts.git_dir_of(wt) == clone / ".git"
    (wt / ".git").write_text("gitdir: ../c/.git\n")
    assert facts.git_dir_of(wt) == clone / ".git"
    (wt / ".git").write_text("gitdir: /nonexistent\n")
    assert facts.git_dir_of(wt) is None
    (wt / ".git").write_text("garbage\n")
    assert facts.git_dir_of(wt) is None
    assert facts.git_dir_of(tmp_path) is None


def test_git_activity_and_worktrees(tmp_path: Path) -> None:
    """Activity reads HEAD, index and branch refs; worktrees must be live.

    Args:
        tmp_path: Pytest fixture.
    """
    gitdir = tmp_path / ".git"
    (gitdir / "refs" / "heads" / "feat").mkdir(parents=True)
    (gitdir / "refs" / "heads" / "feat" / "x").write_text("sha")
    (gitdir / "HEAD").write_text("ref")
    assert facts.git_activity(gitdir) > 0
    assert facts.git_activity(tmp_path / "none") == 0.0
    old = time.time() - config.DAY
    for p in (gitdir / "HEAD", gitdir / "refs" / "heads" / "feat" / "x"):
        os.utime(p, (old, old))
    for d in (gitdir / "refs" / "heads" / "feat", gitdir / "refs" / "heads", gitdir / "refs"):
        os.utime(d, (old, old))
    (gitdir / "refs" / "remotes" / "origin").mkdir(parents=True)
    (gitdir / "refs" / "remotes" / "origin" / "main").write_text("fetched")
    os.utime(gitdir / "refs", (old, old))
    assert facts.git_activity(gitdir) < old + 10  # fetches do not count
    (gitdir / "refs" / "tags").mkdir()
    (gitdir / "refs" / "tags" / "keep").write_text("sha")
    assert facts.git_activity(gitdir) > old + 10  # a new tag counts
    assert facts.linked_worktrees(gitdir) == []
    live = tmp_path / "live"
    live.mkdir()
    (live / ".git").write_text("x")
    for name, target in (("a", live / ".git"), ("b", tmp_path / "dead" / ".git")):
        (gitdir / "worktrees" / name).mkdir(parents=True)
        (gitdir / "worktrees" / name / "gitdir").write_text(f"{target}\n")
    (gitdir / "worktrees" / "c").mkdir()
    assert facts.linked_worktrees(gitdir) == [str(live)]


def test_git_activity_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refs that vanish during the walk are skipped.

    Args:
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(
        "agent_sweeper.facts.os.walk", lambda p: iter([(str(tmp_path / "x"), [], ["y"])])
    )
    assert facts.git_activity(tmp_path) == 0.0


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("git@github.com:example-org/example-repo.git", "example-org/example-repo"),
        ("https://github.com/ga4gh/ga4gh-sdk", "ga4gh/ga4gh-sdk"),
        ("https://github.com/o/r.git/", "o/r"),
        ("/local/path.git", None),
    ],
)
def test_github_repo(url: str, repo: str | None) -> None:
    """GitHub URLs map to owner/name.

    Args:
        url: Remote URL.
        repo: Expected result.
    """
    assert facts.github_repo(url) == repo


def test_gh_cache(tmp_path: Path) -> None:
    """Answers are cached with TTLs; failures stay unknown.

    Args:
        tmp_path: Pytest fixture.
    """
    prs = [{"number": 1, "state": "MERGED", "headRefOid": "a"}]
    runner = FakeRunner({"gh": Result(0, json.dumps(prs), "")})
    cache = facts.GhCache(tmp_path / "c.json", runner)
    assert cache.prs("o/r", "b", 0.0) == prs
    assert cache.prs("o/r", "b", config.DAY) == prs
    assert cache.calls == 1
    cache.save()
    again = facts.GhCache(tmp_path / "c.json", FakeRunner({"gh": Result(0, "[]", "")}))
    assert again.prs("o/r", "b", config.GH_CACHE_TTL_FINAL + 1) == []
    assert again.prs("o/r", "b", config.GH_CACHE_TTL_FINAL + 2) == []
    assert again.calls == 1
    assert again.prs("o/r", "b", config.GH_CACHE_TTL_FINAL + 2 * config.HOUR) == []
    assert again.calls == 2
    assert facts.GhCache(tmp_path / "x.json", FakeRunner({"gh": FAIL})).prs("o/r", "b", 0) is None
    bad = FakeRunner({"gh": Result(0, "not json", "")})
    assert facts.GhCache(tmp_path / "x.json", bad).prs("o/r", "b", 0) is None
    off = facts.GhCache(tmp_path / "x.json", runner, enabled=False)
    assert off.prs("o/r", "b", 0) is None
    (tmp_path / "file").write_text("")
    facts.GhCache(tmp_path / "file" / "c.json", runner).save()


def test_pick_pr() -> None:
    """The PR whose head is the tip wins, else the newest."""
    prs = [
        {"number": 1, "state": "MERGED", "headRefOid": "a"},
        {"number": 5, "state": "OPEN", "headRefOid": "b"},
    ]
    assert facts.pick_pr(prs, "a") == prs[0]
    assert facts.pick_pr(prs, "z") == prs[1]
    assert facts.pick_pr([], "a") is None
    assert facts.pick_pr(None, "a") is None


def test_parse_status() -> None:
    """Changes outside build outputs count; renames consume two items."""
    assert not facts.parse_status("")
    assert not facts.parse_status("?? node_modules/\0?? pkg/.venv/\0")
    assert facts.parse_status("?? src/pkg/build/\0")
    assert facts.parse_status("?? .venvrc\0")
    assert facts.parse_status("?? notes.txt\0")
    assert facts.parse_status(" M a.py\0")
    assert facts.parse_status("R  new\0old\0")
    assert not facts.parse_status("x\0")


def test_git_facts_states(make_clone: Callable[[str], Path], remote: Path) -> None:
    """Clean, dirty, stashed and unpushed clones are told apart.

    Args:
        make_clone: Clone factory.
        remote: Bare remote.
    """
    runner = FakeRunner()

    def gf(path: Path, online: bool = True, fetch: bool = False) -> GitFacts:
        return facts.git_facts(path, path / ".git", runner, None, 0.0, fetch, online)

    clean = make_clone("clean")
    (clean / "node_modules").mkdir()
    (clean / "node_modules" / "x").write_text("x")
    got = gf(clean, fetch=True)
    assert got.safe
    assert got.work_branches == []
    assert gf(clean, online=False).unpushed is None

    dirty = make_clone("dirty")
    (dirty / "README.md").write_text("changed\n")
    assert gf(dirty).dirty is True

    stash = make_clone("stash")
    (stash / "README.md").write_text("changed\n")
    git(stash, "stash", "-q")
    got = gf(stash)
    assert got.stash and got.dirty is False

    ahead = make_clone("ahead")
    git(ahead, "checkout", "-qb", "feat")
    (ahead / "f.txt").write_text("f")
    git(ahead, "add", ".")
    git(ahead, "commit", "-qm", "f")
    gh = facts.GhCache(ahead / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    got = facts.git_facts(ahead, ahead / ".git", runner, gh, 0.0, False, True)
    assert got.work_branches[0].pr_state is None
    got = gf(ahead)
    assert got.unpushed == 1
    assert got.work_branches[0].name == "feat"
    assert not got.work_branches[0].on_remote
    assert not got.work_branches[0].merged

    git(ahead, "push", "-q", "origin", "feat")
    got = gf(ahead)
    assert got.unpushed == 0
    assert got.work_branches[0].on_remote

    git(ahead, "push", "-q", "origin", "feat:main")
    git(ahead, "push", "-q", "origin", "--delete", "feat")
    got = gf(ahead)
    assert got.unpushed == 0
    assert got.work_branches[0].merged and not got.work_branches[0].on_remote


def test_git_facts_remotes(
    make_clone: Callable[[str], Path], root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable remotes leave pushes unverified; gh supplies PR states.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """
    clone = make_clone("c")
    git(clone, "checkout", "-qb", "feat")
    git(clone, "remote", "add", "gh", "git@github.com:o/r.git")
    git(clone, "remote", "add", "gh2", "git@github.com:o/r.git")
    git(clone, "remote", "add", "a0", "https://github.com/o/zero")
    prs = [{"number": 3, "state": "MERGED", "headRefOid": "x"}]
    runner = FakeRunner(
        {
            "gh": Result(0, json.dumps(prs), ""),
            ("gh", "pr", "list", "--repo", "o/zero"): Result(0, "[]", ""),
            ("git", "-C", str(clone), "ls-remote", "--symref", "gh"): FAIL,
            ("git", "-C", str(clone), "ls-remote", "--symref", "https://github.com/o/r.git"): FAIL,
            ("git", "-C", str(clone), "ls-remote", "--symref", "https://github.com/o/zero.git"): OK,
            ("git", "-C", str(clone), "ls-remote", "--symref", "gh2"): FAIL,
        }
    )
    gh = facts.GhCache(tmp_path / "gh.json", runner)
    got = facts.git_facts(clone, clone / ".git", runner, gh, 0.0, False, True)
    assert got.unpushed == 0
    assert got.work_branches[0].pr_state == "MERGED"

    lonely = root / "lonely"
    lonely.mkdir()
    git(lonely, "init", "-q")
    got = facts.git_facts(lonely, lonely / ".git", runner, None, 0.0, False, True)
    assert got.unpushed == 0 and got.dirty is False
    (lonely / "a").write_text("a")
    git(lonely, "add", ".")
    git(lonely, "commit", "-qm", "a")
    got = facts.git_facts(lonely, lonely / ".git", runner, None, 0.0, False, True)
    assert got.unpushed == 1

    git(clone, "remote", "remove", "origin")
    git(clone, "remote", "remove", "a0")
    got = facts.git_facts(clone, clone / ".git", runner, None, 0.0, False, True)
    assert got.unpushed is None

    broken = FakeRunner({("git", "--no-optional-locks", "-C", str(lonely), "rev-list"): FAIL})
    got = facts.git_facts(lonely, lonely / ".git", broken, None, 0.0, False, True)
    assert got.unpushed is None
    nostatus = FakeRunner({("git", "--no-optional-locks", "-C", str(lonely), "status"): FAIL})
    got = facts.git_facts(lonely, lonely / ".git", nostatus, None, 0.0, False, True)
    assert got.dirty is None


def test_git_facts_worktree(make_clone: Callable[[str], Path], root: Path) -> None:
    """Linked worktrees know their main clone and vice versa.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    main = make_clone("main")
    wt = root / "wt"
    git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    runner = FakeRunner()
    gitdir = facts.git_dir_of(wt)
    assert gitdir is not None
    got = facts.git_facts(wt, gitdir, runner, None, 0.0, False, True)
    assert got.main_repo == str(main)
    assert facts.git_facts(main, main / ".git", runner, None, 0.0, False, True).linked_worktrees
    (gitdir / "commondir").unlink()
    got = facts.git_facts(wt, gitdir, runner, None, 0.0, False, False)
    assert got.main_repo == str(main)


def test_list_rescues(tmp_path: Path) -> None:
    """Rescues are read from manifests or directory names.

    Args:
        tmp_path: Pytest fixture.
    """
    assert facts.list_rescues(tmp_path / "none") == []
    rescue = tmp_path / ".rescue"
    (rescue / "a-2026-01-01").mkdir(parents=True)
    (rescue / "a-2026-01-01" / "manifest.json").write_text('{"clone": "a", "created": 5}')
    (rescue / "b-c-2026-02-02-1").mkdir()
    (rescue / "file").write_text("")
    found = facts.list_rescues(rescue)
    assert [(r.clone, r.created == 5, r.complete) for r in found] == [
        ("a", True, True),
        ("b-c", False, False),
    ]


def test_labelled_docker() -> None:
    """Labelled containers and networks are listed."""
    runner = FakeRunner(
        {
            ("docker", "ps"): Result(0, "c1\tid1\nc2\t\n", ""),
            ("docker", "network"): FAIL,
        }
    )
    got = facts.labelled_docker(runner)
    assert [(d.kind, d.name, d.ledger_id) for d in got] == [("docker-container", "c1", "id1")]


def test_probe_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe reports connection success.

    Args:
        monkeypatch: Pytest fixture.
    """

    class Conn:
        def __enter__(self) -> Conn:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("agent_sweeper.facts.socket.create_connection", lambda *a, **k: Conn())
    assert facts.probe_online()

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("down")

    monkeypatch.setattr("agent_sweeper.facts.socket.create_connection", refuse)
    assert not facts.probe_online()


def test_clone_facts(make_clone: Callable[[str], Path], root: Path) -> None:
    """Clone facts combine walk, git, ledger, use and rescues.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    runner = FakeRunner()
    clone = make_clone("c")
    (clone / "dist").mkdir()
    (clone / "dist" / "tracked").write_text("x")
    git(clone, "add", "-f", "dist/tracked")
    git(clone, "commit", "-qm", "dist")
    (clone / "node_modules").mkdir()
    (clone / "build").mkdir()
    (clone / "build" / "new_module.py").write_text("x")
    (clone / "site").mkdir()
    (clone / ".git" / "info" / "exclude").write_text("site/\n")
    rescues = [
        RescueInfo("/x", "c", 7.0),
        RescueInfo("/y", "other", 9.0),
        RescueInfo("/z", "c", 99.0, complete=False),
        RescueInfo("/w", "c", 98.0, fingerprint="refs changed since"),
    ]
    got = facts.clone_facts(clone, None, set(), rescues, runner, None, time.time(), False, True)
    assert got.kind == "git" and not got.in_use and got.rescued_at == 7.0
    assert {Path(o.path).name: o.keep for o in got.build_outputs} == {
        "dist": "build output holds tracked files",
        "node_modules": "",
        "build": "build output not ignored by git (may hold source)",
        "site": "",
    }
    assert got.git is not None and got.git.dirty and len(got.git.fingerprint) == 64
    nols = FakeRunner({("git", "--no-optional-locks", "-C", str(clone), "ls-files"): FAIL})
    got = facts.clone_facts(clone, None, set(), [], nols, None, time.time(), False, True)
    assert all(o.keep.startswith("git ls-files failed") for o in got.build_outputs)
    busy = facts.clone_facts(clone, None, {5, 0}, [], runner, None, time.time(), False, True)
    assert busy.in_use and busy.in_use_reason == "pids 0,5" and busy.git is None
    entry = Entry(id="c", clone="c", state="running")
    running = facts.clone_facts(clone, entry, set(), [], runner, None, time.time(), False, True)
    assert running.in_use and running.rescued_at is None
    plain = root / "plain"
    plain.mkdir()
    got = facts.clone_facts(plain, None, set(), [], runner, None, time.time(), False, True)
    assert got.kind == "nogit" and got.git is None
    wt = root / "wt"
    git(clone, "worktree", "add", "-q", "-b", "side", str(wt))
    got = facts.clone_facts(wt, None, set(), [], runner, None, time.time(), False, True)
    assert got.kind == "worktree"


def test_read_state(root: Path) -> None:
    """The state file is optional.

    Args:
        root: Clone root.
    """
    cfg = Config(root=root)
    assert facts.read_state(cfg) == {}
    cfg.ledger_dir.mkdir()
    (cfg.ledger_dir / "state.json").write_text('{"last_notify": 3}')
    assert facts.read_state(cfg) == {"last_notify": 3}


def test_collect(
    make_clone: Callable[[str], Path], root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collection walks every clone dir and resolves missing ledger paths.

    Args:
        make_clone: Clone factory.
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    make_clone("a")
    (root / "notes.txt").write_text("")
    (root / ".rescue").mkdir()
    (root / "link").symlink_to(root / "a")
    ledger = Ledger(root)
    ledger.save(Entry(id="a", clone="a", clone_path=str(root / "a")))
    ledger.save(Entry(id="gone", clone="gone", clone_path=str(root / "gone")))
    ledger.save(Entry(id="nopath", clone="nopath"))
    runner = FakeRunner({"docker": OK})
    cfg = Config(root=root, dry_run=True)
    world, gh = facts.collect(
        cfg, now=1.0, proc_scan=lambda: [], runner=runner, online=False, disk_pct=50.0
    )
    assert [c.name for c in world.clones] == ["a"]
    assert world.missing_paths == frozenset({str(root / "gone")})
    assert world.disk_pct == 50.0 and not gh.enabled

    monkeypatch.setattr(facts, "scan_proc", lambda: [(str(root / "a"), 9)])
    monkeypatch.setattr(facts, "docker_mounts", lambda r: [])
    monkeypatch.setattr(facts, "probe_online", lambda: False)
    world, _ = facts.collect(cfg, runner=runner)
    assert world.clones[0].in_use
    assert 0 < world.disk_pct <= 100


def test_git_facts_behind_without_fetch(
    make_clone: Callable[[str], Path], remote: Path, tmp_path: Path
) -> None:
    """Without a fetch, tracking refs count only if GitHub confirms them.

    Regression tests: the remote tips from ls-remote are missing locally, so
    the tracking ref proves the local commits are on the remote, but only if
    the current tip still contains it (no force-push or recreated branch).

    Args:
        make_clone: Clone factory.
        remote: Bare remote.
        tmp_path: Pytest fixture.
    """
    behind = make_clone("behind")
    other = make_clone("other")
    git(other, "commit", "--allow-empty", "-qm", "advance")
    git(other, "push", "-q", "origin", "main")
    git(behind, "remote", "set-url", "origin", "git@github.com:o/r.git")
    listing = Result(0, git(behind, "ls-remote", "--symref", str(remote)), "")
    https = ("git", "-C", str(behind), "ls-remote", "--symref", "https://github.com/o/r.git")
    cases = [("ahead\n", True, 0), ("diverged\n", True, 1), ("ahead\n", False, 1)]
    for n, (status, with_gh, unpushed) in enumerate(cases):
        runner = FakeRunner(
            {
                https: listing,
                ("git", "-C", str(behind), "-c"): FAIL,
                ("gh", "api"): Result(0, status, ""),
                ("gh", "pr"): Result(0, "[]", ""),
            }
        )
        gh = facts.GhCache(tmp_path / f"{n}.json", runner) if with_gh else None
        got = facts.git_facts(behind, behind / ".git", runner, gh, 0.0, n == 0, True)
        assert got.unpushed == unpushed, (status, with_gh)


def test_git_facts_local_tag(make_clone: Callable[[str], Path]) -> None:
    """Commits kept only by a local tag are unpushed.

    Args:
        make_clone: Clone factory.
    """
    clone = make_clone("c")
    git(clone, "commit", "--allow-empty", "-qm", "tagged only")
    git(clone, "tag", "keep")
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    got = facts.git_facts(clone, clone / ".git", FakeRunner(), None, 0.0, False, True)
    assert got.unpushed == 1
    broken = FakeRunner({("git", "--no-optional-locks", "-C", str(clone), "for-each-ref"): FAIL})
    got = facts.git_facts(clone, clone / ".git", broken, None, 0.0, False, True)
    assert got.unpushed is None


def test_gh_contains(tmp_path: Path) -> None:
    """Compare answers are cached for good; failures stay unknown.

    Args:
        tmp_path: Pytest fixture.
    """
    runner = FakeRunner({("gh", "api"): Result(0, "ahead\n", "")})
    cache = facts.GhCache(tmp_path / "c.json", runner)
    assert cache.contains("o/r", "a", "b") is True
    assert cache.contains("o/r", "a", "b") is True
    assert cache.calls == 1
    behind = facts.GhCache(
        tmp_path / "x.json", FakeRunner({("gh", "api"): Result(0, "diverged", "")})
    )
    assert behind.contains("o/r", "a", "b") is False
    failing = facts.GhCache(tmp_path / "x.json", FakeRunner({"gh": FAIL}))
    assert failing.contains("o/r", "a", "b") is None
    assert (
        facts.GhCache(tmp_path / "x.json", runner, enabled=False).contains("o/r", "a", "b") is None
    )


def test_git_facts_pr_head_on_github(make_clone: Callable[[str], Path], tmp_path: Path) -> None:
    """Local commits inside a PR head that is not local count as pushed.

    Regression test: after "update branch" on GitHub the PR head has a
    commit the clone lacks, so only GitHub can prove the tip is on it.

    Args:
        make_clone: Clone factory.
        tmp_path: Pytest fixture.
    """
    clone = make_clone("c")
    git(clone, "checkout", "-qb", "feat")
    git(clone, "commit", "--allow-empty", "-qm", "work")
    git(clone, "remote", "add", "gh", "git@github.com:o/r.git")
    prs = [{"number": 1, "state": "MERGED", "headRefOid": "f" * 40}]
    answers: dict[object, Result] = {
        ("gh", "pr", "list"): Result(0, json.dumps(prs), ""),
        ("git", "-C", str(clone), "ls-remote", "--symref", "gh"): FAIL,
        ("git", "-C", str(clone), "ls-remote", "--symref", "https://github.com/o/r.git"): FAIL,
    }
    for status, unpushed in (("ahead\n", 0), ("diverged\n", 1)):
        runner = FakeRunner({**answers, ("gh", "api"): Result(0, status, "")})
        gh = facts.GhCache(tmp_path / f"{unpushed}.json", runner)
        got = facts.git_facts(clone, clone / ".git", runner, gh, 0.0, False, True)
        assert got.unpushed == unpushed
        assert got.work_branches[0].pr_state == "MERGED"


def test_fingerprint_and_last_change(make_clone: Callable[[str], Path], root: Path) -> None:
    """Fingerprints change with local refs; last_change matches the walk.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    clone = make_clone("c")
    before = facts.fingerprint(clone, FakeRunner())
    git(clone, "commit", "--allow-empty", "-qm", "new")
    assert facts.fingerprint(clone, FakeRunner()) != before
    broken = FakeRunner({("git", "--no-optional-locks", "-C", str(clone), "for-each-ref"): FAIL})
    assert facts.fingerprint(clone, broken) == ""
    assert facts.last_change(clone, FakeRunner()) > 0
    plain = root / "plain"
    plain.mkdir()
    assert facts.last_change(plain, FakeRunner()) == os.lstat(plain).st_mtime
    old = time.time() - 30 * config.DAY
    (clone / "build").mkdir()
    (clone / "build" / "gen.sh").write_text("x")
    for dirpath, dirs, files in os.walk(clone):
        for name in [*dirs, *files]:
            os.utime(os.path.join(dirpath, name), (old, old))
    os.utime(clone, (old, old))
    assert facts.last_change(clone, FakeRunner()) < old + 10
    (clone / "build" / "gen.sh").write_text("edited")  # kept dir: not ignored
    assert facts.last_change(clone, FakeRunner()) > old + 10
    (clone / "build" / "dep").mkdir()
    (clone / "build" / "dep" / ".git").mkdir()
    tree = facts.scan_tree(clone, True)
    assert tree.nested and tree.nested_outputs == [str(clone / "build")]
    assert facts.activity(clone, tree, FakeRunner())[2][str(clone / "build")] == (
        "build output holds a git repository"
    )


def test_git_facts_orphan_head(make_clone: Callable[[str], Path]) -> None:
    """An unborn HEAD next to existing branches still checks the branches.

    Args:
        make_clone: Clone factory.
    """
    clone = make_clone("c")
    git(clone, "checkout", "-q", "--orphan", "fresh")
    git(clone, "rm", "-rqf", ".")
    got = facts.git_facts(clone, clone / ".git", FakeRunner(), None, 0.0, False, True)
    assert got.unpushed == 0


def test_git_below_caches(
    make_clone: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository inside a cache dir keeps the dir and marks the clone nested.

    Regression test: caches were not walked, so an editable install's
    checkout under ``.venv/src`` was deleted with the cache.

    Args:
        make_clone: Clone factory.
        monkeypatch: Pytest fixture.
    """
    clone = make_clone("c")
    (clone / ".venv" / "src" / "pkg" / ".git").mkdir(parents=True)
    (clone / ".cache" / "x").mkdir(parents=True)
    tree = facts.scan_tree(clone, True)
    venv = str(clone / ".venv")
    assert tree.nested and tree.nested_outputs == [venv]
    assert (
        facts.activity(clone, tree, FakeRunner())[2][venv] == "build output holds a git repository"
    )
    plain = make_clone("plain")
    (plain / ".venv" / "src" / ".git").mkdir(parents=True)
    shutil.rmtree(plain / ".git")
    assert facts.activity(plain, facts.scan_tree(plain, False), FakeRunner())[2] == {
        str(plain / ".venv"): "build output holds a git repository"
    }

    real = os.scandir

    def flaky(path: str) -> object:
        if path.endswith("x"):
            raise OSError("gone")
        return real(path)

    monkeypatch.setattr("agent_sweeper.facts.os.scandir", flaky)
    assert not facts.has_git_below(str(clone / ".cache"))


def test_git_below_stat_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries that cannot be stat'ed are skipped.

    Args:
        tmp_path: Temporary directory.
        monkeypatch: Pytest fixture.
    """

    class Broken:
        name = "d"
        path = str(tmp_path / "d")

        def is_dir(self, follow_symlinks: bool = True) -> bool:
            raise OSError("gone")

    class Listing:
        def __enter__(self) -> list[Broken]:
            return [Broken()]

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("agent_sweeper.facts.os.scandir", lambda path: Listing())
    assert not facts.has_git_below(str(tmp_path))


def test_gh_cache_final_but_moved(tmp_path: Path) -> None:
    """A final cached answer is re-queried when no PR head is the current tip.

    Regression test: a reused branch name with a new commit kept the old
    merged PR's verdict for 30 days.

    Args:
        tmp_path: Temporary directory.
    """
    merged = '[{"number": 1, "state": "MERGED", "headRefOid": "old"}]'
    runner = FakeRunner({"gh": Result(0, merged, "")})
    cache = facts.GhCache(tmp_path / "c.json", runner)
    assert cache.prs("o/r", "b", 0.0, "old")
    calls = len(runner.calls)
    assert cache.prs("o/r", "b", 10.0, "old")
    assert len(runner.calls) == calls
    cache.prs("o/r", "b", 10.0, "new")
    assert len(runner.calls) == calls + 1


def test_collect_unreadable_and_trash(make_clone: Callable[[str], Path], root: Path) -> None:
    """Unreadable ledger entries and trash leftovers reach the world.

    Args:
        make_clone: Clone factory.
        root: Clone root.
    """
    make_clone("a")
    ledger = Ledger(root)
    ledger.dir.mkdir()
    (ledger.dir / "a.json").write_text("{not json")
    (root / ".trash" / "x-1").mkdir(parents=True)
    cfg = Config(root=root, dry_run=True)
    world, _ = facts.collect(
        cfg, now=1.0, proc_scan=lambda: [], runner=FakeRunner({"docker": OK}), online=False,
        disk_pct=50.0,
    )  # fmt: skip
    assert world.unreadable == frozenset({"a"})
    assert [p for p, _ in world.trash] == [str(root / ".trash" / "x-1")]
    assert facts.list_trash(root / "none") == []


def test_uv_git_cache_ignored(make_clone: Callable[[str], Path]) -> None:
    """Git checkout caches of uv do not count as nested repositories.

    Args:
        make_clone: Clone factory.
    """
    clone = make_clone("c")
    (clone / ".uv-cache" / "git-v0" / "checkouts" / "x" / ".git").mkdir(parents=True)
    (clone / ".uvc" / "git-v0" / "db" / ".git").mkdir(parents=True)
    (clone / ".cache" / "uv" / "git-v0" / "y" / ".git").mkdir(parents=True)
    (clone / ".tools" / "uv" / "src" / ".git").mkdir(parents=True)
    (clone / ".uv-cache" / "sdists-v9").mkdir()
    (clone / ".uv-cache" / "sdists-v9" / ".git").write_text("")  # uv's marker file
    (clone / ".npm-cache" / "x").mkdir(parents=True)
    (clone / ".npm-cache" / "x" / ".git").write_text("gitdir: /elsewhere\n")
    tree = facts.scan_tree(clone, True)
    assert sorted(tree.nested_outputs) == [str(clone / ".npm-cache"), str(clone / ".tools")]
    assert facts.uv_git_cache("/r/c/.cache/uv") and not facts.uv_git_cache("/r/c/git-v0")


def test_secret_files(make_clone: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Only secret-like files that git does not hold unchanged are reported.

    Args:
        make_clone: Clone factory.
        monkeypatch: Pytest fixture.
    """
    clone = make_clone("c")
    assert facts.secret_files(clone, FakeRunner()) == []
    (clone / "tracked.pem").write_text("x")
    (clone / "changed.key").write_text("x")
    git(clone, "add", "tracked.pem", "changed.key")
    git(clone, "commit", "-qm", "keys")
    (clone / "changed.key").write_text("y")
    (clone / ".env").write_text("x")
    (clone / ".gitignore").write_text(".kube/\nbuild/\n")
    (clone / ".kube").mkdir()
    (clone / ".kube" / "config").write_text("x")
    (clone / "node_modules" / ".env").parent.mkdir()
    (clone / "node_modules" / ".env").write_text("x")  # disposable cache: ignored
    (clone / "build").mkdir()
    (clone / "build" / "id_rsa").write_text("x")  # ignored build output: deleted anyway
    assert facts.secret_files(clone, FakeRunner()) == [".env", ".kube/config", "changed.key"]
    (clone / "dist").mkdir()  # not ignored: kept and walked
    (clone / "dist" / "token.pem").write_text("x")
    assert "dist/token.pem" in (facts.secret_files(clone, FakeRunner()) or [])
    failing = FakeRunner(
        {("git", "--no-optional-locks", "-C", str(clone), "ls-files", "-z", "--"): FAIL}
    )
    assert facts.unsaved_secrets(clone, [str(clone / ".env")], failing) is None
    no_head = FakeRunner({("git", "--no-optional-locks", "-C", str(clone), "diff-index"): FAIL})
    assert facts.unsaved_secrets(clone, [str(clone / "tracked.pem")], no_head) == ["tracked.pem"]
    plain = clone.parent / "plain"
    plain.mkdir()
    (plain / ".env").write_text("x")
    assert facts.activity(plain, facts.scan_tree(plain, False), FakeRunner())[3] == [
        str(plain / ".env")
    ]


def test_cache_exempt_and_parse_status_tracked() -> None:
    """Only untracked cache dirs are exempt from the dirty check.

    Regression test: new files in a tracked .tools/ did not count.
    """
    assert facts.cache_exempt(".venv/lib/x.py", frozenset())
    assert not facts.cache_exempt(".tools/new.sh", {".tools"})
    assert facts.cache_exempt(".tools/node_modules/x.js", {".tools"})
    assert not facts.cache_exempt(".venv/x", None)
    assert facts.parse_status("?? .tools/new.sh\0", {".tools"})
    assert not facts.parse_status("?? .tools/new.sh\0", frozenset())
    assert facts.parse_status("?? .venv/x\0", None)
    assert facts.parse_status(" M .venv/tracked.py\0", frozenset())


def test_committed_secrets(make_clone: Callable[[str], Path], remote: Path) -> None:
    """Secret-like paths in unpushed commits and the stash are found.

    Args:
        make_clone: Clone factory.
        remote: Bare remote.
    """
    clone = make_clone("c")
    assert facts.committed_secrets(clone, FakeRunner(), pushed(clone)) == []
    (clone / ".env").write_text("x")
    git(clone, "add", ".env")
    git(clone, "stash", "-q")
    assert facts.committed_secrets(clone, FakeRunner(), pushed(clone)) == [".env"]
    git(clone, "stash", "drop", "-q")
    (clone / "app.key").write_text("x")
    git(clone, "add", "app.key")
    git(clone, "commit", "-qm", "key")
    assert facts.committed_secrets(clone, FakeRunner(), pushed(clone)) == ["app.key"]
    git(clone, "push", "-q", "origin", "HEAD")
    assert facts.committed_secrets(clone, FakeRunner(), pushed(clone)) == []
    gh = facts.GhCache(clone / "gh.json", FakeRunner({"gh": Result(0, "[]", "")}))
    gitdir = clone / ".git"
    online = facts.git_facts(clone, gitdir, FakeRunner(), gh, 0.0, False, True)
    assert online.bundle_secrets == [] and online.pushed
    # Offline nothing is verified as pushed, so all history counts.
    offline = facts.git_facts(clone, gitdir, FakeRunner(), gh, 0.0, False, False)
    assert offline.pushed == [] and offline.bundle_secrets == ["app.key"]
    prefix = ("git", "--no-optional-locks", "-C", str(clone))
    assert facts.committed_secrets(clone, FakeRunner({(*prefix, "for-each-ref"): FAIL}), []) is None
    assert facts.committed_secrets(clone, FakeRunner({(*prefix, "rev-list"): FAIL}), []) is None
    empty = clone.parent / "empty"
    git(clone.parent, "init", "-q", str(empty))
    assert facts.committed_secrets(empty, FakeRunner(), []) == []


def test_trash_time() -> None:
    """Trash entry names carry the deletion time."""
    assert facts.trash_time("c-2000000000") == 2.0
    assert facts.trash_time("c-3000000000-kept") == 3.0
    assert facts.trash_time("odd") == float("inf")
