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

#: Key under which a section records the keys it gives more than once.
DUPLICATE = "!duplicate"
#: Key under which a section keeps its heading (``!`` can't start a field key).
HEADING = "!heading"

# Fields are top-level list items only; nested items are prose
_ITEM = re.compile(r"^[-*]\s+([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$")
_PAIR = re.compile(r"^\s*([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CLOSING = re.compile(r"^ {0,3}(`+|~+)\s*$")
_QUOTED = re.compile(r"^([\"'`])(.*)\1(?:\s+#(?:\s.*)?)?$")
_TRAILING_COMMENT = re.compile(r"\s+#(?:\s.*)?$")


@dataclass
class People:
    """The parsed people file.

    Attributes:
        settings: Front-matter keys (e.g. ``owner``, ``auto-note``), lower-cased.
        persons: One dict of lower-cased keys and values per level-2 section; the heading is
            under ``HEADING``.
        unsafe: Why the file can't be trusted for auto-sending (e.g. an HTML comment marker in
            the middle of a line), or empty.
    """

    settings: dict[str, str] = field(default_factory=dict)
    persons: list[dict[str, str]] = field(default_factory=list)
    unsafe: str = ""


class GuardError(Exception):
    """A reason to deny the tool call, shown to the agent."""


def _clean(value: str) -> str:
    """Strip whitespace, matching quotes or backticks and a trailing ``# comment`` from a value.

    A quoted value keeps everything between its quotes, ``#`` included.

    Args:
        value: A raw value from the people file.

    Returns:
        The bare value.
    """
    value = value.strip()
    quoted = _QUOTED.match(value)
    if quoted:
        return quoted.group(2).strip()
    return _TRAILING_COMMENT.sub("", value).strip()


def _closes(line: str, fence: str) -> bool:
    """Tell whether a line closes a fenced code block (CommonMark rules).

    Args:
        line: The line.
        fence: The opening fence run, e.g. three or more backticks.

    Returns:
        True if the line is a closing fence for it.
    """
    match = _CLOSING.match(line)
    if match is None:
        return False
    run = match.group(1)
    return run[0] == fence[0] and len(run) >= len(fence)


def parse_people(text: str) -> People:
    """Parse a people file.

    The file may start with front matter between two ``---`` lines, holding ``key: value``
    settings. Each level-2 heading starts a person; top-level ``- key: value`` list items below it
    are the person's fields. Any other heading ends the person, so fields under it count for
    nobody. Fenced code blocks, nested list items, trailing ``# comments`` and HTML comments that
    start a line are ignored, so fill-in instructions and examples never count. An HTML comment
    marker anywhere else marks the whole file unsafe for auto-sending, because a renderer and this
    parser could disagree about what it hides.

    Args:
        text: The content of the people file.

    Returns:
        The settings and persons found.
    """
    people = People()
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[index + 1 :]
                break
            if "<!--" in line or "-->" in line:
                people.unsafe = "an HTML comment marker in the front matter"
            match = _PAIR.match(line)
            if match:
                _store(people.settings, match.group(1), match.group(2))
        else:
            # Unterminated front matter: treat the whole file as settings only
            lines = []
    person: dict[str, str] | None = None
    fence: str | None = None
    comment = False
    previous = ""
    for line in lines:
        if fence is not None:
            if _closes(line, fence):
                fence = None
        elif comment:
            if "-->" in line:
                comment = False
                if line.split("-->", 1)[1].strip():
                    people.unsafe = "text after the end of an HTML comment"
        elif line.lstrip().startswith("<!--"):
            rest = line.lstrip()[4:]
            if "-->" in rest:
                if rest.split("-->", 1)[1].strip():
                    people.unsafe = "text after the end of an HTML comment"
            else:
                comment = True
        elif "<!--" in line or "-->" in line:
            people.unsafe = "an HTML comment marker in the middle of a line"
        elif opening := _FENCE.match(line):
            fence = opening.group(1)
        elif heading := _HEADING.match(line):
            # Only level-2 headings start a person; any other heading ends one
            if len(heading.group(1)) == 2:
                person = {HEADING: heading.group(2) or ""}
                people.persons.append(person)
            else:
                person = None
        elif _SETEXT.match(line) and previous.strip():
            person = None
        elif (item := _ITEM.match(line)) and person is not None:
            _store(person, item.group(1), item.group(2))
        previous = line
    return people


def _store(fields: dict[str, str], key: str, raw: str) -> None:
    """Store one field; a key given twice is recorded as a duplicate.

    Args:
        fields: The settings or a person's fields.
        key: The key as written.
        raw: The raw value.
    """
    key = key.lower()
    if key in fields:
        fields[DUPLICATE] = ",".join(filter(None, (fields.get(DUPLICATE), key)))
    fields[key] = _clean(raw)


def auto_note(people: People) -> str:
    """Return the note that must end every auto-sent message.

    Args:
        people: The parsed people file.

    Returns:
        The note with ``{owner}`` replaced by the owner's name.

    Raises:
        GuardError: If the file is unsafe, or its front matter names no owner, gives a key
            twice or sets an empty note.
    """
    if people.unsafe:
        raise GuardError(f"the people file has {people.unsafe}")
    if DUPLICATE in people.settings:
        raise GuardError(f"the people file's front matter repeats {people.settings[DUPLICATE]}")
    owner = people.settings.get("owner", "")
    if not owner:
        raise GuardError(
            "the people file names no owner (front matter `owner: <your name>`), so the "
            "auto-send note can't be checked"
        )
    note = people.settings.get("auto-note", DEFAULT_NOTE).replace("{owner}", owner).strip()
    if not note:
        raise GuardError("the people file sets an empty auto-note")
    return note


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
        if DUPLICATE in person:
            raise GuardError(f"{person[HEADING]} gives {person[DUPLICATE]} more than once")
        if person.get("send", "draft").lower() != "auto":
            raise GuardError(f"{person[HEADING]} is not marked `send: auto`")
    note = auto_note(people)
    message = tool_input.get("message")
    body = message.rstrip() if isinstance(message, str) else ""
    text = body[: -len(note)].rstrip(" \t")
    if not body.endswith(note) or not text.endswith("\n") or not text.strip():
        raise GuardError(
            f"an auto-sent message must end with this note, on a line of its own after the "
            f"text: {note}"
        )


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
    if not isinstance(tool, str):
        return "the hook input names no tool"
    if not tool.endswith(GUARDED_TOOLS):
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
