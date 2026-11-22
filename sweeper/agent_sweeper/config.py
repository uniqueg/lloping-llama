"""Thresholds, names and paths that the sweeper rules use.

All thresholds are constants here so that a change of policy is a one-line
edit with a test. :class:`Config` bundles them with the paths of one run so
that tests can point the sweeper at a temporary directory.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from pathlib import Path

HOUR = 3600.0
DAY = 24 * HOUR

#: A ledger entry in state ``running`` keeps its clone in use for this long
#: after the clone last changed.
RUNNING_WINDOW = 2 * HOUR
#: Build outputs of an idle clone are removed after this much inactivity.
BUILD_IDLE = 2 * HOUR
#: No whole clone is deleted within this time of its last change, whatever
#: its PR state (protects clones that were just created for an agent).
CLONE_MIN_IDLE = 2 * HOUR
#: Clones with unsafe work get a rescue copy after this much inactivity.
RESCUE_AFTER = 7 * DAY
#: Clones untouched for this long are deleted (after a rescue if unsafe).
DELETE_AFTER = 14 * DAY
#: Leftovers in ``.trash`` (a deletion interrupted after the rename) are
#: removed after this long.
TRASH_KEEP = 1 * HOUR
#: Rescue copies are kept this long.
RESCUE_KEEP = 90 * DAY
#: Recorded resources of an idle clone are removed after this long.
RESOURCE_IDLE = 24 * HOUR
#: A ledger entry without a clone directory is marked cleaned only after
#: this grace period (the session writes the entry before cloning).
LEDGER_GRACE = 1 * HOUR
#: Disk usage of ``/`` (percent) above which build outputs of every idle
#: clone are removed regardless of age.
DISK_SOFT = 85.0
#: Disk usage (percent) above which a notification is sent as well.
DISK_HARD = 92.0
#: Minimum time between two disk notifications.
NOTIFY_INTERVAL = 24 * HOUR
#: Whether clones whose work is all on a remote also get a full bundle
#: before deletion. Deliberately off: a safe clone's commits are all on a
#: remote, so a bundle would only cost disk.
BUNDLE_SAFE_CLONES = False

#: Timeouts in seconds for external calls.
TIMEOUT_GIT_LOCAL = 60.0
TIMEOUT_GIT_REMOTE = 60.0
TIMEOUT_GIT_BUNDLE = 600.0
TIMEOUT_GH = 30.0
TIMEOUT_DOCKER = 30.0
TIMEOUT_KUBECTL = 60.0
TIMEOUT_NOTIFY = 20.0
TIMEOUT_PROBE = 5.0

#: Parallel workers for collecting facts (git remote calls and gh).
WORKERS = 6
#: Cache lifetime of a gh answer that contains an open PR or no PR.
GH_CACHE_TTL_OPEN = 1 * HOUR
#: Cache lifetime of a gh answer whose PRs are all merged or closed.
GH_CACHE_TTL_FINAL = 30 * DAY

#: Directory names that are rebuildable build outputs inside a git clone.
BUILD_OUTPUT_NAMES = frozenset(
    {
        "target",
        "node_modules",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "site",
        ".next",
        "coverage",
        "htmlcov",
        ".nyc_output",
        "playwright-report",
        "test-results",
        "ms-playwright",
        ".uv-cache",
        ".npm-cache",
        ".pnpm-store",
        ".turbo",
        ".cache",
        ".tools",
        ".uvc",
        ".pc-cache",
    }
)
#: Name prefixes that mark build outputs (``.venv``, ``.venv-py313``, ...).
BUILD_OUTPUT_PREFIXES = (".venv",)
#: Subset of build outputs that are removed in directories without git,
#: and the only ones whose untracked contents do not make a tree dirty
#: where names like ``build`` or ``site`` may hold hand-made content.
NOGIT_OUTPUT_NAMES = frozenset(
    {
        "node_modules",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".npm-cache",
        ".pnpm-store",
        "ms-playwright",
        ".turbo",
        ".cache",
        ".tools",
        ".uvc",
        ".pc-cache",
    }
)
#: Branch names that count as default branches besides each remote's HEAD.
DEFAULT_BRANCHES = frozenset({"main", "master", "dev", "develop"})
#: Ledger states, in lifecycle order.
STATES = ("running", "ready", "handed-over", "merged", "cleaned")
#: Ledger states in which build outputs are removed at once.
FINISHED_STATES = frozenset({"ready", "handed-over", "merged"})
#: Resource kinds the ledger accepts.
RESOURCE_KINDS = (
    "kind-cluster",
    "k3d-cluster",
    "docker-container",
    "docker-network",
    "port",
    "k8s-namespace",
)
#: Docker label that marks resources owned by a ledger entry.
LEDGER_LABEL = "agent-ledger"


def default_root() -> Path:
    """Return the agent clone root.

    Returns:
        ``$AGENT_CLONES`` if set, else ``~/agent-clones``.
    """
    env = os.environ.get("AGENT_CLONES")
    return Path(env) if env else Path.home() / "agent-clones"


def default_notify() -> str | None:
    """Return the program that sends the disk notification.

    Returns:
        ``$AGENT_SWEEP_NOTIFY`` if set and not empty, else None (no
        notification; the disk usage is still in the summary).
    """
    return os.environ.get("AGENT_SWEEP_NOTIFY") or None


@dataclass(frozen=True)
class Config:
    """Paths and switches of one sweeper run.

    Attributes:
        root: Directory that holds the agent clones.
        dry_run: Plan only; change nothing (no fetch, no writes in ``root``).
        cache_path: Where gh answers are cached; defaults to the ledger dir.
        fetch: Run ``git fetch --prune`` before checking reachability.
        disk_path: Mount point whose usage triggers the disk rules.
        notify: Program called with one line of text when the disk is
            nearly full; None sends nothing.
    """

    root: Path = field(default_factory=default_root)
    dry_run: bool = False
    cache_path: Path | None = None
    fetch: bool = True
    disk_path: Path = Path("/")
    notify: str | None = field(default_factory=default_notify)

    @property
    def ledger_dir(self) -> Path:
        """Return the ledger directory.

        Returns:
            ``<root>/.ledger``.
        """
        return self.root / ".ledger"

    @property
    def rescue_dir(self) -> Path:
        """Return the rescue directory.

        Returns:
            ``<root>/.rescue``.
        """
        return self.root / ".rescue"

    @property
    def gh_cache(self) -> Path:
        """Return the gh cache file.

        Returns:
            ``cache_path`` if set, else ``<root>/.ledger/gh-cache.json``.
        """
        return self.cache_path or self.ledger_dir / "gh-cache.json"


def is_build_output(name: str, git: bool = True) -> bool:
    """Tell whether a directory name marks a rebuildable build output.

    Args:
        name: Base name of the directory.
        git: Whether the directory lies in a git clone; outside git only the
            unambiguous cache names count.

    Returns:
        True if the directory may be removed as a build output.
    """
    if not git:
        return name in NOGIT_OUTPUT_NAMES or name.startswith(BUILD_OUTPUT_PREFIXES)
    return name in BUILD_OUTPUT_NAMES or name.startswith(BUILD_OUTPUT_PREFIXES)


#: Secret-like paths (see :func:`is_secret`).
SECRET_DIRS = frozenset({".kube", ".ssh", ".gnupg", ".docker", ".aws", ".azure", ".gcloud"})
#: Directories below ``.config`` that hold credentials (gh, gcloud, ...).
SECRET_CONFIG_DIRS = frozenset({"gh", "gcloud", "hub", "rclone"})
SECRET_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".envrc",
        ".dev.vars",
        "credentials.json",
        "service-account.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
    }
)
SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kubeconfig",
    ".tfvars",
    ".tfstate",
)
SECRET_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "kubeconfig")


def is_secret(rel: str) -> bool:
    """Tell whether a path looks like a secret (never copied into a rescue).

    Args:
        rel: Path relative to the clone.

    Returns:
        True for ``.env*`` (except ``.env.example``), files below ``.kube``,
        ``.ssh``, ``.config/gh`` and similar dirs (any case), credential
        files (``.netrc``, ``.envrc``, ``kubeconfig*``, ...), keys,
        certificates and Terraform variables and state. Exact patterns only, so source files
        like ``auth/token.py`` are never dropped.
    """
    parts = rel.lower().split("/")
    name = parts[-1]
    dirs = parts[:-1]
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or any(p in SECRET_DIRS for p in dirs)
        or any(a == ".config" and b in SECRET_CONFIG_DIRS for a, b in itertools.pairwise(dirs))
        or name in SECRET_NAMES
        or name.endswith(SECRET_SUFFIXES)
        or name.startswith(SECRET_PREFIXES)
    )
