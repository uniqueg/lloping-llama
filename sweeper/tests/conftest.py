"""Shared fixtures: temporary clone roots with real git repositories."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_sweeper.util import Result, run

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(path: Path, *args: str) -> str:
    """Run git in a fixture repository and fail the test on errors.

    Args:
        path: Repository directory.
        *args: Git arguments.

    Returns:
        Standard output.
    """
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
         *args],
        capture_output=True, text=True, env=env, check=True,
    )  # fmt: skip
    return proc.stdout


def age(path: Path, seconds: float) -> None:
    """Set the mtime of a tree (and git activity files) into the past.

    Args:
        path: Directory to age.
        seconds: How far back.
    """
    ts = time.time() - seconds
    for dirpath, dirs, files in os.walk(path):
        for name in [*files, *dirs]:
            os.utime(os.path.join(dirpath, name), (ts, ts), follow_symlinks=False)
    os.utime(path, (ts, ts))


@pytest.fixture(autouse=True)
def _git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate git from the user's configuration.

    Args:
        monkeypatch: Pytest fixture.
    """
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "main")


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """Return an empty clone root.

    Args:
        tmp_path: Pytest fixture.

    Returns:
        The root directory.
    """
    path = tmp_path / "agent-clones"
    path.mkdir()
    return path


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    """Return a bare remote with one commit on ``main``.

    Args:
        tmp_path: Pytest fixture.

    Returns:
        Path of the bare repository.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    (seed / "README.md").write_text("hi\n")
    (seed / ".gitignore").write_text("node_modules/\n.venv/\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "init")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


@pytest.fixture()
def make_clone(root: Path, remote: Path) -> Callable[[str], Path]:
    """Return a factory for clones of the fixture remote.

    Args:
        root: Clone root.
        remote: Bare remote.

    Returns:
        ``factory(name) -> clone path``.
    """

    def factory(name: str) -> Path:
        path = root / name
        git(root, "clone", "-q", str(remote), str(path))
        return path

    return factory


class FakeRunner:
    """Command runner that answers some programs and passes git through.

    Attributes:
        answers: Results by program name (first argument) or by full
            command prefix tuple.
        calls: Commands seen.
    """

    def __init__(self, answers: dict[object, Result] | None = None) -> None:
        """Initialise the runner.

        Args:
            answers: Canned results.
        """
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float, **kwargs: object) -> Result:
        """Answer or delegate a command.

        Args:
            cmd: Command.
            timeout: Timeout.
            **kwargs: Passed to :func:`agent_sweeper.util.run`.

        Returns:
            The canned or real result.
        """
        self.calls.append(list(cmd))
        for n in range(len(cmd), 0, -1):
            key = tuple(cmd[:n])
            if key in self.answers:
                return self.answers[key]
        if cmd[0] in self.answers:
            return self.answers[cmd[0]]
        if cmd[0] == "git":
            return run(cmd, timeout, **kwargs)  # type: ignore[arg-type]
        return Result(127, "", "not faked")


def pushed(clone: Path) -> list[str]:
    """Return the remote-tracking tips of a fixture clone (all verified pushed).

    Args:
        clone: Clone directory.

    Returns:
        Commit SHAs.
    """
    return git(clone, "for-each-ref", "--format=%(objectname)", "refs/remotes").split()
