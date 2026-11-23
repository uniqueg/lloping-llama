"""Small helpers: subprocess calls with timeouts and disk usage."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Environment for every external command: never prompt, never page.
QUIET_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_SPINNER_DISABLED": "1",
    "LC_ALL": "C.UTF-8",
}


@dataclass(frozen=True)
class Result:
    """Outcome of an external command.

    Attributes:
        rc: Exit status; 124 on timeout, 127 if the program is missing.
        out: Standard output.
        err: Standard error.
    """

    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        """Tell whether the command succeeded.

        Returns:
            True if the exit status is 0.
        """
        return self.rc == 0


def run(
    cmd: Sequence[str],
    timeout: float,
    cwd: str | Path | None = None,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    """Run a command with a timeout; never raise.

    Args:
        cmd: Program and arguments.
        timeout: Seconds before the command is killed.
        cwd: Working directory.
        stdin: Text fed to standard input.
        env: Extra environment variables.

    Returns:
        The result.
    """
    full_env = {**os.environ, **QUIET_ENV, **(env or {})}
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            check=False,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timeout after {timeout:.0f}s")
    except (FileNotFoundError, NotADirectoryError, PermissionError) as err:
        return Result(127, "", str(err))
    return Result(proc.returncode, proc.stdout, proc.stderr)


def which(program: str) -> bool:
    """Tell whether a program is on the PATH.

    Args:
        program: Program name.

    Returns:
        True if found.
    """
    return shutil.which(program) is not None


def du(path: str | Path) -> int:
    """Return the disk usage of a tree without following symlinks.

    Args:
        path: File or directory.

    Returns:
        Bytes allocated (``st_blocks * 512``); unreadable parts count as 0.
    """
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            continue
        total += st.st_blocks * 512
        if not os.path.isdir(current) or os.path.islink(current):
            continue
        try:
            with os.scandir(current) as it:
                for item in it:
                    if item.is_dir(follow_symlinks=False):
                        stack.append(item.path)
                    else:
                        try:
                            total += item.stat(follow_symlinks=False).st_blocks * 512
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def disk_percent(path: str | Path) -> float:
    """Return the usage of a file system the way ``df`` shows it.

    Args:
        path: Any path on the file system.

    Returns:
        ``used / (used + available)`` in percent.
    """
    st = os.statvfs(path)
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    return 100.0 * used / (used + avail) if used + avail else 0.0


def within(path: str | Path, parent: str | Path) -> bool:
    """Tell whether a path lies strictly inside a directory.

    Symlinks in ``path`` itself are not resolved (it may be a symlink that
    must be unlinked); its parent directory is.

    Args:
        path: Candidate path.
        parent: Directory.

    Returns:
        True if ``path`` is below ``parent``.
    """
    p = Path(path)
    resolved = p.parent.resolve() / p.name
    base = Path(parent).resolve()
    return resolved != base and base in resolved.parents
