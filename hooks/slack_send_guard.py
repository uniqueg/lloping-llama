#!/usr/bin/env python3
"""PreToolUse hook that lets Slack messages go out only where the people file allows it.

Claude Code runs this hook before every Slack send or schedule call (see ``hooks.json``) and
passes the tool call as JSON on stdin. The call goes ahead (exit 0 without output, so the normal
permission flow applies) only if all of these hold:

- the target is a single person: a Slack member ID (``U…``, ``W…``) or a direct-message
  channel (``D…``), never a channel or group (``C…``, ``G…``) and never a thread reply that is
  also broadcast to a channel;
- the person has a section in the people file whose ``slack`` or ``dm`` value is that ID, and
  every section with that ID says ``send: auto``;
- the message ends with the auto-send note from the people file's front matter.

Anything else is denied with a reason that tells the agent to create a draft instead. The hook
fails closed: a missing or unreadable people file, invalid input or an unexpected error blocks
the call. Only the Python standard library is used.

The people file is ``$LLOPING_LLAMA_PEOPLE`` or, if that is unset, ``me/people.md`` in
``$LLOPING_LLAMA_OVERLAY``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

#: Tool name suffixes this hook guards; other tools pass unchecked.
GUARDED_TOOLS = ("slack_send_message", "slack_schedule_message")

#: Note that ends every auto-sent message unless the people file sets ``auto-note``.
DEFAULT_NOTE = (
    "(Sent automatically by Claude on behalf of {owner}, without their review; "
    "please treat with care.)"
)

#: Slack ID prefixes of single persons: members (U, W) and direct messages (D).
PERSON_PREFIXES = ("U", "W", "D")

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_ITEM = re.compile(r"^\s*[-*]\s+([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$")
_PAIR = re.compile(r"^\s*([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$")
_HEADING = re.compile(r"^##\s+(.+?)\s*$")


@dataclass
class People:
    """The parsed people file.

    Attributes:
        settings: Front-matter keys (e.g. ``owner``, ``auto-note``), lower-cased.
        persons: One dict of lower-cased keys and values per level-2 section.
    """

    settings: dict[str, str] = field(default_factory=dict)
    persons: list[dict[str, str]] = field(default_factory=list)


class GuardError(Exception):
    """A reason to deny the tool call, shown to the agent."""


def _clean(value: str) -> str:
    """Strip whitespace, matching quotes and inline-code backticks from a value.

    Args:
        value: A raw value from the people file.

    Returns:
        The bare value.
    """
    value = value.strip()
    for quote in ('"', "'", "`"):
        if len(value) >= 2 and value[0] == quote and value[-1] == quote:
            return value[1:-1].strip()
    return value


def parse_people(text: str) -> People:
    """Parse a people file.

    The file may start with front matter between two ``---`` lines, holding ``key: value``
    settings. Each level-2 heading starts a person; ``- key: value`` list items below it are the
    person's fields. HTML comments are ignored, so fill-in instructions never count.

    Args:
        text: The content of the people file.

    Returns:
        The settings and persons found.
    """
    people = People()
    lines = _COMMENT.sub("", text).splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[index + 1 :]
                break
            match = _PAIR.match(line)
            if match:
                people.settings[match.group(1).lower()] = _clean(match.group(2))
        else:
            # Unterminated front matter: treat the whole file as settings only
            lines = []
    person: dict[str, str] | None = None
    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            person = {"heading": heading.group(1)}
            people.persons.append(person)
            continue
        item = _ITEM.match(line)
        if item and person is not None:
            person[item.group(1).lower()] = _clean(item.group(2))
    return people


def auto_note(people: People) -> str:
    """Return the note that must end every auto-sent message.

    Args:
        people: The parsed people file.

    Returns:
        The note with ``{owner}`` replaced by the owner's name.

    Raises:
        GuardError: If the front matter names no owner.
    """
    owner = people.settings.get("owner", "")
    if not owner:
        raise GuardError(
            "the people file names no owner (front matter `owner: <your name>`), so the "
            "auto-send note can't be checked"
        )
    return people.settings.get("auto-note", DEFAULT_NOTE).replace("{owner}", owner)


def check(tool_input: dict[str, Any], people: People) -> None:
    """Check one Slack send or schedule call against the people file.

    Args:
        tool_input: The tool call's arguments (``channel_id``, ``message``, …).
        people: The parsed people file.

    Raises:
        GuardError: If the call must not go ahead.
    """
    target = tool_input.get("channel_id")
    if not isinstance(target, str) or not target.strip():
        raise GuardError("the call has no channel_id")
    target = target.strip().upper()
    if tool_input.get("reply_broadcast"):
        raise GuardError("thread replies broadcast to a channel are never sent automatically")
    if not target.startswith(PERSON_PREFIXES):
        raise GuardError(
            f"{target} is a channel or group; messages to channels and groups are never sent "
            "automatically"
        )
    matches = [
        person
        for person in people.persons
        if target in (person.get("slack", "").upper(), person.get("dm", "").upper())
    ]
    if not matches:
        raise GuardError(f"{target} is not a person in the people file")
    for person in matches:
        if person.get("send", "draft").lower() != "auto":
            raise GuardError(f"{person['heading']} is not marked `send: auto`")
    note = auto_note(people)
    message = tool_input.get("message")
    if not isinstance(message, str) or not message.rstrip().endswith(note):
        raise GuardError(f"an auto-sent message must end with this note: {note}")


def people_path(environ: dict[str, str]) -> Path:
    """Locate the people file from the environment.

    Args:
        environ: The environment variables.

    Returns:
        The path of the people file.

    Raises:
        GuardError: If neither variable is set.
    """
    if environ.get("LLOPING_LLAMA_PEOPLE"):
        return Path(environ["LLOPING_LLAMA_PEOPLE"]).expanduser()
    if environ.get("LLOPING_LLAMA_OVERLAY"):
        return Path(environ["LLOPING_LLAMA_OVERLAY"]).expanduser() / "me" / "people.md"
    raise GuardError("neither LLOPING_LLAMA_PEOPLE nor LLOPING_LLAMA_OVERLAY is set")


def evaluate(event: object, environ: dict[str, str]) -> str | None:
    """Decide on one hook event.

    Args:
        event: The decoded hook input.
        environ: The environment variables.

    Returns:
        None if the call may go ahead, else the reason to deny it.
    """
    if not isinstance(event, dict):
        return "the hook input is not a JSON object"
    tool = event.get("tool_name")
    if not isinstance(tool, str) or not tool.endswith(GUARDED_TOOLS):
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return "the tool call has no arguments"
    try:
        path = people_path(environ)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise GuardError(f"can't read the people file {path}: {error}") from error
        check(tool_input, parse_people(text))
    except GuardError as error:
        return str(error)
    return None


def deny(reason: str) -> dict[str, Any]:
    """Build the hook output that denies the tool call.

    Args:
        reason: Why the call is denied.

    Returns:
        The PreToolUse hook output.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Slack send blocked: {reason}. Create an unsent draft with "
                "slack_send_message_draft instead (see the message-drafts skill)."
            ),
        }
    }


def main(stdin: TextIO, stdout: TextIO, stderr: TextIO, environ: dict[str, str]) -> int:
    """Run the hook.

    Args:
        stdin: The hook input stream.
        stdout: Where the decision goes.
        stderr: Where unexpected errors go.
        environ: The environment variables.

    Returns:
        The exit status: 0 (decision on stdout, or no objection), or 2 (blocked on an
        unexpected error).
    """
    try:
        try:
            event = json.load(stdin)
        except ValueError:
            reason: str | None = "the hook input is not valid JSON"
        else:
            reason = evaluate(event, environ)
        if reason is not None:
            json.dump(deny(reason), stdout)
        return 0
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"slack_send_guard: blocked on an unexpected error: {error!r}", file=stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess test
    sys.exit(main(sys.stdin, sys.stdout, sys.stderr, dict(os.environ)))
