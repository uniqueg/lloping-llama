"""Fixtures for the profile-gate tests: git repositories and profile files.

The fixtures run git locally only (``git init``, ``git remote add``, ``git
config``); no test calls gh or the network.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

PROFILES_LIST = """\
version: 1
unknown: ask
profiles:
  owner-sole: {description: no restrictions}
  maintainer:
    description: within responsibilities
repositories:
  - match: example-org/example-site
    profile: owner-blessed-maintainer
    maintainer: alice
  - match: example-org/example-sdk
    profile: owner-shared
    co_owners: [bob, dave]
  - match: other-org/example-service
    profile: maintainer
    owner: carol
    responsibilities: [CI and release workflows]
  - match: other-org/merge-ok
    profile: maintainer
    owner: carol
    allowed_actions: [merge]
  - match: other-org/example-tool
    profile: developer
    owner: carol
  - match: upstream-org/*
    profile: contributor
  - match: you/*
    profile: owner-sole
  - match: example-org/*
    profile: owner-sole
  - match: gitlab.example.com/team/project
    profile: contributor
"""

PROFILES_MAPPING = """\
version: 1
profiles:
  developer:
    summary: may merge here
    allowed_actions: [merge]
repos:
  upstream-org/*:
    profile: owner-sole
  upstream-org/api:
    profile: contributor
  other-org/blessed:
    profile: owner-blessed-maintainer
  other-org/shared:
    profile: owner-shared
  other-org/maintained:
    profile: maintainer
  other-org/dev:
    profile: developer
  other-org/dev-nobody:
    profile: developer
"""


def git(cwd: Path, *args: str) -> str:
    """Run git in a directory.

    Args:
        cwd: The directory.
        *args: The git arguments.

    Returns:
        The stripped output.
    """
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate git and the state directory from the user's configuration.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Pytest's monkeypatch.

    Yields:
        Nothing.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("LLOPING_LLAMA_STATE_DIR", str(tmp_path / "state"))
    for name in ("GH_REPO", "CLAUDE_PLUGIN_DATA", "XDG_STATE_HOME", "GIT_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    """Create git repositories with remotes.

    Args:
        tmp_path: The test's directory.

    Returns:
        A function that takes a directory name and remotes (name to URL) and
        returns the repository's path.
    """

    def make(name: str = "repo", **remotes: str) -> Path:
        path = tmp_path / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        for remote, url in remotes.items():
            git(path, "remote", "add", remote, url)
        return path

    return make


@pytest.fixture
def profiles_list(tmp_path: Path) -> Path:
    """Write a profiles file in the list form (``repositories``).

    Args:
        tmp_path: The test's directory.

    Returns:
        The file's path.
    """
    path = tmp_path / "profiles.yaml"
    path.write_text(PROFILES_LIST, encoding="utf-8")
    return path


@pytest.fixture
def profiles_mapping(tmp_path: Path) -> Path:
    """Write a profiles file in the mapping form (``repos``).

    Args:
        tmp_path: The test's directory.

    Returns:
        The file's path.
    """
    path = tmp_path / "profiles-mapping.yaml"
    path.write_text(PROFILES_MAPPING, encoding="utf-8")
    return path
