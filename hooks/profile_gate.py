#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""Profile gate: apply repository profiles to ``gh`` and ``git`` commands.

A Claude Code ``PreToolUse`` hook for the Bash tool. It finds the actions in a
command that change a GitHub repository (pushes, ref changes through the API,
merges), works out which repository each action targets, looks the repository
up in the overlay's ``profiles.yaml`` and answers ``deny`` or ``ask`` where the
repository's profile doesn't allow the action. It never answers ``allow``: an
action the profile allows is left to the other permission rules.

The path of ``profiles.yaml`` comes from the environment variable
``LLOPING_LLAMA_PROFILES``; without it, the hook does nothing.

The gate guards against mistakes, not against an adversary: it reads shell
syntax well enough for the commands agents write, and asks when it cannot tell
which repository a gated action targets.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

CONFIG_ENV = "LLOPING_LLAMA_PROFILES"
STATE_ENV = "LLOPING_LLAMA_STATE_DIR"
PREFIX = "profile-gate: "

PROFILES = (
    "owner-sole",
    "owner-blessed-maintainer",
    "owner-shared",
    "maintainer",
    "developer",
    "contributor",
)
MAX_DEPTH = 5
MAX_SESSIONS = 50
GIT_TIMEOUT = 5.0

Kind = Literal["push", "merge"]
ACTIONS: tuple[str, ...] = get_args(Kind)
Verdict = Literal["deny", "ask"]

# Tokens that end a simple command
SEPARATORS = frozenset({"&&", "||", ";", ";;", "|", "|&", "&", "\n", "(", ")"})
OPERATOR_CHARS = frozenset("();<>|&\n")
OPERATOR = re.compile(r"&&|\|\||;;|\|&|[<>]+&?|&>|[;&|()\n]")
# Shell keywords (and fish's and/or/not) that may precede a command
KEYWORDS = frozenset(
    {"{", "}", "!", "if", "then", "do", "else", "elif", "while", "until"}
    | {"and", "or", "not", "begin"}
)
# Commands that run their arguments as a command: options that take a value
WRAPPERS: dict[str, frozenset[str]] = {
    "builtin": frozenset(),
    "command": frozenset(),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "exec": frozenset({"-a"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}),
    "time": frozenset({"-f", "-o"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "xargs": frozenset({"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"}),
}
# Wrappers whose first positional argument is not the command
WRAPPER_POSITIONALS = {"timeout": 1}
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

GIT_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--super-prefix",
        "--attr-source",
    }
)
# Global options that change which repository and configuration git uses;
# passed on to the git commands that resolve the target repository
GIT_CONTEXT_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree"})
# Built-in git commands (git doesn't let aliases override them); anything else
# may be an alias and is looked up. Only an optimisation: a missing name costs
# one `git config` call
GIT_BUILTINS = frozenset(
    [
        "add",
        "am",
        "apply",
        "archive",
        "bisect",
        "blame",
        "branch",
        "bundle",
        "cat-file",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "describe",
        "diff",
        "fetch",
        "format-patch",
        "fsck",
        "gc",
        "grep",
        "help",
        "init",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge",
        "merge-base",
        "mv",
        "notes",
        "pull",
        "push",
        "range-diff",
        "rebase",
        "reflog",
        "remote",
        "repack",
        "replace",
        "reset",
        "restore",
        "rev-list",
        "rev-parse",
        "revert",
        "rm",
        "shortlog",
        "show",
        "show-ref",
        "sparse-checkout",
        "stash",
        "status",
        "submodule",
        "switch",
        "symbolic-ref",
        "tag",
        "update-index",
        "update-ref",
        "version",
        "worktree",
    ]
)
PUSH_VALUE_OPTIONS = frozenset(
    {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
)

GH_PR_MERGE_VALUE_OPTIONS = frozenset(
    {
        "-R",
        "--repo",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-t",
        "--subject",
        "-A",
        "--author-email",
        "--match-head-commit",
    }
)
GH_API_VALUE_OPTIONS = frozenset(
    {
        "-X",
        "--method",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "-H",
        "--header",
        "--input",
        "-q",
        "--jq",
        "-t",
        "--template",
        "--hostname",
        "--cache",
        "-p",
        "--preview",
    }
)
GH_API_FIELD_OPTIONS = frozenset({"-f", "--raw-field", "-F", "--field"})
# gh picks the default repository from these remotes, in this order
GH_REMOTE_ORDER = ("upstream", "github", "origin")
GRAPHQL_MUTATIONS: dict[str, Kind] = {
    "mergePullRequest": "merge",
    "enablePullRequestAutoMerge": "merge",
    "mergeBranch": "merge",
    "createRef": "push",
    "updateRef": "push",
    "updateRefs": "push",
    "deleteRef": "push",
    "createCommitOnBranch": "push",
}
API_RULES: tuple[tuple[re.Pattern[str], frozenset[str], Kind], ...] = (
    (re.compile(r"^pulls/\d+/merge$"), frozenset({"PUT"}), "merge"),
    (re.compile(r"^merges$"), frozenset({"POST"}), "merge"),
    (re.compile(r"^git/refs(/.*)?$"), frozenset({"POST", "PATCH", "DELETE"}), "push"),
    (re.compile(r"^contents/.+$"), frozenset({"PUT", "DELETE"}), "push"),
    (re.compile(r"^branches/[^/]+/rename$"), frozenset({"POST"}), "push"),
    (re.compile(r"^pulls/\d+/update-branch$"), frozenset({"PUT"}), "push"),
)
URL_PATTERNS = (
    # ssh://, git://, https://, with optional user and port
    re.compile(
        r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?(?P<host>[^:/]+)(?::\d+)?/(?P<path>.*)$"
    ),
    # scp-like: git@github.com:owner/repo.git
    re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$"),
)


class GateError(Exception):
    """A gated action whose target or profile cannot be determined."""


@dataclass(frozen=True)
class Action:
    """An action in a command that the gate checks.

    Attributes:
        kind: ``push`` (pushes and other ref changes) or ``merge``.
        repos: The repositories the action may target (``owner/repo``, or
            ``host/owner/repo`` outside github.com); the strictest one counts.
        what: A short description for messages, e.g. ``git push``.
    """

    kind: Kind
    repos: tuple[str, ...]
    what: str


@dataclass(frozen=True)
class Decision:
    """A verdict on one action.

    Attributes:
        verdict: ``deny`` or ``ask``.
        reason: Why, shown to the user and the agent.
    """

    verdict: Verdict
    reason: str


@dataclass
class Context:
    """Where a simple command runs.

    Attributes:
        cwd: The working directory, or None once it can no longer be told
            (e.g. after ``cd -``).
        env: Variables set for the command (``VAR=value`` prefixes).
    """

    cwd: Path | None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RepoEntry:
    """One repository entry of ``profiles.yaml``.

    Attributes:
        pattern: The repository name or glob, lower case.
        profile: The profile name.
        data: The whole entry, for the profile's fields.
        allowed: The entry's ``allowed_actions``.
    """

    pattern: str
    profile: str
    data: Mapping[str, Any]
    allowed: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Profiles:
    """The parsed ``profiles.yaml``.

    Attributes:
        path: Where the file is.
        listed: Entries of ``repositories`` (a list; first match wins).
        mapped: Entries of ``repos`` (a mapping; exact name, then first glob).
        allowed: Actions each profile allows (``allowed_actions`` in
            ``profiles``).
        unknown: What to do for unknown repositories: ``ask`` (once per
            session) or ``deny``.
    """

    path: Path
    listed: tuple[RepoEntry, ...]
    mapped: tuple[RepoEntry, ...]
    allowed: Mapping[str, frozenset[str]]
    unknown: Verdict = "ask"

    def lookup(self, repo: str) -> RepoEntry | None:
        """Find the entry for a repository.

        Args:
            repo: The repository, as normalised by ``normalise_repo``.

        Returns:
            The matching entry, or None if the repository is unknown. A
            repository outside github.com that has no entry of its own is
            looked up as ``owner/repo``, so that SSH host aliases of GitHub
            get its profile.
        """
        entry = self._lookup(repo)
        if entry is None and repo.count("/") == 2:
            # A host other than github.com, e.g. an SSH alias for it
            entry = self._lookup(repo.split("/", 1)[1])
        return entry

    def _lookup(self, repo: str) -> RepoEntry | None:
        """Find the entry for a repository name as given.

        Args:
            repo: The repository.

        Returns:
            The matching entry, or None.
        """
        for entry in self.listed:
            if glob_match(entry.pattern, repo):
                return entry
        for entry in self.mapped:
            if entry.pattern == repo:
                return entry
        for entry in self.mapped:
            is_glob = "*" in entry.pattern or "?" in entry.pattern
            if is_glob and glob_match(entry.pattern, repo):
                return entry
        return None


def glob_match(pattern: str, name: str) -> bool:
    """Match a repository glob, where ``*`` and ``?`` don't match ``/``.

    Args:
        pattern: The glob, lower case.
        name: The repository name, lower case.

    Returns:
        Whether the name matches.
    """
    regex = "".join(
        "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in pattern
    )
    return re.fullmatch(regex, name) is not None


def normalise_repo(host: str, path: str) -> str | None:
    """Turn a host and a repository path into the name used in profiles.

    Args:
        host: The host, e.g. ``github.com``.
        path: ``owner/repo``, possibly with ``.git`` or a trailing ``/``.

    Returns:
        ``owner/repo`` (``host/owner/repo`` outside github.com), lower case, or
        None if the path is not ``owner/repo``.
    """
    path = path.strip("/")
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    host = host.lower()
    name = "/".join(parts).lower()
    return name if host in ("github.com", "www.github.com") else f"{host}/{name}"


def repo_from_url(url: str) -> str | None:
    """Get the repository from a remote URL.

    Args:
        url: A git remote URL (https, ssh or scp-like).

    Returns:
        The normalised repository, or None for local paths and URLs that
        don't name ``owner/repo``.
    """
    for pattern in URL_PATTERNS:
        m = pattern.match(url)
        if m:
            return normalise_repo(m["host"], m["path"])
    return None


def repo_from_gh_arg(value: str) -> str | None:
    """Get the repository from a gh ``--repo`` value or a GitHub URL.

    Args:
        value: ``owner/repo``, ``host/owner/repo`` or a URL such as
            ``https://github.com/owner/repo/pull/1``.

    Returns:
        The normalised repository, or None if the value names none.
    """
    m = re.match(r"^https?://([^/]+)/([^/]+)/([^/]+)", value)
    if m:
        return normalise_repo(m[1], f"{m[2]}/{m[3]}")
    parts = value.strip("/").split("/")
    if len(parts) == 3:
        return normalise_repo(parts[0], f"{parts[1]}/{parts[2]}")
    return normalise_repo("github.com", value)


def tokenize(command: str) -> list[str]:
    """Split a shell command into words and operators.

    Args:
        command: The command line.

    Returns:
        The tokens; operators (``&&``, ``;``, newline, ...) are tokens of
        their own.

    Raises:
        ValueError: If the command has unbalanced quotes.
    """
    lexer = shlex.shlex(
        command.replace("\\\n", " ").replace("`", " ; "),
        posix=True,
        punctuation_chars="();<>|&\n",
    )
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.whitespace_split = True
    return list(lexer)


def split_operators(tokens: Sequence[str]) -> list[str]:
    """Split operator tokens that shlex joined (``&&`` and a newline, ``);``).

    Args:
        tokens: Output of ``tokenize``.

    Returns:
        The tokens, with each operator on its own.
    """
    out: list[str] = []
    for token in tokens:
        if token and set(token) <= OPERATOR_CHARS:
            out += OPERATOR.findall(token)
        else:
            out.append(token)
    return out


def simple_commands(tokens: Sequence[str]) -> list[list[str]]:
    """Split tokens into simple commands and drop redirections.

    Args:
        tokens: Output of ``tokenize``.

    Returns:
        The words of each simple command; ``(`` and ``)`` are commands of
        their own, so that a ``cd`` in a subshell doesn't leak out of it.
    """
    commands: list[list[str]] = [[]]
    skip = False
    for token in split_operators(tokens):
        if skip:
            skip = False
        elif token in ("(", ")"):
            commands += [[token], []]
        elif token in SEPARATORS:
            commands.append([])
        elif token and set(token) <= set("<>&|"):
            # A redirection: drop the target and a file descriptor before it
            if commands[-1] and commands[-1][-1].isdigit():
                commands[-1].pop()
            skip = True
        else:
            commands[-1].append(token)
    return [c for c in commands if c]


def strip_wrappers(words: list[str], ctx: Context) -> list[str]:
    """Drop variable assignments and wrappers such as ``env`` or ``sudo``.

    Assignments are recorded in the context's environment.

    Args:
        words: The words of a simple command.
        ctx: The command's context, updated in place.

    Returns:
        The words from the command name on.
    """
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word):
            name, _, value = word.partition("=")
            ctx.env[name] = value
            i += 1
            continue
        if word in KEYWORDS:
            i += 1
            continue
        name = os.path.basename(word)
        if name not in WRAPPERS:
            break
        value_options = WRAPPERS[name]
        positionals = WRAPPER_POSITIONALS.get(name, 0)
        i += 1
        while i < len(words):
            arg = words[i]
            if arg == "--":
                i += 1
                break
            if arg.startswith("-") and arg != "-":
                i += 2 if arg in value_options else 1
            elif positionals:
                positionals -= 1
                i += 1
            else:
                break
    return words[i:]


def run_git(args: Sequence[str], ctx: Context) -> str | None:
    """Run a read-only git command to resolve configuration.

    Args:
        args: Arguments after ``git``, context options included.
        ctx: The context to run in.

    Returns:
        The stripped output, or None if git failed or is missing.
    """
    if ctx.cwd is None:
        return None
    env = {**os.environ, **ctx.env, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ctx.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def remote_repos(remote: str, git_opts: Sequence[str], ctx: Context) -> tuple[str, ...]:
    """Resolve a remote name or URL to the repositories a push reaches.

    A remote with several push URLs pushes to all of them.

    Args:
        remote: A remote name or URL.
        git_opts: Global git options of the command (``-C``, ``-c``, ...).
        ctx: The command's context.

    Returns:
        The normalised repositories; none for local paths.

    Raises:
        GateError: If the remote can't be resolved.
    """
    urls = run_git([*git_opts, "remote", "get-url", "--push", "--all", remote], ctx)
    if urls is None:
        if "/" not in remote and ":" not in remote:
            raise GateError(f"cannot resolve the remote {remote!r}")
        urls = remote
    repos = (repo_from_url(url) for url in urls.splitlines())
    return tuple(dict.fromkeys(r for r in repos if r))


def push_action(
    args: Sequence[str], git_opts: Sequence[str], ctx: Context
) -> list[Action]:
    """Turn ``git push`` arguments into an action.

    Args:
        args: Arguments after ``push``.
        git_opts: Global git options of the command.
        ctx: The command's context.

    Returns:
        The push action, or nothing for a push to a local path.

    Raises:
        GateError: If the target repository can't be determined.
    """
    remote: str | None = None
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("--repo="):
            remote = arg.partition("=")[2]
        elif arg in PUSH_VALUE_OPTIONS:
            if arg == "--repo" and i + 1 < len(args):
                remote = args[i + 1]
            i += 1
        elif not arg.startswith("-"):
            positionals.append(arg)
        i += 1
    if positionals:
        remote = positionals[0]
    if remote is None:
        remote = default_push_remote(git_opts, ctx)
    repos = remote_repos(remote, git_opts, ctx)
    return [Action("push", repos, "git push")] if repos else []


def default_push_remote(git_opts: Sequence[str], ctx: Context) -> str:
    """Find the remote that a bare ``git push`` uses.

    Args:
        git_opts: Global git options of the command.
        ctx: The command's context.

    Returns:
        The remote name.

    Raises:
        GateError: If the working directory is not a git repository.
    """
    if run_git([*git_opts, "rev-parse", "--git-dir"], ctx) is None:
        raise GateError("cannot tell which repository `git push` targets")
    branch = run_git([*git_opts, "symbolic-ref", "--short", "-q", "HEAD"], ctx)
    keys = ["remote.pushDefault"]
    if branch:
        keys = [f"branch.{branch}.pushRemote", *keys, f"branch.{branch}.remote"]
    for key in keys:
        value = run_git([*git_opts, "config", "--get", key], ctx)
        if value:
            return value
    return "origin"


def git_actions(words: Sequence[str], ctx: Context, depth: int) -> list[Action]:
    """Find gated actions in a git command.

    Args:
        words: The words from ``git`` on.
        ctx: The command's context.
        depth: The nesting depth (shell strings, aliases).

    Returns:
        The gated actions.

    Raises:
        GateError: If a gated action's target can't be determined.
    """
    git_opts: list[str] = []
    i = 1
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        name = opt.partition("=")[0]
        takes_value = opt in GIT_VALUE_OPTIONS and i + 1 < len(words)
        if name in GIT_CONTEXT_OPTIONS:
            git_opts.extend(words[i : i + 2] if takes_value else [opt])
        i += 2 if takes_value else 1
    if i >= len(words):
        return []
    sub, args = words[i], list(words[i + 1 :])
    if sub == "push":
        return push_action(args, git_opts, ctx)
    if sub in GIT_BUILTINS:
        return []
    alias = run_git([*git_opts, "config", "--get", f"alias.{sub}"], ctx)
    if not alias:
        return []
    if alias.startswith("!"):
        return actions_in(alias[1:] + " " + shlex.join(args), ctx.cwd, depth + 1)
    return git_actions(["git", *git_opts, *shlex.split(alias), *args], ctx, depth + 1)


def gh_default_repos(ctx: Context) -> tuple[str, ...]:
    """Find the repositories that gh may pick as the default.

    ``GH_REPO`` wins; then a remote marked by ``gh repo set-default``;
    otherwise every remote counts, so that the strictest profile applies.

    Args:
        ctx: The command's context.

    Returns:
        The candidate repositories.

    Raises:
        GateError: If there are none.
    """
    gh_repo = ctx.env.get("GH_REPO", os.environ.get("GH_REPO"))
    if gh_repo:
        repo = repo_from_gh_arg(gh_repo)
        if repo:
            return (repo,)
    names = (run_git(["remote"], ctx) or "").split()
    names.sort(key=lambda n: GH_REMOTE_ORDER.index(n) if n in GH_REMOTE_ORDER else 3)
    repos: list[str] = []
    for name in names:
        url = run_git(["remote", "get-url", name], ctx)
        repo = repo_from_url(url) if url else None
        if repo is None:
            continue
        if run_git(["config", "--get", f"remote.{name}.gh-resolved"], ctx) == "base":
            return (repo,)
        repos.append(repo)
    if not repos:
        raise GateError("cannot tell which repository gh targets")
    return tuple(dict.fromkeys(repos))


def split_options(
    args: Sequence[str], value_options: Iterable[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Split command arguments into options and positionals.

    Args:
        args: The arguments.
        value_options: Options that take a value.

    Returns:
        The values of each option (an empty string for flags) and the
        positional arguments.
    """
    value_options = frozenset(value_options)
    options: dict[str, list[str]] = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("--") and "=" in arg:
            name, _, value = arg.partition("=")
        elif arg in value_options:
            name, value = arg, args[i + 1] if i + 1 < len(args) else ""
            i += 1
        elif len(arg) > 2 and arg[:2] in value_options and not arg.startswith("--"):
            name, value = arg[:2], arg[2:]
        elif arg.startswith("-") and arg != "-":
            name, value = arg, ""
        else:
            positionals.append(arg)
            i += 1
            continue
        options.setdefault(name, []).append(value)
        i += 1
    return options, positionals


def gh_actions(words: Sequence[str], ctx: Context) -> list[Action]:
    """Find gated actions in a gh command.

    Args:
        words: The words from ``gh`` on.
        ctx: The command's context.

    Returns:
        The gated actions.

    Raises:
        GateError: If a gated action's target can't be determined.
    """
    head: list[str] = []
    rest = list(words[1:])
    # Move --repo/-R given before the subcommand behind it
    while rest and len(head) < 2:
        arg = rest.pop(0)
        if arg in ("-R", "--repo") and rest:
            rest += [arg, rest.pop(0)]
        elif arg.startswith(("--repo=", "-R")) and arg not in ("-R", "--repo"):
            rest.append(arg)
        else:
            head.append(arg)
    if head == ["pr", "merge"]:
        return gh_pr_merge(rest, ctx)
    if head[:1] == ["api"]:
        return gh_api(head[1:] + rest, ctx)
    return []


def gh_pr_merge(args: Sequence[str], ctx: Context) -> list[Action]:
    """Turn ``gh pr merge`` arguments into an action.

    Args:
        args: Arguments after ``merge``.
        ctx: The command's context.

    Returns:
        The merge action, or nothing for ``--disable-auto``.

    Raises:
        GateError: If the repository can't be determined.
    """
    options, positionals = split_options(args, GH_PR_MERGE_VALUE_OPTIONS)
    if "--disable-auto" in options:
        return []
    repo_values = options.get("-R", []) + options.get("--repo", [])
    repo: str | None = None
    if repo_values:
        repo = repo_from_gh_arg(repo_values[-1])
        if repo is None:
            raise GateError(f"cannot read the repository {repo_values[-1]!r}")
    elif positionals and positionals[0].startswith(("http://", "https://")):
        repo = repo_from_gh_arg(positionals[0])
    repos = (repo,) if repo else gh_default_repos(ctx)
    return [Action("merge", repos, "gh pr merge")]


def gh_api(args: Sequence[str], ctx: Context) -> list[Action]:
    """Turn ``gh api`` arguments into actions.

    Args:
        args: Arguments after ``api``.
        ctx: The command's context.

    Returns:
        The gated actions: ref changes and merges.

    Raises:
        GateError: If a gated call's repository can't be determined.
    """
    options, positionals = split_options(args, GH_API_VALUE_OPTIONS)
    if not positionals:
        return []
    fields = [v for o in GH_API_FIELD_OPTIONS for v in options.get(o, [])]
    methods = options.get("-X", []) + options.get("--method", [])
    if methods:
        method = methods[-1].upper()
    elif fields or "--input" in options:
        method = "POST"
    else:
        method = "GET"
    endpoint = re.sub(r"^https?://[^/]+/(api/v3/)?", "", positionals[0]).lstrip("/")
    endpoint = endpoint.split("?", 1)[0]
    if endpoint == "graphql":
        inputs = options.get("--input", [])
        return graphql_actions(fields + [f"query=@{i}" for i in inputs], ctx)
    m = re.match(r"^repos/([^/]+)/([^/]+)/(.+)$", endpoint)
    by_id = re.match(r"^repositories/\d+/(.+)$", endpoint)
    rest = m[3] if m else by_id[1] if by_id else None
    if rest is None:
        return []
    kinds = [k for p, ms, k in API_RULES if method in ms and p.match(rest)]
    if not kinds:
        return []
    kind = kinds[0]
    if m is None:
        return [Action(kind, (), f"gh api {method} {endpoint}")]
    owner, name = m[1], m[2]
    if any(p.startswith(("{", ":")) for p in (owner, name)):
        repos = gh_default_repos(ctx)
    else:
        repo = normalise_repo("github.com", f"{owner}/{name}")
        repos = (repo,) if repo else ()
    return [Action(kind, repos, f"gh api {method} {endpoint}")]


def graphql_actions(fields: Sequence[str], ctx: Context) -> list[Action]:
    """Check a GraphQL call for mutations that merge or change refs.

    The repository of a GraphQL mutation is given by node IDs, which the gate
    can't resolve without calling GitHub, so such a call always asks.

    Args:
        fields: The ``-f``/``-F`` values (``key=value``).
        ctx: The command's context.

    Returns:
        Nothing; read-only calls pass.

    Raises:
        GateError: If the call holds a gated mutation or its query can't be
            read.
    """
    for value in fields:
        key, _, text = value.partition("=")
        if key != "query":
            continue
        if text == "@-":
            raise GateError("cannot read a GraphQL query from stdin")
        if text.startswith("@"):
            try:
                base = ctx.cwd or Path.cwd()
                text = (base / text[1:]).read_text(encoding="utf-8")[:1_000_000]
            except (OSError, UnicodeDecodeError):
                raise GateError(f"cannot read the GraphQL query {text[1:]!r}") from None
        for mutation in GRAPHQL_MUTATIONS:
            if re.search(rf"\b{mutation}\b", text):
                raise GateError(
                    f"the GraphQL mutation {mutation} targets a repository by node ID"
                )
    return []


def actions_in(command: str, cwd: Path | None, depth: int = 0) -> list[Action]:
    """Find the gated actions in a shell command.

    Args:
        command: The command line.
        cwd: The working directory the command starts in.
        depth: The nesting depth (shell strings, aliases).

    Returns:
        The gated actions.

    Raises:
        GateError: If the command can't be read, or a gated action's target
            can't be determined.
    """
    if depth > MAX_DEPTH:
        raise GateError("the command nests too deeply to check")
    try:
        tokens = tokenize(command)
    except ValueError:
        if re.search(r"\b(git|gh)\b", command) and re.search(
            r"\b(push|merge|api)\b", command
        ):
            raise GateError("cannot parse the command") from None
        return []
    actions: list[Action] = []
    stack: list[Path | None] = []
    for words in simple_commands(tokens):
        if words == ["("]:
            stack.append(cwd)
            continue
        if words == [")"]:
            cwd = stack.pop() if stack else cwd
            continue
        ctx = Context(cwd)
        words = strip_wrappers(words, ctx)
        if not words:
            continue
        name = os.path.basename(words[0]).removesuffix(".exe")
        if name in ("cd", "pushd"):
            cwd = change_dir(cwd, words[1:])
        elif name == "popd":
            cwd = None
        elif name == "eval":
            actions += actions_in(" ".join(words[1:]), cwd, depth + 1)
        elif name in SHELLS:
            script = shell_script(words[1:])
            if script is not None:
                actions += actions_in(script, cwd, depth + 1)
        elif name == "git":
            actions += git_actions(words, ctx, depth)
        elif name == "gh":
            actions += gh_actions(words, ctx)
    return actions


def change_dir(cwd: Path | None, args: Sequence[str]) -> Path | None:
    """Follow a ``cd`` within a command.

    Args:
        cwd: The current directory, or None if unknown.
        args: The arguments of ``cd``.

    Returns:
        The new directory, or None if it can't be told.
    """
    args = [a for a in args if a not in ("-L", "-P", "--")]
    if not args:
        return Path.home()
    target = args[0]
    if target == "-" or "$" in target or cwd is None:
        return None
    return (cwd / Path(target).expanduser()).resolve()


def shell_script(args: Sequence[str]) -> str | None:
    """Get the script of ``bash -c <script>`` and the like.

    Args:
        args: The arguments of the shell.

    Returns:
        The script, or None if the shell doesn't run one given by ``-c``.
    """
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return args[i + 1] if i + 1 < len(args) else None
        if not arg.startswith("-"):
            return None
    return None


def load_profiles(path: Path) -> Profiles:
    """Read and check ``profiles.yaml``.

    Args:
        path: The file.

    Returns:
        The parsed profiles.

    Raises:
        GateError: If the file can't be read or is invalid.
    """
    try:
        import yaml
    except ImportError:
        raise GateError(
            "PyYAML is missing; install it (`pip install pyyaml`) or uv"
        ) from None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GateError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise GateError(f"{path} is not a mapping")
    definitions = data.get("profiles")
    definitions = {} if definitions is None else definitions
    if not isinstance(definitions, dict):
        raise GateError(f"{path}: `profiles` is not a mapping")
    allowed: dict[str, frozenset[str]] = {}
    for name in set(PROFILES) | set(definitions):
        definition = definitions.get(name) or {}
        if not isinstance(definition, dict):
            raise GateError(f"{path}: profile {name!r} is not a mapping")
        allowed[name] = allowed_actions(definition, f"profile {name!r}", path)
    listed = data.get("repositories")
    listed = [] if listed is None else listed
    mapped = data.get("repos")
    mapped = {} if mapped is None else mapped
    if not isinstance(listed, list) or not isinstance(mapped, dict):
        raise GateError(f"{path}: `repositories` must be a list and `repos` a mapping")
    entries_listed = tuple(
        repo_entry(
            item.get("match") if isinstance(item, dict) else None, item, allowed, path
        )
        for item in listed
    )
    entries_mapped = tuple(
        repo_entry(key, value, allowed, path) for key, value in mapped.items()
    )
    unknown = data.get("unknown", "ask")
    if unknown not in ("ask", "deny"):
        raise GateError(f"{path}: `unknown` must be ask or deny")
    return Profiles(path, entries_listed, entries_mapped, allowed, unknown)


def allowed_actions(data: Mapping[str, Any], where: str, path: Path) -> frozenset[str]:
    """Read and check an ``allowed_actions`` field.

    Args:
        data: The profile definition or repository entry.
        where: What the data is, for messages.
        path: The file, for messages.

    Returns:
        The allowed actions.

    Raises:
        GateError: If the field is not a list of known actions.
    """
    value = data.get("allowed_actions") or []
    if not isinstance(value, list) or not set(value) <= set(ACTIONS):
        raise GateError(
            f"{path}: {where}: `allowed_actions` must be a list of {', '.join(ACTIONS)}"
        )
    return frozenset(value)


def repo_entry(
    pattern: object, data: object, allowed: Mapping[str, frozenset[str]], path: Path
) -> RepoEntry:
    """Check one repository entry.

    Args:
        pattern: The ``match`` value or the ``repos`` key.
        data: The entry.
        allowed: The known profiles and what they allow.
        path: The file, for messages.

    Returns:
        The entry.

    Raises:
        GateError: If the entry is invalid.
    """
    if not isinstance(pattern, str) or not isinstance(data, dict):
        raise GateError(f"{path}: invalid repository entry {pattern!r}")
    profile = data.get("profile")
    if profile not in allowed:
        raise GateError(f"{path}: {pattern}: unknown profile {profile!r}")
    return RepoEntry(
        pattern.lower(), str(profile), data, allowed_actions(data, pattern, path)
    )


def names(value: object, fallback: str) -> str:
    """Format the people of a profile field for a message.

    Args:
        value: A login, a list of logins, or nothing.
        fallback: What to say when the field is empty.

    Returns:
        The logins, joined with commas, or the fallback.
    """
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return str(value) if value else fallback


def decide(
    action: Action, repo: str, entry: RepoEntry, profiles: Profiles
) -> Decision | None:
    """Apply a repository's profile to an action.

    Args:
        action: The action.
        repo: The repository.
        entry: The repository's entry.
        profiles: All profiles, for what each allows.

    Returns:
        The verdict, or None if the profile allows the action.
    """
    data = entry.data
    allowed = profiles.allowed[entry.profile] | entry.allowed
    if action.kind in allowed:
        return None
    where = f"{action.what} in {repo} ({entry.profile})"
    if entry.profile == "contributor":
        if action.kind == "push":
            return Decision(
                "deny",
                f"{where}: contributors push to their fork only; push the branch to "
                "your fork and open a PR from there",
            )
        return Decision(
            "deny", f"{where}: contributors don't merge; the maintainers do"
        )
    if action.kind == "push":
        return None
    if entry.profile == "owner-blessed-maintainer":
        who = names(data.get("maintainer"), "the blessed maintainer")
        return Decision(
            "ask",
            f"{where}: involve {who} before merging; overrule only when they are "
            "unavailable, under time pressure or without consensus",
        )
    if entry.profile == "owner-shared":
        who = names(data.get("co_owners"), "the co-owners")
        return Decision("ask", f"{where}: merging needs consensus with {who}")
    if entry.profile == "maintainer":
        who = names(data.get("owner"), "the owner")
        return Decision(
            "ask",
            f"{where}: merges are not among your responsibilities; ask {who}",
        )
    if entry.profile == "developer":
        who = names(data.get("owner"), "the maintainers")
        return Decision("ask", f"{where}: the maintainers decide; ask {who}")
    return None


def state_file() -> Path:
    """Find the file that records unknown repositories already asked about.

    Returns:
        The path; its directory may not exist yet.
    """
    base = os.environ.get(STATE_ENV) or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not base:
        xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        base = str(Path(xdg) / "lloping-llama")
    return Path(base) / "profile-gate-asked.json"


def first_ask(session: str, repo: str) -> bool:
    """Record that the gate asks about an unknown repository in a session.

    Args:
        session: The session ID; without one, every call counts as first.
        repo: The repository.

    Returns:
        Whether this is the first ask for the repository in the session.
    """
    if not session:
        return True
    path = state_file()
    try:
        asked = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(asked, dict):
            asked = {}
    except (OSError, ValueError):
        asked = {}
    repos = asked.pop(session, [])
    if not isinstance(repos, list):
        repos = []
    if repo in repos:
        asked[session] = repos
        return False
    asked[session] = [*repos, repo]
    while len(asked) > MAX_SESSIONS:
        asked.pop(next(iter(asked)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8", suffix=".tmp"
        ) as tmp:
            json.dump(asked, tmp)
        os.replace(tmp.name, path)
    except OSError:
        pass
    return True


def evaluate(
    command: str, cwd: Path | None, session: str, config: Path
) -> Decision | None:
    """Decide on a Bash command.

    Args:
        command: The command line.
        cwd: The working directory.
        session: The session ID.
        config: The path of ``profiles.yaml``.

    Returns:
        The strictest verdict over all gated actions, or None if the command
        has none or the profiles allow them all.
    """
    try:
        actions = actions_in(command, cwd)
        if not actions:
            return None
        profiles = load_profiles(config)
    except GateError as exc:
        return Decision("ask", PREFIX + str(exc))
    decisions: list[Decision] = []
    for action in actions:
        if not action.repos:
            decisions.append(
                Decision(
                    "ask", f"{PREFIX}cannot tell which repository {action.what} targets"
                )
            )
        for repo in action.repos:
            entry = profiles.lookup(repo)
            if entry is None and profiles.unknown == "deny":
                decisions.append(
                    Decision(
                        "deny",
                        f"{PREFIX}{repo} has no profile; record one in {profiles.path}",
                    )
                )
                continue
            if entry is None:
                if first_ask(session, repo):
                    decisions.append(
                        Decision(
                            "ask",
                            f"{PREFIX}{repo} has no profile; record one in "
                            f"{profiles.path}, then go on",
                        )
                    )
                continue
            decision = decide(action, repo, entry, profiles)
            if decision:
                decisions.append(Decision(decision.verdict, PREFIX + decision.reason))
    if not decisions:
        return None
    verdict: Verdict = "deny" if any(d.verdict == "deny" for d in decisions) else "ask"
    reasons = dict.fromkeys(d.reason for d in decisions if d.verdict == verdict)
    return Decision(verdict, "; ".join(reasons))


def main(stdin: str, environ: Mapping[str, str]) -> str:
    """Run the hook.

    Args:
        stdin: The hook input (JSON).
        environ: The environment.

    Returns:
        The hook output (JSON), or an empty string for no objection.
    """
    config = environ.get(CONFIG_ENV)
    if not config:
        return ""
    try:
        payload = json.loads(stdin)
        if payload.get("tool_name") != "Bash":
            return ""
        command = payload.get("tool_input", {}).get("command", "")
        if not isinstance(command, str) or not re.search(r"\b(git|gh)\b", command):
            return ""
        cwd = Path(payload.get("cwd") or os.getcwd())
        session = str(payload.get("session_id") or "")
        decision = evaluate(command, cwd, session, Path(config).expanduser())
    except Exception as exc:
        decision = Decision("ask", f"{PREFIX}internal error: {exc!r}")
    if decision is None:
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision.verdict,
                "permissionDecisionReason": decision.reason,
            }
        }
    )


if __name__ == "__main__":  # pragma: no cover
    sys.stdout.write(main(sys.stdin.read(), os.environ))
