"""End-to-end tests of the ``agent-sweep`` command on fixture roots."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_sweeper import config, sweep
from agent_sweeper.config import Config
from agent_sweeper.execute import Outcome
from agent_sweeper.ledger import Entry, Ledger, Resource
from agent_sweeper.rules import KEPT_REASON, SECRETS_REASON, SECRETS_UNKNOWN, Action, World
from agent_sweeper.util import Result

from .conftest import FakeRunner, age, git

OK = Result(0, "", "")


@pytest.fixture()
def populated(make_clone: Callable[[str], Path], root: Path) -> Path:
    """Build a root with one clone per rule.

    Args:
        make_clone: Clone factory.
        root: Clone root.

    Returns:
        The root.
    """
    merged = make_clone("merged")
    (merged / "node_modules").mkdir()
    (merged / "node_modules" / "x").write_bytes(b"x" * 4096)
    age(merged, 3 * config.HOUR)
    Ledger(root).save(
        Entry(
            id="merged",
            clone="merged",
            clone_path=str(merged),
            state="merged",
            resources=[Resource("docker-container", "db")],
        )
    )
    dirty = make_clone("dirty")
    (dirty / "README.md").write_text("work in progress\n")
    (dirty / ".venv").mkdir()
    age(dirty, 3 * config.HOUR)
    busy = make_clone("busy")
    (busy / "node_modules").mkdir()
    age(busy, 30 * config.DAY)
    old = make_clone("old")
    git(old, "checkout", "-qb", "feat")
    (old / "f").write_text("f")
    git(old, "add", ".")
    git(old, "commit", "-qm", "local only")
    age(old, 20 * config.DAY)
    Ledger(root).save(Entry(id="ghost", clone="ghost", clone_path=str(root / "ghost")))
    return root


def runner() -> FakeRunner:
    """Return a runner with docker and notify faked.

    Returns:
        The runner.
    """
    return FakeRunner({"docker": OK, "notify-cmd": OK})


def test_dry_run_changes_nothing(populated: Path, tmp_path: Path) -> None:
    """A dry run writes only its plan.

    Args:
        populated: Fixture root.
        tmp_path: Pytest fixture.
    """
    plan_file = tmp_path / "out" / "plan.txt"
    cfg = Config(root=populated, dry_run=True, cache_path=tmp_path / "gh.json")
    busy = lambda: [(str(populated / "busy" / "x"), 42)]  # noqa: E731
    text = sweep.sweep(cfg, plan_file, online=True, proc_scan=busy, disk_pct=50.0, runner=runner())
    assert "DRY RUN" in text
    body = plan_file.read_text()
    assert "delete-clone" in body and "skip: in use" in body
    assert (populated / "merged").exists()
    assert not (populated / ".ledger" / "sweep.log").exists()
    assert not (populated / ".rescue").exists()
    sweep.sweep(cfg, None, online=True, proc_scan=busy, disk_pct=50.0, runner=runner())
    assert (tmp_path / "gh.json").exists()
    plain = Config(root=populated, dry_run=True)
    sweep.sweep(plain, None, online=True, proc_scan=busy, disk_pct=50.0, runner=runner())
    assert not plain.gh_cache.exists()


@pytest.mark.parametrize("bundle", [False, True])
def test_real_run(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle: bool
) -> None:
    """A real run follows every rule and logs every decision.

    Args:
        populated: Fixture root.
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
        bundle: Value of ``BUNDLE_SAFE_CLONES``.
    """
    monkeypatch.setattr(config, "BUNDLE_SAFE_CLONES", bundle)
    cfg = Config(root=populated, fetch=True, cache_path=tmp_path / "gh.json", notify="notify-cmd")
    busy = lambda: [(str(populated / "busy"), 42)]  # noqa: E731
    fake = runner()
    text = sweep.sweep(cfg, online=True, proc_scan=busy, disk_pct=93.0, runner=fake)
    assert not (populated / "merged").exists()
    assert not (populated / "old").exists()
    assert (populated / "dirty" / "README.md").exists()
    assert not (populated / "dirty" / ".venv").exists()
    assert (populated / "busy" / "node_modules").exists()
    rescues = sorted(p.name.rsplit("-", 3)[0] for p in (populated / ".rescue").iterdir())
    assert rescues == (["merged", "old"] if bundle else ["old"])
    ledger = Ledger(populated)
    merged = ledger.load("merged")
    ghost = ledger.load("ghost")
    assert merged is not None and merged.state == "cleaned"
    assert merged.resources[0].removed is not None
    assert ghost is not None and ghost.state == "cleaned"
    assert ["docker", "rm", "-f", "db"] in fake.calls
    assert any(c[0] == "notify-cmd" for c in fake.calls)
    log = [json.loads(line) for line in (cfg.ledger_dir / "sweep.log").read_text().splitlines()]
    assert {rec["action"] for rec in log} >= {"delete-clone", "rescue", "skip", "set-state"}
    assert (cfg.ledger_dir / "last-sweep.txt").read_text() == text
    again = sweep.sweep(cfg, online=True, proc_scan=busy, disk_pct=93.0, runner=fake)
    assert "delete-clone" not in again and "notify" not in again


def test_summary_and_details() -> None:
    """Summaries total by category and list failures; details sort."""
    world = World(now=0.0)
    outcomes = [
        Outcome(Action("delete-clone", "a", "/r/a", "safe", 2 * sweep.GB), "done"),
        Outcome(Action("rescue", "a", "/r/a", "unsafe", sweep.GB), "failed", "boom"),
        Outcome(Action("delete-build-output", "b", "/r/b/dist", "idle 3h"), "skipped", "in use"),
        Outcome(Action("skip", "c", "/r/c", "clone kept: unsafe (dirty); idle 2.0d"), "skipped"),
        Outcome(Action("skip", "c", "/r/c/dist", "build output holds tracked files"), "skipped"),
    ]
    text = sweep.summarize(outcomes, world, False, False)
    assert "freed 2.0 GB" in text and "OFFLINE" in text and "FAILED rescue a: boom" in text
    assert "skip: clone kept: unsafe" in text
    assert "[skipped: in use]" in text
    lines = sweep.details(outcomes).splitlines()
    assert lines[1].startswith("delete-clone") and "/r/c/dist" in lines[-1]
    assert sweep.category(Action("skip", "", "", "changed 3.5h ago")) == "skip: changed Nh ago"


def test_log_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The log rotates once it exceeds its size limit.

    Args:
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """
    log = tmp_path / "l" / "sweep.log"
    monkeypatch.setattr(sweep, "LOG_MAX_BYTES", 10)
    out = [Outcome(Action("skip", "c", "/r/c", "x"), "skipped")]
    sweep.append_log(log, out)
    sweep.append_log(log, out)
    assert log.with_suffix(".log.1").exists()


def test_path_in_use(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live check uses /proc and docker unless a scanner is given.

    Args:
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    scan = lambda: [(str(root / "a" / "f"), 1)]  # noqa: E731
    assert sweep.path_in_use(str(root / "a"), scan)
    assert not sweep.path_in_use(str(root / "ab"), scan)
    monkeypatch.setattr(sweep, "scan_proc", lambda: [(str(root / "b"), 1)])
    monkeypatch.setattr(sweep, "docker_mounts", lambda: [])
    assert sweep.path_in_use(str(root / "b"))


def test_main(populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command checks the root, honours the lock and runs.

    Args:
        populated: Fixture root.
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """
    calls: list[tuple[Config, Path | None, bool | None]] = []

    def fake_sweep(cfg: Config, plan_out: Path | None = None, online: bool | None = None) -> str:
        calls.append((cfg, plan_out, online))
        return "ok\n"

    monkeypatch.setattr(sweep, "sweep", fake_sweep)
    assert sweep.main(["--root", str(tmp_path / "missing")]) == 1
    assert sweep.main(["--root", str(populated), "--dry-run", "--plan-out", "p", "--offline"]) == 0
    assert calls[-1][0].dry_run and calls[-1][1] == Path("p") and calls[-1][2] is False
    assert sweep.main(["--root", str(populated), "--no-fetch"]) == 0
    assert not calls[-1][0].fetch and calls[-1][2] is None
    with open(populated / ".ledger" / "sweep.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert sweep.main(["--root", str(populated)]) == 0
    assert len(calls) == 2


def test_summary_lists_secret_holders() -> None:
    """Clones kept for secret-like files are listed once in the summary."""
    outcomes = [
        Outcome(Action("skip", "a", "/r/a", SECRETS_REASON), "skipped"),
        Outcome(Action("skip", "b", "/r/b", SECRETS_UNKNOWN), "skipped"),
        Outcome(Action("delete-clone", "c", "/r/c", "safe"), "skipped", SECRETS_REASON),
        Outcome(Action("skip", "d", "/r/d", "clone kept: pushed"), "skipped"),
    ]
    text = sweep.summarize(outcomes, World(now=0.0), False, True)
    assert "Kept for secret-like files (tell the user once): a, b, c" in text
    assert "secret-like" not in sweep.summarize(outcomes[3:], World(now=0.0), False, True)


def test_summary_lists_kept_trash() -> None:
    """Trash entries kept after a failed delete are listed for the user."""
    outcomes = [Outcome(Action("skip", "", "/r/.trash/c-1-kept", KEPT_REASON), "skipped")]
    text = sweep.summarize(outcomes, World(now=0.0), False, True)
    assert "review by hand (tell the user once): /r/.trash/c-1-kept" in text
