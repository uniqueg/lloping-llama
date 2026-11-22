"""Tests of the ledger and its command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sweeper.ledger import Entry, Ledger, main


def test_cli_lifecycle(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Entries are created, extended, updated, pinned and shown.

    Args:
        root: Clone root.
        capsys: Pytest fixture.
    """
    r = ["--root", str(root)]
    assert (
        main([*r, "new", "c", "--repo", "o/r", "--issue", "5", "-d", "fix", "--branch", "b"]) == 0
    )
    assert main([*r, "new", "c", "--repo", "o/r"]) == 1
    assert main([*r, "new", "c", "--repo", "o/r", "--force", "--id", "x1"]) == 0
    kube = root / "kc"
    assert main([*r, "add-resource", "c", "k8s-namespace", "ns", "--kubeconfig", str(kube)]) == 0
    assert main([*r, "add-resource", "c", "k8s-namespace", "ns"]) == 0
    assert main([*r, "set-state", "c", "ready"]) == 0
    assert main([*r, "pin", "c"]) == 0
    capsys.readouterr()
    assert main([*r, "show", "c"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == "x1" and shown["state"] == "ready" and shown["pinned"]
    assert shown["clone_path"] == str(root / "c")
    assert len(shown["resources"]) == 1 and shown["resources"][0]["kubeconfig"] == str(kube)
    assert [h["state"] for h in shown["history"]] == ["running", "ready"]
    assert main([*r, "pin", "c", "--off"]) == 0
    assert main([*r, "show"]) == 0
    assert "ready" in capsys.readouterr().out


def test_cli_missing_entries(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands on unknown entries fail; bad names fail with status 2.

    Args:
        root: Clone root.
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setenv("AGENT_CLONES", str(root))
    assert main(["add-resource", "x", "port", "1"]) == 1
    assert main(["set-state", "x", "merged"]) == 1
    assert main(["pin", "x"]) == 1
    (root / "old").mkdir()
    assert main(["pin", "old", "--off"]) == 1
    assert main(["pin", "old"]) == 0  # clone without an entry gets one
    entry = Ledger(root).load("old")
    assert entry is not None and entry.pinned and entry.clone_path == str(root / "old")
    assert main(["show", "x"]) == 1
    assert main(["show"]) == 0
    assert main(["new", "../evil", "--repo", "o/r"]) == 2


def test_entry_roundtrip_and_errors(root: Path) -> None:
    """Unknown keys are ignored, bad states and files are rejected.

    Args:
        root: Clone root.
    """
    data = {
        "id": "a",
        "clone": "a",
        "future": 1,
        "resources": [{"kind": "port", "name": "1", "x": 2}],
    }
    entry = Entry.from_dict(data)
    assert entry.resources[0].name == "1"
    with pytest.raises(ValueError, match="unknown state"):
        entry.set_state("done")
    ledger = Ledger(root)
    assert ledger.load_all() == {}
    with pytest.raises(ValueError, match="invalid clone name"):
        ledger.path(".hidden")
    ledger.save(entry)
    (ledger.dir / "broken.json").write_text("{")
    (ledger.dir / "gh-cache.json").write_text("{}")
    (ledger.dir / ".x.json").write_text("{}")
    (ledger.dir / "typed.json").write_text('{"id": "t", "clone": "t", "created": "2026-11-22"}')
    (ledger.dir / "named.json").write_text('{"id": 5, "clone": "n"}')
    (ledger.dir / "stated.json").write_text('{"id": "s", "clone": "s", "state": "done"}')
    (ledger.dir / "pinned.json").write_text('{"id": "p", "clone": "p", "pinned": "yes"}')
    (ledger.dir / "hist.json").write_text('{"id": "h", "clone": "h", "history": "x"}')
    (ledger.dir / "state.json").write_text('{"last_notify": 1}')
    (ledger.dir / "res.json").write_text('{"id": "r", "clone": "r", "resources": "x"}')
    (ledger.dir / "res2.json").write_text('{"id": "r", "clone": "r", "resources": ["x"]}')
    (ledger.dir / "list.json").write_text("[1]")
    loaded = ledger.load_all()
    assert list(loaded) == ["a", "stated"]
    assert loaded["stated"].state == "running"  # unknown state fails safe
    assert sorted(ledger.unreadable) == [
        "broken", "hist", "list", "named", "pinned", "res", "res2", "typed",
    ]  # fmt: skip
