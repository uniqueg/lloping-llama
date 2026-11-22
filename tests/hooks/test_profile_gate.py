"""Tests of the profile-gate hook."""

from __future__ import annotations

import builtins
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import profile_gate as pg
from conftest import git

Result = tuple[str, str] | None


def run(
    command: str,
    cwd: Path,
    config: Path,
    session: str = "s1",
    tool: str = "Bash",
) -> Result:
    """Run the hook on a command.

    Args:
        command: The Bash command.
        cwd: The working directory.
        config: The profiles file.
        session: The session ID.
        tool: The tool name.

    Returns:
        The verdict and the reason, or None for no objection.
    """
    payload = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "cwd": str(cwd),
        "session_id": session,
    }
    out = pg.main(json.dumps(payload), {pg.CONFIG_ENV: str(config)})
    if not out:
        return None
    spec = json.loads(out)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    return spec["permissionDecision"], spec["permissionDecisionReason"]


# Input handling


def test_no_config_does_nothing(tmp_path: Path) -> None:
    """Without the environment variable, the hook stays silent."""
    assert pg.main('{"tool_name": "Bash"}', {}) == ""


def test_other_tools_and_commands_pass(tmp_path: Path, profiles_list: Path) -> None:
    """Non-Bash tools and commands without git or gh pass."""
    assert run("git push", tmp_path, profiles_list, tool="Read") is None
    assert run("ls -la", tmp_path, profiles_list) is None


def test_invalid_input_asks(profiles_list: Path) -> None:
    """Unreadable hook input fails closed."""
    out = pg.main("not json", {pg.CONFIG_ENV: str(profiles_list)})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "internal error" in out


def test_missing_cwd_uses_process_cwd(
    make_repo: Callable[..., Path], profiles_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input without cwd falls back to the hook's working directory."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    monkeypatch.chdir(repo)
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
    out = pg.main(json.dumps(payload), {pg.CONFIG_ENV: str(profiles_list)})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# git push


@pytest.mark.parametrize(
    ("url", "verdict"),
    [
        ("git@github.com:upstream-org/tool.git", "deny"),
        ("https://github.com/Upstream-Org/Tool", "deny"),
        ("ssh://git@github.com:22/upstream-org/tool.git", "deny"),
        ("https://user@www.github.com/upstream-org/tool/", "deny"),
        ("git@gitlab.example.com:team/project.git", "deny"),
        ("git@github.com:you/tool.git", None),
        ("git@github.com:example-org/example-site.git", None),
    ],
)
def test_push_by_profile(
    make_repo: Callable[..., Path], profiles_list: Path, url: str, verdict: str | None
) -> None:
    """Contributors can't push upstream; other profiles may push."""
    repo = make_repo(origin=url)
    result = run("git push origin feature", repo, profiles_list)
    assert (result[0] if result else None) == verdict
    if result:
        assert "fork" in result[1]


def test_push_uses_push_url_and_default_remote(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """The push URL and the configured push remote decide the target."""
    repo = make_repo(
        origin="git@github.com:you/tool.git",
        upstream="git@github.com:upstream-org/tool.git",
    )
    assert run("git push", repo, profiles_list) is None
    git(repo, "config", "remote.pushDefault", "upstream")
    assert run("git push", repo, profiles_list) == (
        "deny",
        "profile-gate: git push in upstream-org/tool (contributor): contributors push "
        "to their fork only; push the branch to your fork and open a PR from there",
    )
    git(repo, "config", "remote.origin.pushurl", "git@github.com:upstream-org/x.git")
    assert run("git push origin", repo, profiles_list) is not None


def test_push_branch_remote(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """A branch's push remote and remote are used for a bare push."""
    repo = make_repo(
        origin="git@github.com:you/tool.git",
        upstream="git@github.com:upstream-org/tool.git",
    )
    git(repo, "config", "branch.main.remote", "upstream")
    assert run("git push", repo, profiles_list) is not None
    git(repo, "config", "branch.main.pushRemote", "origin")
    assert run("git push", repo, profiles_list) is None


def test_push_detached_head(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """A bare push without a current branch uses origin."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    git(repo, "checkout", "-q", "--detach")
    assert run("git push", repo, profiles_list) is not None


def test_push_options_and_repo(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """Options with values are skipped; --repo names the target."""
    repo = make_repo(
        origin="git@github.com:you/tool.git",
        upstream="git@github.com:upstream-org/tool.git",
    )
    assert (
        run("git push -o ci.skip --force-with-lease origin x", repo, profiles_list)
        is None
    )
    assert run("git push --repo upstream", repo, profiles_list) is not None
    assert run("git push --repo=upstream", repo, profiles_list) is not None
    assert run("git push --receive-pack", repo, profiles_list) is None
    assert run("git push -- upstream main", repo, profiles_list) is not None


def test_push_to_url_and_path(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """URLs are checked directly; local paths pass."""
    repo = make_repo()
    url = "https://github.com/upstream-org/tool.git"
    assert run(f"git push {url} main", repo, profiles_list) is not None
    assert run("git push ../other main", repo, profiles_list) is None
    assert run("git push /srv/git/x.git main", repo, profiles_list) is None


def test_push_unknown_remote_asks(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """A remote name that doesn't resolve fails closed."""
    repo = make_repo()
    result = run("git push nowhere", repo, profiles_list)
    assert result == ("ask", "profile-gate: cannot resolve the remote 'nowhere'")


def test_push_outside_repo_asks(tmp_path: Path, profiles_list: Path) -> None:
    """A bare push outside a repository fails closed."""
    result = run("git push", tmp_path, profiles_list)
    assert result is not None and "cannot tell" in result[1]


def test_git_global_options(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """-C, -c, --git-dir and other global options are honoured."""
    repo = make_repo("up", origin="git@github.com:upstream-org/tool.git")
    other = make_repo("other", origin="git@github.com:you/tool.git")
    assert run(f"git -C {repo} push", other, profiles_list) is not None
    assert run("git -C ../up push", other, profiles_list) is not None
    assert run(f"git --git-dir={repo}/.git push", other, profiles_list) is not None
    assert (
        run("git --no-pager -c push.default=simple push", repo, profiles_list)
        is not None
    )
    url = "remote.origin.url=git@github.com:upstream-org/tool.git"
    assert run(f"git -c {url} push", other, profiles_list) is not None
    assert run("git --namespace x push", repo, profiles_list) is not None
    assert run("git --version", repo, profiles_list) is None
    assert run("git", repo, profiles_list) is None


def test_git_read_commands_pass(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """Built-in git commands other than push pass without lookups."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    assert run("git status && git log -1 && git fetch", repo, profiles_list) is None


def test_git_aliases(make_repo: Callable[..., Path], profiles_list: Path) -> None:
    """Aliases are resolved, shell aliases included."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    git(repo, "config", "alias.p", "push --force-with-lease")
    git(repo, "config", "alias.sp", "!git status && git push")
    git(repo, "config", "alias.st", "status -sb")
    assert run("git p", repo, profiles_list) is not None
    assert run("git sp", repo, profiles_list) is not None
    assert run("git st", repo, profiles_list) is None
    assert run("git nosuchalias", repo, profiles_list) is None


def test_git_alias_loop_asks(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """Recursive aliases stop at the depth limit and ask."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    git(repo, "config", "alias.loop", "!git loop")
    result = run("git loop", repo, profiles_list)
    assert result is not None and "nests too deeply" in result[1]


# Shell syntax


def test_shell_syntax(make_repo: Callable[..., Path], profiles_list: Path) -> None:
    """Chains, wrappers, shells, eval and redirections are looked through."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    for command in [
        "git status; git push",
        "git status\ngit push",
        "echo x | git push 2>&1",
        "GIT_TRACE=1 env -u X sudo -u me nice -n 5 timeout 10 /usr/bin/git push",
        "command git push >/dev/null",
        "bash -lc 'git push'",
        "bash --norc -c 'git push'",
        "eval git push",
        "echo $(git push)",
        "git push \\\n  origin",
        "env -- git push",
    ]:
        assert run(command, repo, profiles_list) is not None, command
    for command in [
        "bash script.sh git push",
        "bash -c",
        "sh",
        "echo git push",
        "timeout 5",
        "echo hi > out && git status",
        "git status; X=1",
        "git status; timeout 5",
        "git status; ( )",
        "git status; sh -e",
    ]:
        assert run(command, repo, profiles_list) is None, command


def test_cd_is_followed(make_repo: Callable[..., Path], profiles_list: Path) -> None:
    """Cd changes the directory of later commands, but not out of a subshell."""
    up = make_repo("up", origin="git@github.com:upstream-org/tool.git")
    mine = make_repo("mine", origin="git@github.com:you/tool.git")
    assert run("cd ../up && git push", mine, profiles_list) is not None
    assert run("pushd -P ../up && git push", mine, profiles_list) is not None
    assert run("(cd ../up && git status); git push", mine, profiles_list) is None
    assert run("cd ../mine; git push", up, profiles_list) is None
    assert run(") git push", mine, profiles_list) is None


def test_cd_unknown_asks(
    make_repo: Callable[..., Path], profiles_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cd -, cd $VAR and cd without arguments leave the directory unknown or home."""
    repo = make_repo(origin="git@github.com:you/tool.git")
    for command in ["cd - && git push", "cd $X && git push", "cd -; cd sub; git push"]:
        result = run(command, repo, profiles_list)
        assert result is not None and result[0] == "ask", command
    monkeypatch.setenv("HOME", str(repo))
    assert run("cd && git push", repo.parent, profiles_list) is None


def test_unparsable_command(tmp_path: Path, profiles_list: Path) -> None:
    """Unbalanced quotes ask only when the command looks gated."""
    assert run("git push 'oops", tmp_path, profiles_list) == (
        "ask",
        "profile-gate: cannot parse the command",
    )
    assert run("git log 'oops", tmp_path, profiles_list) is None


# gh pr merge


@pytest.mark.parametrize(
    ("repo", "verdict", "needle"),
    [
        ("example-org/example-site", "ask", "involve alice"),
        ("example-org/example-sdk", "ask", "consensus with bob, dave"),
        ("other-org/example-service", "ask", "ask carol"),
        ("other-org/example-tool", "ask", "maintainers decide; ask carol"),
        ("upstream-org/tool", "deny", "don't merge"),
        ("other-org/merge-ok", None, ""),
        ("you/tool", None, ""),
        ("github.com/you/tool", None, ""),
        ("https://github.com/upstream-org/tool", "deny", "don't merge"),
    ],
)
def test_merge_by_profile(
    tmp_path: Path, profiles_list: Path, repo: str, verdict: str | None, needle: str
) -> None:
    """Merges ask or deny by profile, naming the person to involve."""
    result = run(f"gh pr merge 5 --squash -R {repo}", tmp_path, profiles_list)
    assert (result[0] if result else None) == verdict
    if result:
        assert needle in result[1]


def test_merge_profile_fallback_names(tmp_path: Path, profiles_mapping: Path) -> None:
    """Without people in the entry, the messages name the role."""
    expected = {
        "other-org/blessed": "involve the blessed maintainer",
        "other-org/shared": "consensus with the co-owners",
        "other-org/maintained": "ask the owner",
    }
    for repo, needle in expected.items():
        result = run(f"gh pr merge --repo={repo}", tmp_path, profiles_mapping)
        assert result is not None and needle in result[1]


def test_profile_level_allowed_actions(tmp_path: Path, profiles_mapping: Path) -> None:
    """allowed_actions in a profile definition applies to all its repositories."""
    assert run("gh pr merge -R other-org/dev", tmp_path, profiles_mapping) is None


def test_merge_url_and_flags(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """A PR URL names the repository; --disable-auto is no merge."""
    repo = make_repo(origin="git@github.com:you/tool.git")
    url = "https://github.com/upstream-org/tool/pull/3"
    assert run(f"gh pr merge {url} --auto -b 'x y'", repo, profiles_list) is not None
    assert run(f"gh pr merge {url} --disable-auto", repo, profiles_list) is None
    assert run("gh pr merge -Rupstream-org/tool", repo, profiles_list) is not None
    assert run("gh pr merge -- 5", repo, profiles_list) is None
    assert run("gh pr merge - 5", repo, profiles_list) is None
    assert run("gh pr merge 5 -b", repo, profiles_list) is None


def test_merge_bad_repo_value_asks(tmp_path: Path, profiles_list: Path) -> None:
    """A --repo value that names no repository fails closed."""
    result = run("gh pr merge -R justaname", tmp_path, profiles_list)
    assert result == ("ask", "profile-gate: cannot read the repository 'justaname'")


def test_merge_default_repo(
    make_repo: Callable[..., Path], profiles_list: Path
) -> None:
    """The default repository: GH_REPO, gh-resolved, else the strictest remote."""
    repo = make_repo(
        origin="git@github.com:you/tool.git",
        upstream="git@github.com:upstream-org/tool.git",
        local="/srv/git/x.git",
    )
    assert run("gh pr merge 1", repo, profiles_list) is not None
    git(repo, "config", "remote.origin.gh-resolved", "base")
    assert run("gh pr merge 1", repo, profiles_list) is None
    assert (
        run("GH_REPO=upstream-org/tool gh pr merge 1", repo, profiles_list) is not None
    )
    assert run("GH_REPO=x gh pr merge 1", repo, profiles_list) is None


def test_merge_gh_repo_from_environment(
    tmp_path: Path, profiles_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH_REPO in the hook's environment counts."""
    monkeypatch.setenv("GH_REPO", "upstream-org/tool")
    assert run("gh pr merge 1", tmp_path, profiles_list) is not None


def test_merge_without_remotes_asks(tmp_path: Path, profiles_list: Path) -> None:
    """Gh outside a repository with remotes fails closed."""
    result = run("gh pr merge 1", tmp_path, profiles_list)
    assert result == ("ask", "profile-gate: cannot tell which repository gh targets")


def test_other_gh_commands_pass(tmp_path: Path, profiles_list: Path) -> None:
    """Gh commands other than pr merge and api pass."""
    assert run("gh pr view 1 -R upstream-org/tool", tmp_path, profiles_list) is None
    assert run("gh", tmp_path, profiles_list) is None


# gh api


@pytest.mark.parametrize(
    "args",
    [
        "-X PUT repos/upstream-org/tool/pulls/1/merge",
        "--method=put /repos/upstream-org/tool/pulls/1/merge",
        "-XPUT https://api.github.com/repos/upstream-org/tool/pulls/1/merge",
        "repos/upstream-org/tool/merges -f base=main -f head=x",
        "repos/upstream-org/tool/git/refs -f ref=refs/heads/x -f sha=abc",
        "-X DELETE repos/upstream-org/tool/git/refs/heads/x",
        "-X PATCH repos/upstream-org/tool/git/refs/heads/x?force=1 -F sha=abc",
        "-X PUT repos/upstream-org/tool/contents/README.md --input body.json",
        "-X POST repos/upstream-org/tool/branches/main/rename -f new_name=x",
        "-X PUT repos/upstream-org/tool/pulls/1/update-branch",
    ],
)
def test_api_gated(tmp_path: Path, profiles_list: Path, args: str) -> None:
    """API calls that merge or change refs are gated."""
    result = run(f"gh api {args}", tmp_path, profiles_list)
    assert result is not None and result[0] == "deny"


@pytest.mark.parametrize(
    "args",
    [
        "repos/upstream-org/tool/pulls/1/merge",
        "repos/upstream-org/tool/git/refs",
        "-X POST repos/upstream-org/tool/issues -f title=x",
        "user",
        "--jq .x",
        "repos/upstream-org",
    ],
)
def test_api_ungated(tmp_path: Path, profiles_list: Path, args: str) -> None:
    """Reads and calls that change nothing gated pass."""
    assert run(f"gh api {args}", tmp_path, profiles_list) is None


def test_api_placeholders(make_repo: Callable[..., Path], profiles_list: Path) -> None:
    """{owner}/{repo} and :owner/:repo resolve to the default repository."""
    repo = make_repo(origin="git@github.com:upstream-org/tool.git")
    for endpoint in ["repos/{owner}/{repo}/merges", "repos/:owner/:repo/merges"]:
        result = run(f"gh api {endpoint} -f base=main", repo, profiles_list)
        assert result is not None and result[0] == "deny"


def test_api_bad_repo_asks(tmp_path: Path, profiles_list: Path) -> None:
    """An endpoint whose owner/repo can't be normalised asks."""
    result = run("gh api -X PUT repos/a/.git/pulls/1/merge", tmp_path, profiles_list)
    assert result is not None and "cannot tell which repository" in result[1]


def test_graphql(tmp_path: Path, profiles_list: Path) -> None:
    """GraphQL mutations that merge or change refs ask; reads pass."""
    merge = "gh api graphql -f query='mutation { mergePullRequest(input: {}) { x } }'"
    result = run(merge, tmp_path, profiles_list)
    assert result is not None and "mergePullRequest" in result[1]
    read = "gh api graphql -F owner=x -f query='{ viewer { login } }'"
    assert run(read, tmp_path, profiles_list) is None
    (tmp_path / "q.graphql").write_text("mutation { createRef(input: {}) { x } }")
    assert (
        run("gh api graphql -F query=@q.graphql", tmp_path, profiles_list) is not None
    )
    (tmp_path / "r.graphql").write_text("{ viewer { login } }")
    assert run("gh api graphql -F query=@r.graphql", tmp_path, profiles_list) is None
    result = run("gh api graphql -F query=@missing.graphql", tmp_path, profiles_list)
    assert result is not None and "cannot read the GraphQL query" in result[1]


# Unknown repositories


def test_unknown_repo_asks_once(tmp_path: Path, profiles_list: Path) -> None:
    """An unknown repository asks once per session, then defers."""
    command = "gh pr merge -R nobody/thing"
    result = run(command, tmp_path, profiles_list)
    assert result is not None and result[0] == "ask"
    assert "nobody/thing has no profile; record one in" in result[1]
    assert run(command, tmp_path, profiles_list) is None
    assert run(command, tmp_path, profiles_list, session="s2") is not None
    assert run(command, tmp_path, profiles_list, session="") is not None
    assert run(command, tmp_path, profiles_list, session="") is not None


def test_state_file_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state file follows the state, plugin data and XDG directories."""
    assert pg.state_file() == tmp_path / "state" / "profile-gate-asked.json"
    monkeypatch.delenv(pg.STATE_ENV)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert pg.state_file().parent == tmp_path / "data"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert pg.state_file().parent == tmp_path / "xdg" / "lloping-llama"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert pg.state_file().parent == Path.home() / ".local" / "state" / "lloping-llama"


def test_state_file_robustness(tmp_path: Path) -> None:
    """Corrupt state is replaced; old sessions are pruned; write errors pass."""
    path = pg.state_file()
    path.parent.mkdir()
    path.write_text("[]")
    assert pg.first_ask("s", "a/b")
    path.write_text('{"s": "x"}')
    assert pg.first_ask("s", "a/b")
    path.write_text("garbage")
    assert pg.first_ask("s", "a/b")
    for i in range(pg.MAX_SESSIONS + 5):
        pg.first_ask(f"n{i}", "a/b")
    asked = json.loads(path.read_text())
    assert len(asked) == pg.MAX_SESSIONS and "n0" not in asked
    path.unlink()
    path.parent.chmod(0o500)
    try:
        assert pg.first_ask("s", "a/b")
    finally:
        path.parent.chmod(0o700)


# Profiles file


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("- a\n", "is not a mapping"),
        ("profiles: []\n", "`profiles` is not a mapping"),
        ("profiles: {owner-sole: x}\n", "profile 'owner-sole' is not a mapping"),
        (
            "profiles: {owner-sole: {allowed_actions: [fly]}}\n",
            "`allowed_actions` must",
        ),
        ("repositories: {}\n", "`repositories` must be a list"),
        ("repos: []\n", "`repos` a mapping"),
        ("repositories: [x]\n", "invalid repository entry None"),
        ("repos: {a/b: x}\n", "invalid repository entry 'a/b'"),
        ("repos: {a/b: {profile: boss}}\n", "unknown profile 'boss'"),
        (
            "repos: {a/b: {profile: owner-sole, allowed_actions: merge}}\n",
            "a/b: `allowed",
        ),
        ("a: [\n", "cannot read"),
    ],
)
def test_invalid_profiles(tmp_path: Path, text: str, needle: str) -> None:
    """An invalid profiles file asks on gated actions, saying what's wrong."""
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    result = run("gh pr merge -R a/b", tmp_path, path)
    assert result is not None and result[0] == "ask"
    assert needle in result[1]


def test_missing_profiles_file(tmp_path: Path) -> None:
    """A missing profiles file asks on gated actions only."""
    path = tmp_path / "missing.yaml"
    assert run("gh pr view", tmp_path, path) is None
    result = run("gh pr merge -R a/b", tmp_path, path)
    assert result is not None and "cannot read" in result[1]


def test_custom_profile(tmp_path: Path) -> None:
    """Profiles defined beyond the six raise no verdict of their own."""
    path = tmp_path / "custom.yaml"
    path.write_text("profiles: {guest: {}}\nrepos: {a/b: {profile: guest}}\n")
    assert run("gh pr merge -R a/b", tmp_path, path) is None


def test_empty_profiles_file(tmp_path: Path) -> None:
    """An empty mapping makes every repository unknown."""
    path = tmp_path / "empty.yaml"
    path.write_text("version: 1\n")
    result = run("gh pr merge -R a/b", tmp_path, path)
    assert result is not None and "has no profile" in result[1]


def test_mapping_lookup_order(tmp_path: Path, profiles_mapping: Path) -> None:
    """In ``repos``, an exact name wins over an earlier glob."""
    result = run("gh pr merge -R zzz/y", tmp_path, profiles_mapping)
    assert result is not None and "has no profile" in result[1]
    assert (
        run("gh pr merge -R upstream-org/api", tmp_path, profiles_mapping) is not None
    )
    assert run("gh pr merge -R upstream-org/other", tmp_path, profiles_mapping) is None


def test_missing_pyyaml_asks(
    tmp_path: Path, profiles_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without PyYAML, gated actions ask with a hint."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = run("gh pr merge -R a/b", tmp_path, profiles_list)
    assert result is not None and "PyYAML is missing" in result[1]


# Combined verdicts and helpers


def test_strictest_verdict_wins(tmp_path: Path, profiles_list: Path) -> None:
    """Deny beats ask; reasons of the same verdict are joined once."""
    command = (
        "gh pr merge -R example-org/example-site && gh pr merge -R upstream-org/a"
        " && gh pr merge -R upstream-org/b"
    )
    result = run(command, tmp_path, profiles_list)
    assert result is not None and result[0] == "deny"
    assert "upstream-org/a" in result[1] and "upstream-org/b" in result[1]
    assert "alice" not in result[1]


def test_git_failure_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_git returns None when git can't run or the directory is unknown."""
    ctx = pg.Context(tmp_path)
    assert pg.run_git(["--version"], pg.Context(None)) is None

    def broken(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert pg.run_git(["--version"], ctx) is None


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("git@github.com:a/b.git", "a/b"),
        ("https://github.com/a/b/c", None),
        ("C:\\work\\repo", None),
        ("relative/path", None),
    ],
)
def test_repo_from_url(url: str, repo: str | None) -> None:
    """Remote URLs map to owner/repo; other shapes to None."""
    assert pg.repo_from_url(url) == repo


def test_glob_match() -> None:
    """Globs don't cross a slash."""
    assert pg.glob_match("a/*", "a/b")
    assert not pg.glob_match("*", "a/b")
    assert pg.glob_match("a/b?", "a/bc")
    assert not pg.glob_match("a/b?", "a/b/")
