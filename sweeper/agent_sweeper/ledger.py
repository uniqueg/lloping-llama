"""Ledger of agent clones and the resources they own.

Each clone has one JSON file ``<root>/.ledger/<clone-name>.json``. The main
session writes it before it launches an agent; sessions, agents and the
sweeper update it with the ``agent-ledger`` command.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_sweeper import config


@dataclass
class Resource:
    """A resource that an agent created and that must be removed later.

    Attributes:
        kind: One of :data:`agent_sweeper.config.RESOURCE_KINDS`.
        name: Name of the cluster, container, network, port or namespace.
        kubeconfig: Kubeconfig file for ``k8s-namespace`` resources.
        removed: Time the sweeper removed or released it, if it did.
    """

    kind: str
    name: str
    kubeconfig: str | None = None
    removed: float | None = None


@dataclass
class Entry:
    """Ledger entry of one clone.

    Attributes:
        id: Stable identifier; also the value of the ``agent-ledger`` label.
        clone: Clone name (the file name without ``.json``).
        description: What the agent works on.
        repo: ``owner/name`` of the repository.
        issue: Issue number or URL.
        branch: Feature branch.
        clone_path: Absolute path of the clone.
        created: Creation time (epoch seconds).
        updated: Last update time (epoch seconds).
        state: One of :data:`agent_sweeper.config.STATES`.
        pinned: Never delete the clone (build outputs may still go).
        resources: Resources the agent created.
        history: State changes as ``{"ts", "state"}`` records.
    """

    id: str
    clone: str
    description: str = ""
    repo: str = ""
    issue: str = ""
    branch: str = ""
    clone_path: str = ""
    created: float = 0.0
    updated: float = 0.0
    state: str = "running"
    pinned: bool = False
    resources: list[Resource] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entry:
        """Build an entry from its JSON form, ignoring unknown keys.

        Args:
            data: Parsed JSON object.

        Returns:
            The entry.
        """
        if not isinstance(data, dict):
            raise TypeError("entry must be an object")
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("created", "updated"):
            if not isinstance(known.get(key, 0.0), (int, float)):
                raise TypeError(f"{key} must be a number")
        for key in ("id", "clone", "clone_path", "state"):
            if not isinstance(known.get(key, ""), str):
                raise TypeError(f"{key} must be a string")
        if known.get("state", "running") not in config.STATES:
            known["state"] = "running"  # unknown states fail safe: still active
        if not isinstance(known.get("pinned", False), bool):
            raise TypeError("pinned must be true or false")
        history = known.get("history", [])
        if not isinstance(history, list) or not all(isinstance(h, dict) for h in history):
            raise TypeError("history must be a list of objects")
        resources = data.get("resources", [])
        if not isinstance(resources, list) or not all(isinstance(r, dict) for r in resources):
            raise TypeError("resources must be a list of objects")
        known["resources"] = [
            Resource(**{k: v for k, v in r.items() if k in Resource.__dataclass_fields__})
            for r in data.get("resources", [])
        ]
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of the entry.

        Returns:
            A JSON-serialisable dictionary.
        """
        return asdict(self)

    def set_state(self, state: str, now: float | None = None) -> None:
        """Change the state and record it in the history.

        Args:
            state: New state.
            now: Time of the change; defaults to the current time.

        Raises:
            ValueError: If the state is unknown.
        """
        if state not in config.STATES:
            raise ValueError(f"unknown state {state!r}; use one of {config.STATES}")
        ts = time.time() if now is None else now
        self.state = state
        self.updated = ts
        self.history.append({"ts": ts, "state": state})


#: Files in the ledger directory that are not entries.
NOT_ENTRIES = frozenset({"gh-cache.json", "state.json"})


class Ledger:
    """Access to the ledger files below one clone root.

    Args:
        root: Directory that holds the clones.
    """

    def __init__(self, root: Path) -> None:
        """Initialise the ledger.

        Args:
            root: Directory that holds the clones.
        """
        self.root = root
        self.dir = root / ".ledger"
        self.unreadable: list[str] = []

    def path(self, clone: str) -> Path:
        """Return the file of one entry.

        Args:
            clone: Clone name.

        Returns:
            Path of the entry's JSON file.

        Raises:
            ValueError: If the name could escape the ledger directory.
        """
        if not clone or "/" in clone or clone.startswith("."):
            raise ValueError(f"invalid clone name {clone!r}")
        return self.dir / f"{clone}.json"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the ledger write lock for a read-modify-write cycle.

        Yields:
            Nothing; the lock is held inside the block.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".write.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self, clone: str) -> Entry | None:
        """Load one entry.

        Args:
            clone: Clone name.

        Returns:
            The entry, or None if there is none or it is unreadable.
        """
        try:
            return Entry.from_dict(json.loads(self.path(clone).read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def load_all(self) -> dict[str, Entry]:
        """Load every readable entry.

        Names of unreadable entries are kept in :attr:`unreadable`, so that
        their clones can be left alone (a broken entry may have held a pin).

        Returns:
            Entries by clone name.
        """
        entries: dict[str, Entry] = {}
        self.unreadable = []
        if not self.dir.is_dir():
            return entries
        for file in sorted(self.dir.glob("*.json")):
            if file.name.startswith(".") or file.name in NOT_ENTRIES:
                continue
            entry = self.load(file.stem)
            if entry is None:
                self.unreadable.append(file.stem)
            else:
                entries[file.stem] = entry
        return entries

    def save(self, entry: Entry) -> None:
        """Write one entry atomically.

        Args:
            entry: Entry to write.
        """
        target = self.path(entry.clone)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".json.tmp{os.getpid()}")
        tmp.write_text(json.dumps(entry.to_dict(), indent=2, sort_keys=True) + "\n")
        os.replace(tmp, target)


def _cmd_new(ledger: Ledger, args: argparse.Namespace) -> int:
    """Create an entry (``agent-ledger new``).

    Args:
        ledger: Ledger to write.
        args: Parsed arguments.

    Returns:
        Exit status.
    """
    with ledger.locked():
        if ledger.load(args.clone) is not None and not args.force:
            print(f"agent-ledger: entry {args.clone} exists (use --force)", file=sys.stderr)
            return 1
        now = time.time()
        entry = Entry(
            id=args.id or args.clone,
            clone=args.clone,
            description=args.description,
            repo=args.repo,
            issue=str(args.issue or ""),
            branch=args.branch,
            clone_path=str(Path(args.path or ledger.root / args.clone).absolute()),
            created=now,
        )
        entry.set_state(args.state, now)
        ledger.save(entry)
    print(ledger.path(args.clone))
    return 0


def _cmd_add_resource(ledger: Ledger, args: argparse.Namespace) -> int:
    """Record a resource (``ledger add-resource``).

    Args:
        ledger: Ledger to write.
        args: Parsed arguments.

    Returns:
        Exit status.
    """
    with ledger.locked():
        entry = ledger.load(args.clone)
        if entry is None:
            print(f"agent-ledger: no entry {args.clone}", file=sys.stderr)
            return 1
        if any(r.kind == args.kind and r.name == args.name for r in entry.resources):
            return 0
        kubeconfig = str(Path(args.kubeconfig).absolute()) if args.kubeconfig else None
        entry.resources.append(Resource(args.kind, args.name, kubeconfig))
        entry.updated = time.time()
        ledger.save(entry)
    return 0


def _cmd_set_state(ledger: Ledger, args: argparse.Namespace) -> int:
    """Change an entry's state (``ledger set-state``).

    Args:
        ledger: Ledger to write.
        args: Parsed arguments.

    Returns:
        Exit status.
    """
    with ledger.locked():
        entry = ledger.load(args.clone)
        if entry is None:
            print(f"agent-ledger: no entry {args.clone}", file=sys.stderr)
            return 1
        entry.set_state(args.state)
        ledger.save(entry)
    return 0


def _cmd_pin(ledger: Ledger, args: argparse.Namespace) -> int:
    """Pin or unpin a clone (``agent-ledger pin``).

    A clone dir without an entry (e.g. one made before the ledger existed)
    gets a minimal entry, so it can be pinned right after install.

    Args:
        ledger: Ledger to write.
        args: Parsed arguments.

    Returns:
        Exit status.
    """
    with ledger.locked():
        entry = ledger.load(args.clone)
        path = ledger.root / args.clone
        if entry is None and not args.off and path.is_dir():
            now = time.time()
            entry = Entry(id=args.clone, clone=args.clone, clone_path=str(path.absolute()),
                          created=now, updated=now)  # fmt: skip
        if entry is None:
            print(f"agent-ledger: no entry {args.clone}", file=sys.stderr)
            return 1
        entry.pinned = not args.off
        entry.updated = time.time()
        ledger.save(entry)
    return 0


def _cmd_show(ledger: Ledger, args: argparse.Namespace) -> int:
    """Print one entry as JSON, or a table of all (``ledger show``).

    Args:
        ledger: Ledger to read.
        args: Parsed arguments.

    Returns:
        Exit status.
    """
    if args.clone:
        entry = ledger.load(args.clone)
        if entry is None:
            print(f"agent-ledger: no entry {args.clone}", file=sys.stderr)
            return 1
        print(json.dumps(entry.to_dict(), indent=2, sort_keys=True))
        return 0
    for name, entry in ledger.load_all().items():
        open_res = sum(1 for r in entry.resources if r.removed is None)
        pin = " pinned" if entry.pinned else ""
        print(f"{name:<40} {entry.state:<12} {entry.repo:<36} res={open_res}{pin}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``agent-ledger`` argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(prog="agent-ledger", description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="clone root")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create an entry before launching an agent")
    new.add_argument("clone", help="clone name (directory name)")
    new.add_argument("--repo", required=True, help="owner/name")
    new.add_argument("--issue", default="", help="issue number or URL")
    new.add_argument("--branch", default="", help="feature branch")
    new.add_argument("--description", "-d", default="", help="agent description")
    new.add_argument("--path", default=None, help="clone path (default root/clone)")
    new.add_argument("--id", default=None, help="entry id (default: clone name)")
    new.add_argument("--state", default="running", choices=config.STATES)
    new.add_argument("--force", action="store_true", help="overwrite an entry")
    new.set_defaults(func=_cmd_new)

    add = sub.add_parser("add-resource", help="record a resource the agent made")
    add.add_argument("clone")
    add.add_argument("kind", choices=config.RESOURCE_KINDS)
    add.add_argument("name")
    add.add_argument("--kubeconfig", default=None, help="for k8s-namespace")
    add.set_defaults(func=_cmd_add_resource)

    state = sub.add_parser("set-state", help="change an entry's state")
    state.add_argument("clone")
    state.add_argument("state", choices=config.STATES)
    state.set_defaults(func=_cmd_set_state)

    pin = sub.add_parser("pin", help="never delete this clone")
    pin.add_argument("clone")
    pin.add_argument("--off", action="store_true", help="unpin")
    pin.set_defaults(func=_cmd_pin)

    show = sub.add_parser("show", help="show one entry or list all")
    show.add_argument("clone", nargs="?", default=None)
    show.set_defaults(func=_cmd_show)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``agent-ledger`` command.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv``.

    Returns:
        Exit status.
    """
    args = build_parser().parse_args(argv)
    ledger = Ledger(args.root or config.default_root())
    try:
        result: int = args.func(ledger, args)
    except ValueError as err:
        print(f"agent-ledger: {err}", file=sys.stderr)
        return 2
    return result
