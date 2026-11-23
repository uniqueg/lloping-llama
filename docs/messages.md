# Messages

Agents never print a message for you to copy and paste. The `message-drafts` skill puts every
message for someone else into Slack as an **unsent draft**, which you review and send from
Slack's "Drafts & sent". Only single persons you allow get messages directly, and the Slack send
guard hook enforces that.

## How it works

```text
agent writes a message for Bob
  -> skill message-drafts reads <overlay>/me/people.md
       Bob: send: draft (or channel, group, unknown) -> slack_send_message_draft
                                                        + a line in <overlay>/todos/drafts.md
       Bob: send: auto (single person)               -> slack_send_message, ending with the note
  -> hook slack_send_guard.py checks every slack_send_message / slack_schedule_message call
       and blocks it unless the people file allows it
  -> you see "N drafts waiting" in the report and on the to-do board
```

## The people file

`<overlay>/me/people.md` (template: `templates/overlay/me/people.md`) starts with front matter and
has one level-2 section per person, with `- key: value` lines. HTML comments are ignored.

```markdown
---
owner: Pat Example
auto-note: "(Sent automatically by Claude on behalf of {owner}, without their review; please treat with care.)"
---

## Bob Example

- name: Bob
- slack: U0000000002
- handle: bob
- dm: D0000000002
- send: auto
- notes: may receive status updates directly
```

Front matter:

```text
Key        Required  Value
---------  --------  -----------------------------------------------------------------
owner      yes       your name in the auto-send note; without it nothing is auto-sent
auto-note  no        the note that ends every auto-sent message; {owner} is replaced
```

The default note is "(Sent automatically by Claude on behalf of {owner}, without their review;
please treat with care.)".

Persons:

```text
Key        Required  Value
---------  --------  -----------------------------------------------------------------
name       yes       how to address the person
github     no        GitHub login, without @
slack      yes*      Slack member ID (U… or W…); agents look it up with slack_search_users
handle     no        Slack display name, for people reading the file
dm         no        ID of your direct-message channel with them (D…)
email      no        only if you message them by email
roles      no        what they do for you (see Profiles)
language   no        language for messages (default: yours)
send       yes       draft (default) or auto
notes      no        what an agent should know before writing to them
```

\* Needed for Slack drafts. Keys are case-insensitive; values may be quoted or in backticks, and
a trailing `# comment` is ignored (quote a value that contains ` # `). Only top-level list items
directly under a person's level-2 heading count: any other heading ends the person, and nested
items, fenced code blocks and HTML comments that start a line are ignored. A key given twice in
one section blocks auto-sending to that person, as does a repeated or empty setting in the front
matter. An HTML comment marker (`<!--`, `-->`) anywhere else, e.g. inside a value or after the end
of a comment, blocks auto-sending for the whole file.

### `send: auto`

- Only for **single persons**. Channels (`C…`), private channels and group DMs (`G…`) and any
  other target are always drafts, whatever the file says; the hook blocks them.
- A missing or unknown value means `draft`. A person listed twice is `auto` only if every entry
  says so.
- Every auto-sent message ends with the auto-send note, unchanged and on a line of its own after
  the text, also in messages in another language.
- Thread replies that are also broadcast to the channel are never auto-sent.
- Scheduled messages (`slack_schedule_message`) follow the same rules as sends.

## The drafts ledger

Slack's API can't list your drafts, so the skill records each one in `<overlay>/todos/drafts.md`:

```text
- [ ] 2026-01-31 Alice: question about the release date (https://app.slack.com/client/T…/D…)
```

Open lines are the "N drafts waiting" count on the to-do board. Agents tick a line once your
message shows up in the conversation and delete it if you discard the draft.

## The Slack send guard hook

`hooks/slack_send_guard.py` runs before every Slack tool call whose name ends in
`slack_send_message` or `slack_schedule_message`, from any MCP server; drafts are never blocked.
Slack servers whose send tools have other names (e.g. `slack_post_message`) are **not**
guarded; don't enable them next to this plugin.
It needs `python3` (3.10 or later, standard library only) and the location of your people file:

```json
{
  "env": {
    "LLOPING_LLAMA_OVERLAY": "/home/you/path/to/agent-overlay"
  }
}
```

Put this into `~/.claude/settings.json`, or set `LLOPING_LLAMA_PEOPLE` to the file itself. The
hook allows a call only if the target is a person ID or DM channel, every section with that ID
says `send: auto`, the call isn't a broadcast reply and the message ends with the note on a line
of its own. Otherwise it denies the call and tells the agent to draft instead.

The hook fails closed: without the variable, with a missing or unreadable people file, without an
owner, on invalid input or an unexpected error, it blocks the send. If `python3` is missing, the
command exits with status 2, which also blocks. A hook that runs into its 10-second timeout may
not block (Claude Code treats a timeout as a non-blocking error); the guard reads one small file,
so that shouldn't happen.

Allowed calls still go through Claude Code's normal permission rules: the hook never approves a
call on its own.
