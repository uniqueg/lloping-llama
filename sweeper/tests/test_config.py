"""Tests of the configurable defaults in :mod:`agent_sweeper.config`."""

from pathlib import Path

import pytest

from agent_sweeper import config
from agent_sweeper.config import Config


def test_default_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The clone root is ``$AGENT_CLONES``, else ``~/agent-clones``.

    Args:
        monkeypatch: Pytest fixture.
        tmp_path: Pytest fixture.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_CLONES", raising=False)
    assert config.default_root() == tmp_path / "agent-clones"
    monkeypatch.setenv("AGENT_CLONES", "")
    assert config.default_root() == tmp_path / "agent-clones"
    monkeypatch.setenv("AGENT_CLONES", str(tmp_path / "elsewhere"))
    assert Config().root == tmp_path / "elsewhere"


def test_default_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """The notification program is ``$AGENT_SWEEP_NOTIFY``; unset or empty sends none.

    Args:
        monkeypatch: Pytest fixture.
    """
    monkeypatch.delenv("AGENT_SWEEP_NOTIFY", raising=False)
    assert config.default_notify() is None
    monkeypatch.setenv("AGENT_SWEEP_NOTIFY", "")
    assert Config().notify is None
    monkeypatch.setenv("AGENT_SWEEP_NOTIFY", "notify-cmd")
    assert Config().notify == "notify-cmd"
