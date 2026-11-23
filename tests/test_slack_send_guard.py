"""Tests of the Slack send guard hook (``hooks/slack_send_guard.py``)."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import slack_send_guard as guard

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hooks" / "slack_send_guard.py"
TEMPLATE = ROOT / "templates" / "overlay" / "me" / "people.md"
NOTE = (
    "(Sent automatically by Claude on behalf of Alice, without their review; "
    "please treat with care.)"
)
MESSAGE = f"Hi Bob\n\n{NOTE}"
PEOPLE = """---
owner: Alice
---

<!-- Fill-in instructions: these lines never count
## Mallory
- slack: U9
- send: auto
-->

## Bob Example

- name: Bob
- slack: `U0000000002`
- dm: D0000000002
- send: auto

## Carol Example

- slack: U0000000003
- send: draft

## Dave Example

- slack: U0000000004

## Erin Example

- slack: U0000000005
- send: AUTO

## Erin Again

- slack: U0000000005
- send: draft
"""


def event(
    tool_input: dict[str, Any], tool: object = "mcp__slack__slack_send_message"
) -> dict[str, Any]:
    """Build a PreToolUse event.

    Args:
        tool_input: The tool call's arguments.
        tool: The tool name.

    Returns:
        The hook input.
    """
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    """Write the example people file and point the hook at it.

    Args:
        tmp_path: pytest's temporary directory.

    Returns:
        An environment with ``LLOPING_LLAMA_PEOPLE`` set.
    """
    path = tmp_path / "people.md"
    path.write_text(PEOPLE, encoding="utf-8")
    return {"LLOPING_LLAMA_PEOPLE": str(path)}


def run(payload: str, environ: dict[str, str]) -> tuple[int, str, str]:
    """Run ``main`` on a raw payload.

    Args:
        payload: The text on stdin.
        environ: The environment variables.

    Returns:
        The exit status, stdout and stderr.
    """
    stdout, stderr = io.StringIO(), io.StringIO()
    status = guard.main(io.StringIO(payload), stdout, stderr, environ)
    return status, stdout.getvalue(), stderr.getvalue()


def reason_of(payload: object, environ: dict[str, str]) -> str | None:
    """Run the hook and return the deny reason, if any.

    Args:
        payload: The hook input.
        environ: The environment variables.

    Returns:
        The deny reason, or None if the call may go ahead.
    """
    status, out, err = run(json.dumps(payload), environ)
    assert status == 0
    assert err == ""
    if not out:
        return None
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "slack_send_message_draft" in reason
    return str(reason)


class TestAllowed:
    """Calls that go ahead."""

    @pytest.mark.parametrize("target", ["U0000000002", "D0000000002", " u0000000002 "])
    def test_auto_person_with_note(self, environ: dict[str, str], target: str) -> None:
        """A message to an auto person's member or DM ID that ends with the note passes."""
        payload = event({"channel_id": target, "message": f"Hi Bob\n\n{NOTE}\n"})
        assert reason_of(payload, environ) is None

    def test_scheduled_message(self, environ: dict[str, str]) -> None:
        """Scheduled messages follow the same rules."""
        payload = event(
            {"channel_id": "U0000000002", "message": MESSAGE, "post_at": 1},
            tool="mcp__claude_ai_Slack__slack_schedule_message",
        )
        assert reason_of(payload, environ) is None

    def test_thread_reply_in_dm(self, environ: dict[str, str]) -> None:
        """A thread reply in an auto person's DM passes."""
        payload = event({"channel_id": "D0000000002", "message": MESSAGE, "thread_ts": "1.2"})
        assert reason_of(payload, environ) is None

    @pytest.mark.parametrize("tool", ["mcp__slack__slack_send_message_draft", "Bash"])
    def test_other_tools(self, environ: dict[str, str], tool: object) -> None:
        """Drafts and other tools are never blocked, even to channels."""
        payload = event({"channel_id": "C1", "message": "x"}, tool=tool)
        assert reason_of(payload, {}) is None

    def test_custom_note(self, tmp_path: Path) -> None:
        """The front matter can replace the note; ``{owner}`` is filled in."""
        path = tmp_path / "people.md"
        path.write_text(
            '---\nowner: Alice\nauto-note: "-- sent by a bot for {owner}"\n---\n'
            "## Bob\n- slack: U2\n- send: auto\n",
            encoding="utf-8",
        )
        environ = {"LLOPING_LLAMA_PEOPLE": str(path)}
        payload = event({"channel_id": "U2", "message": "Hi\n-- sent by a bot for Alice"})
        assert reason_of(payload, environ) is None
        payload = event({"channel_id": "U2", "message": f"Hi\n{NOTE}"})
        assert "-- sent by a bot for Alice" in str(reason_of(payload, environ))

    def test_overlay_variable(self, tmp_path: Path) -> None:
        """Without LLOPING_LLAMA_PEOPLE, the file is read from the overlay root."""
        (tmp_path / "me").mkdir()
        (tmp_path / "me" / "people.md").write_text(PEOPLE, encoding="utf-8")
        payload = event({"channel_id": "U0000000002", "message": MESSAGE})
        assert reason_of(payload, {"LLOPING_LLAMA_OVERLAY": str(tmp_path)}) is None


class TestDenied:
    """Calls that are blocked."""

    @pytest.mark.parametrize("target", ["C0123", "G0123", "c0123", "#general", "S1"])
    def test_channels_and_groups(self, environ: dict[str, str], target: str) -> None:
        """Channels, groups and anything not a person ID are refused."""
        payload = event({"channel_id": target, "message": NOTE})
        assert "channel or group" in str(reason_of(payload, environ))

    def test_listed_channel_with_auto(self, tmp_path: Path) -> None:
        """A channel stays blocked even if the people file marks it auto."""
        path = tmp_path / "people.md"
        path.write_text("---\nowner: A\n---\n## Team\n- slack: C1\n- send: auto\n", "utf-8")
        payload = event({"channel_id": "C1", "message": NOTE})
        assert "channel or group" in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    def test_broadcast_reply(self, environ: dict[str, str]) -> None:
        """A thread reply that is also posted to the channel is refused."""
        payload = event(
            {
                "channel_id": "D0000000002",
                "message": NOTE,
                "thread_ts": "1",
                "reply_broadcast": True,
            }
        )
        assert "broadcast" in str(reason_of(payload, environ))

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("U0000000003", "Carol Example is not marked `send: auto`"),
            ("U0000000004", "Dave Example is not marked `send: auto`"),
            ("U0000000005", "Erin Again is not marked `send: auto`"),
            ("U9", "not a person in the people file"),
            ("U0000000099", "not a person in the people file"),
        ],
    )
    def test_persons_without_auto(
        self, environ: dict[str, str], target: str, expected: str
    ) -> None:
        """Draft, missing, conflicting and unknown persons are refused; comments don't count."""
        payload = event({"channel_id": target, "message": NOTE})
        assert expected in str(reason_of(payload, environ))

    @pytest.mark.parametrize(
        "message", ["Hi Bob", f"{NOTE} Hi Bob", f"Hi Bob {NOTE}", NOTE, f"\n {NOTE}", None, 42]
    )
    def test_missing_note(self, environ: dict[str, str], message: object) -> None:
        """An auto-sent message must end with the note."""
        payload = event({"channel_id": "U0000000002", "message": message})
        assert f"on a line of its own after the text: {NOTE}" in str(reason_of(payload, environ))

    def test_no_owner(self, tmp_path: Path) -> None:
        """Without an owner, the note can't be built, so nothing is auto-sent."""
        path = tmp_path / "people.md"
        path.write_text("## Bob\n- slack: U2\n- send: auto\n", encoding="utf-8")
        payload = event({"channel_id": "U2", "message": NOTE})
        assert "names no owner" in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    @pytest.mark.parametrize("target", [None, "", "  ", 7])
    def test_no_channel(self, environ: dict[str, str], target: object) -> None:
        """A call without a usable channel_id is refused."""
        payload = event({"channel_id": target, "message": NOTE})
        assert "no channel_id" in str(reason_of(payload, environ))

    def test_no_arguments(self, environ: dict[str, str]) -> None:
        """A call whose arguments aren't an object is refused."""
        payload = event({})
        payload["tool_input"] = "U0000000002"
        assert "no arguments" in str(reason_of(payload, environ))

    def test_not_an_object(self, environ: dict[str, str]) -> None:
        """Hook input that isn't a JSON object is refused."""
        assert "not a JSON object" in str(reason_of([1], environ))

    def test_invalid_json(self, environ: dict[str, str]) -> None:
        """Hook input that isn't JSON is refused."""
        status, out, _ = run("{not json", environ)
        assert status == 0
        assert "not valid JSON" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]

    def test_no_configuration(self) -> None:
        """Without a configured people file, every send is refused."""
        payload = event({"channel_id": "U0000000002", "message": NOTE})
        assert "neither LLOPING_LLAMA_PEOPLE" in str(reason_of(payload, {}))

    def test_missing_file(self, tmp_path: Path) -> None:
        """A people file that doesn't exist blocks every send."""
        payload = event({"channel_id": "U0000000002", "message": NOTE})
        environ = {"LLOPING_LLAMA_PEOPLE": str(tmp_path / "missing.md")}
        assert "can't read the people file" in str(reason_of(payload, environ))

    def test_undecodable_file(self, tmp_path: Path) -> None:
        """A people file that isn't UTF-8 blocks every send."""
        path = tmp_path / "people.md"
        path.write_bytes(b"\xff\xfe\x00")
        payload = event({"channel_id": "U0000000002", "message": NOTE})
        assert "can't read" in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    def test_unterminated_front_matter(self, tmp_path: Path) -> None:
        """Persons after unterminated front matter don't count."""
        path = tmp_path / "people.md"
        path.write_text("---\nowner: A\n## Bob\n- slack: U2\n- send: auto\n", encoding="utf-8")
        payload = event({"channel_id": "U2", "message": NOTE})
        assert "not a person" in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    def test_unexpected_error(
        self, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected error blocks the call with exit status 2."""

        def boom(*_: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(guard, "evaluate", boom)
        status, out, err = run(json.dumps(event({})), environ)
        assert (status, out) == (2, "")
        assert "RuntimeError('boom')" in err

    @pytest.mark.parametrize("tool", [None, 3])
    def test_no_tool_name(self, environ: dict[str, str], tool: object) -> None:
        """Hook input without a tool name is refused."""
        payload = event({"channel_id": "U0000000002", "message": MESSAGE}, tool=tool)
        assert "names no tool" in str(reason_of(payload, environ))

    @pytest.mark.parametrize(
        ("people", "expected"),
        [
            # A person under a level-3 heading counts for nobody
            (
                "## Team\n### Bob\n- slack: U2\n- send: auto\n### Carol\n- slack: U3\n",
                "is not a person",
            ),
            # A setext heading ends the previous person
            ("## Bob\n- slack: U2\n- send: auto\n\nCarol\n-----\n- slack: U3\n", "is not a person"),
            # A level-1 heading ends the previous person
            ("## Bob\n- slack: U2\n- send: auto\n# Others\n- slack: U3\n", "is not a person"),
            # Fenced examples don't count
            ("## Bob\n- slack: U2\n- send: auto\n```\n- slack: U3\n```\n", "is not a person"),
            ("## Bob\n- slack: U2\n- send: auto\n~~~md\n- slack: U3\n~~~\n", "is not a person"),
            # Nested items are prose, not fields
            ("## Carol\n- slack: U3\n- notes: x\n  - send: auto\n", "not marked"),
            # A key given twice in one section is refused
            ("## Carol\n- slack: U3\n- send: draft\n- send: auto\n", "gives send more"),
            ("## Carol\n- slack: U0\n- slack: U3\n- send: auto\n", "gives slack more"),
        ],
    )
    def test_layouts(self, tmp_path: Path, people: str, expected: str) -> None:
        """Layouts that could merge one person's fields into another's are safe."""
        path = tmp_path / "people.md"
        path.write_text(f"---\nowner: Alice\n---\n{people}", encoding="utf-8")
        payload = event({"channel_id": "U3", "message": MESSAGE})
        assert expected in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    @pytest.mark.parametrize(
        ("front", "expected"),
        [
            ("owner: Alice\nauto-note:", "empty auto-note"),
            ('owner: Alice\nauto-note: ""', "empty auto-note"),
            ("owner: Alice\nowner: Mallory", "repeats owner"),
        ],
    )
    def test_bad_front_matter(self, tmp_path: Path, front: str, expected: str) -> None:
        """An empty note or a repeated setting blocks auto-sending."""
        path = tmp_path / "people.md"
        path.write_text(f"---\n{front}\n---\n## Bob\n- slack: U2\n- send: auto\n", "utf-8")
        payload = event({"channel_id": "U2", "message": MESSAGE})
        assert expected in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    @pytest.mark.parametrize(
        "people",
        [
            # A shorter fence inside a longer one doesn't close it
            "## Bob\n- slack: U3\n````markdown\n```\n- send: auto\n```\n````\n",
            "## Bob\n- slack: U3\n~~~~\n~~~\n- send: auto\n~~~\n~~~~\n",
            # A closer indented by four spaces or followed by text doesn't close it
            "## Bob\n- slack: U3\n```\n    ```\n- send: auto\n```\n",
            "## Bob\n- slack: U3\n```\n``` not a closer\n- send: auto\n```\n",
            # A comment that starts a line hides what it encloses
            "## Bob\n- slack: U3\n<!--\n- send: auto\n-->\n",
            "## Bob\n- slack: U3\n<!-- - send: auto -->\n",
        ],
    )
    def test_hidden_fields(self, tmp_path: Path, people: str) -> None:
        """Fields inside code blocks and comments never count."""
        path = tmp_path / "people.md"
        path.write_text(f"---\nowner: Alice\n---\n{people}", encoding="utf-8")
        payload = event({"channel_id": "U3", "message": MESSAGE})
        assert "is not marked" in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    @pytest.mark.parametrize(
        ("people", "expected"),
        [
            ("## Bob\n- slack: U3\n- send: auto\n- notes: hide `<!--`\n", "middle of a line"),
            ("## Bob\n- slack: U3\n- send: auto\n<!-- x --> y\n", "text after the end"),
            ("## Bob\n- slack: U3\n- send: auto\n<!--\nx\n--> y\n", "text after the end"),
            ("## Bob\n- slack: U3\n- send: auto\n- notes: end `-->`\n", "middle of a line"),
        ],
    )
    def test_comment_markers(self, tmp_path: Path, people: str, expected: str) -> None:
        """Comment markers a renderer could read differently block auto-sending."""
        path = tmp_path / "people.md"
        path.write_text(f"---\nowner: Alice\n---\n{people}", encoding="utf-8")
        payload = event({"channel_id": "U3", "message": MESSAGE})
        assert expected in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))

    def test_comment_in_front_matter(self, tmp_path: Path) -> None:
        """A comment marker in the front matter blocks auto-sending."""
        path = tmp_path / "people.md"
        path.write_text(
            "---\nowner: Alice <!-- x -->\n---\n## Bob\n- slack: U2\n- send: auto\n", "utf-8"
        )
        payload = event({"channel_id": "U2", "message": MESSAGE})
        assert "front matter" in str(reason_of(payload, {"LLOPING_LLAMA_PEOPLE": str(path)}))


class TestParsing:
    """The people file parser."""

    def test_fields(self) -> None:
        """Keys are lower-cased; quotes and backticks go; items before a heading don't count."""
        people = guard.parse_people(
            "---\nOwner: 'Alice'\nnot a pair\n---\n- slack: U0\n"
            '## Bob\n- Slack: `U2`\n* send: "auto"\ntext\n'
        )
        assert people.settings == {"owner": "Alice"}
        assert people.persons == [{guard.HEADING: "Bob", "slack": "U2", "send": "auto"}]

    def test_trailing_comments(self) -> None:
        """A trailing ``# comment`` is dropped; a ``#`` inside a value stays."""
        people = guard.parse_people(
            '---\nauto-note: "bot # 1"  # quoted\nowner: A # B\n---\n'
            "## Bob\n- slack: U2  # guess\n- notes: see #12\n##\n"
        )
        assert people.settings == {"auto-note": "bot # 1", "owner": "A"}
        assert people.persons == [
            {guard.HEADING: "Bob", "slack": "U2", "notes": "see #12"},
            {guard.HEADING: ""},
        ]

    def test_template(self) -> None:
        """The shipped template parses: an owner, and persons with slack and send."""
        people = guard.parse_people(TEMPLATE.read_text(encoding="utf-8"))
        assert people.settings["owner"]
        assert not people.unsafe
        assert people.persons
        assert all("slack" in person and "send" in person for person in people.persons)


def test_script_runs(tmp_path: Path) -> None:
    """The script works as Claude Code calls it: JSON on stdin, decision on stdout."""
    path = tmp_path / "people.md"
    path.write_text(PEOPLE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event({"channel_id": "C1", "message": "x"})),
        capture_output=True,
        text=True,
        env={"LLOPING_LLAMA_PEOPLE": str(path)},
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hooks_json() -> None:
    """hooks.json guards send and schedule, but not drafts."""
    config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = [
        e
        for e in config["hooks"]["PreToolUse"]
        if "slack_send_guard.py" in e["hooks"][0]["command"]
    ]
    matcher = re.compile(entry["matcher"])
    assert matcher.search("mcp__claude_ai_Slack__slack_send_message")
    assert matcher.search("mcp__slack__slack_schedule_message")
    assert not matcher.search("mcp__claude_ai_Slack__slack_send_message_draft")
    for tool in guard.GUARDED_TOOLS:
        assert matcher.search(f"mcp__any__{tool}")
    assert "slack_send_guard.py" in entry["hooks"][0]["command"]
