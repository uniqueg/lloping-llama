"""Tests of :mod:`agent_sweeper.util`."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sweeper import util


def test_run_ok_and_stdin() -> None:
    """Commands run with input and report their output."""
    res = util.run(["cat"], timeout=5, stdin="hello")
    assert res.ok
    assert res.out == "hello"


def test_run_failures(tmp_path: Path) -> None:
    """Timeouts, missing programs and bad directories never raise.

    Args:
        tmp_path: Pytest fixture.
    """
    assert util.run(["sleep", "5"], timeout=0.1).rc == 124
    assert util.run(["no-such-program-xyz"], timeout=5).rc == 127
    assert util.run(["true"], timeout=5, cwd=tmp_path / "missing").rc == 127
    assert util.run(["false"], timeout=5, env={"X": "1"}).rc == 1


def test_which() -> None:
    """Programs on the PATH are found."""
    assert util.which("sh")
    assert not util.which("no-such-program-xyz")


def test_du(tmp_path: Path) -> None:
    """Disk usage counts files below, not symlink targets or unreadable dirs.

    Args:
        tmp_path: Pytest fixture.
    """
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "f").write_bytes(b"x" * 10000)
    (tmp_path / "d" / "link").symlink_to("/usr")
    locked = tmp_path / "d" / "locked"
    locked.mkdir()
    (locked / "g").write_bytes(b"x" * 10000)
    locked.chmod(0)
    try:
        size = util.du(tmp_path / "d")
    finally:
        locked.chmod(0o755)
    assert 10000 <= size < 10_000_000
    assert util.du(tmp_path / "missing") == 0
    assert util.du(tmp_path / "d" / "e" / "f") >= 10000


def test_du_vanishing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files that vanish during the walk count as zero.

    Args:
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """
    (tmp_path / "a").write_text("x")
    own = os.lstat(tmp_path).st_blocks * 512

    class Entry:
        name = "a"
        path = str(tmp_path / "a")

        def is_dir(self, follow_symlinks: bool = True) -> bool:
            return False

        def stat(self, follow_symlinks: bool = True) -> os.stat_result:
            raise OSError("gone")

    class It:
        def __enter__(self) -> list[Entry]:
            return [Entry()]

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("agent_sweeper.util.os.scandir", lambda p: It())
    assert util.du(tmp_path) == own


def test_disk_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Usage is computed like df, and an empty file system is 0%.

    Args:
        monkeypatch: Pytest fixture.
    """
    assert 0 < util.disk_percent("/") <= 100
    fake = SimpleNamespace(f_blocks=0, f_bfree=0, f_frsize=4096, f_bavail=0)
    monkeypatch.setattr("agent_sweeper.util.os.statvfs", lambda p: fake)
    assert util.disk_percent("/") == 0.0


def test_within(tmp_path: Path) -> None:
    """Only paths strictly below the parent count.

    Args:
        tmp_path: Pytest fixture.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ln").symlink_to("/etc")
    assert util.within(tmp_path / "a", tmp_path)
    assert util.within(tmp_path / "a" / "ln", tmp_path)
    assert not util.within(tmp_path, tmp_path)
    assert not util.within("/etc/passwd", tmp_path)
    assert not util.within(tmp_path / "a" / "ln" / "passwd", tmp_path)
