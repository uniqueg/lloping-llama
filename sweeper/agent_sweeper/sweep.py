"""The ``agent-sweep`` command: collect facts, plan, act, log and summarise.

A real run holds ``<root>/.ledger/sweep.lock`` (a second sweep exits at
once), appends every outcome as a JSON line to ``.ledger/sweep.log`` and
writes a one-screen summary to ``.ledger/last-sweep.txt``. A dry run
changes nothing below the root: it does not fetch, takes no lock and
writes only the plan file it is given.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from agent_sweeper import config
from agent_sweeper.config import Config
from agent_sweeper.execute import Executor, Outcome
from agent_sweeper.facts import ProcScan, Runner, collect, docker_mounts, scan_proc
from agent_sweeper.rules import KEPT_REASON, SECRETS_REASON, SECRETS_UNKNOWN, Action, World, plan
from agent_sweeper.util import run

LOG_MAX_BYTES = 5 * 1024 * 1024
GB = 1024**3


def category(action: Action) -> str:
    """Group an action for the totals.

    Args:
        action: The action.

    Returns:
        The action kind, and for skips a reason without variable parts.
    """
    if action.kind != "skip":
        return action.kind
    reason = re.split(r"[;(]", action.reason, maxsplit=1)[0]
    reason = re.sub(r"\d+(\.\d+)?", "N", reason)
    return f"skip: {reason.strip()}"


def summarize(outcomes: list[Outcome], world: World, dry_run: bool, online: bool) -> str:
    """Render the one-screen summary.

    Args:
        outcomes: Outcomes (status ``planned`` in a dry run).
        world: Facts of the sweep.
        dry_run: Whether this was a dry run.
        online: Whether GitHub was reachable.

    Returns:
        The summary text.
    """
    when = dt.datetime.fromtimestamp(world.now).strftime("%Y-%m-%d %H:%M")
    mode = "DRY RUN (nothing changed)" if dry_run else "sweep"
    ok = {"done", "planned"}
    freed = sum(
        o.action.bytes
        for o in outcomes
        if o.status in ok
        and o.action.kind in ("delete-build-output", "delete-clone", "prune-rescue")
    )
    written = sum(o.action.bytes for o in outcomes if o.status in ok and o.action.kind == "rescue")
    root = world.clones[0].path.rsplit("/", 1)[0] if world.clones else "-"
    net = "online" if online else "OFFLINE (no gh, no remote checks)"
    lines = [
        f"Agent sweeper {mode} at {when}",
        f"root {root}; {len(world.clones)} clone dirs; {len(world.entries)} ledger entries; "
        f"disk {world.disk_pct:.0f}%; {net}",
        f"freed {freed / GB:.1f} GB; rescues write about {written / GB:.1f} GB",
        "",
        f"{'action':<52} {'count':>6} {'GB':>8}",
    ]
    totals: dict[str, list[float]] = collections.defaultdict(lambda: [0, 0.0])
    for o in outcomes:
        key = category(o.action)
        if o.status == "failed":
            key += " [FAILED]"
        elif o.status == "skipped" and o.action.kind != "skip":
            key += f" [skipped: {o.detail}]"
        totals[key][0] += 1
        totals[key][1] += o.action.bytes / GB
    for key in sorted(totals, key=lambda k: (k.startswith("skip"), k)):
        count, gb = totals[key]
        lines.append(f"{key[:52]:<52} {int(count):>6} {gb:>8.2f}")
    reasons = (SECRETS_REASON, SECRETS_UNKNOWN)
    held = sorted(
        {o.action.clone for o in outcomes if o.action.reason in reasons or o.detail in reasons}
    )
    if held:
        lines += ["", "Kept for secret-like files (tell the user once): " + ", ".join(held)]
    stuck = sorted({o.action.target for o in outcomes if o.action.reason == KEPT_REASON})
    if stuck:
        lines += ["", "Kept after a failed delete, review by hand (tell the user once): "
                  + ", ".join(stuck)]  # fmt: skip
    failed = [o for o in outcomes if o.status == "failed"]
    for o in failed[:5]:
        lines.append(f"FAILED {o.action.kind} {o.action.clone}: {o.detail[:80]}")
    return "\n".join(lines) + "\n"


def details(outcomes: list[Outcome]) -> str:
    """Render one line per action for the plan file.

    Args:
        outcomes: Outcomes.

    Returns:
        Fixed-width lines, sorted by action and clone.
    """
    order = {
        "delete-clone": 0,
        "rescue": 1,
        "delete-build-output": 2,
        "set-state": 3,
        "remove-resource": 4,
        "prune-rescue": 5,
        "notify": 6,
        "skip": 7,
    }
    rows = sorted(outcomes, key=lambda o: (order.get(o.action.kind, 9), o.action.clone))
    lines = [f"{'action':<20} {'GB':>6}  {'clone':<34} reason / target"]
    for o in rows:
        a = o.action
        what = (
            a.reason if a.kind in ("delete-clone", "rescue", "skip") else f"{a.reason}: {a.target}"
        )
        if a.kind == "skip" and a.target != a.clone and not a.target.endswith("/" + a.clone):
            what = f"{a.reason}: {a.target}"
        lines.append(f"{a.kind:<20} {a.bytes / GB:>6.2f}  {a.clone:<34} {what}")
    return "\n".join(lines) + "\n"


def append_log(path: Path, outcomes: list[Outcome]) -> None:
    """Append outcomes as JSON lines, rotating the log at 5 MB.

    Args:
        path: Log file.
        outcomes: Outcomes to log.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_suffix(".log.1"))
    with path.open("a") as log:
        for o in outcomes:
            log.write(json.dumps(o.record(), sort_keys=True) + "\n")


def path_in_use(path: str, proc_scan: ProcScan | None = None) -> bool:
    """Scan processes and container mounts now for a path.

    Args:
        path: Directory to check.
        proc_scan: Process scanner; defaults to /proc plus docker mounts.

    Returns:
        True if a process or mount uses the path or anything below it.
    """
    pairs = list(proc_scan()) if proc_scan else [*scan_proc(), *docker_mounts()]
    p = Path(path)
    names = {str(p.absolute()), str(p.parent.resolve() / p.name)}
    return any(used == n or used.startswith(n + "/") for used, _ in pairs for n in names)


def sweep(
    cfg: Config,
    plan_out: Path | None = None,
    online: bool | None = None,
    proc_scan: ProcScan | None = None,
    disk_pct: float | None = None,
    runner: Runner = run,
) -> str:
    """Run one sweep.

    Args:
        cfg: Run configuration.
        plan_out: Where a dry run writes its plan (summary and details).
        online: Override the GitHub reachability probe.
        proc_scan: Process scanner (injectable for tests).
        disk_pct: Override the disk usage (for tests).
        runner: Command runner (injectable for tests).

    Returns:
        The summary text.
    """
    world, gh = collect(cfg, online=online, proc_scan=proc_scan, disk_pct=disk_pct, runner=runner)
    actions = plan(world)
    is_online = gh.enabled
    if cfg.dry_run:
        outcomes = [Outcome(a, "skipped" if a.kind == "skip" else "planned") for a in actions]
        if cfg.cache_path is not None:  # never write below the root in a dry run
            gh.save()
        text = summarize(outcomes, world, True, is_online)
        if plan_out is not None:
            plan_out.parent.mkdir(parents=True, exist_ok=True)
            plan_out.write_text(text + "\n" + details(outcomes))
        return text
    gh.save()

    def in_use(path: str) -> bool:
        # A fresh scan before every destructive step (cheap next to rmtree).
        return path_in_use(path, proc_scan)

    outcomes = Executor(cfg, in_use, runner).execute(actions)
    append_log(cfg.ledger_dir / "sweep.log", outcomes)
    text = summarize(outcomes, world, False, is_online)
    (cfg.ledger_dir / "last-sweep.txt").write_text(text)
    return text


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``agent-sweep`` command.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv``.

    Returns:
        Exit status (0 also when another sweep holds the lock).
    """
    parser = argparse.ArgumentParser(prog="agent-sweep", description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="clone root")
    parser.add_argument("--dry-run", action="store_true", help="plan only, change nothing")
    parser.add_argument("--plan-out", type=Path, default=None, help="dry run: plan file")
    parser.add_argument("--cache", type=Path, default=None, help="gh cache file")
    parser.add_argument("--no-fetch", action="store_true", help="skip git fetch")
    parser.add_argument("--offline", action="store_true", help="no gh, no remote calls")
    args = parser.parse_args(argv)
    cfg = Config(
        root=args.root or config.default_root(),
        dry_run=args.dry_run,
        cache_path=args.cache,
        fetch=not args.no_fetch,
    )
    if not cfg.root.is_dir():
        print(f"agent-sweep: no clone root {cfg.root}", file=sys.stderr)
        return 1
    online = False if args.offline else None
    if cfg.dry_run:
        print(sweep(cfg, args.plan_out, online), end="")
        return 0
    cfg.ledger_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.ledger_dir / "sweep.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("agent-sweep: another sweep is running", file=sys.stderr)
            return 0
        print(sweep(cfg, online=online), end="")
    return 0
