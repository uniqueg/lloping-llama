---
owner: Pat Example
# auto-note: "(Sent automatically by Claude on behalf of {owner}, without their review; please treat with care.)"
---

# People

<!-- Fill in one section per person you work with. Agents read this file before drafting or sending
     a message to someone (skill message-drafts); the Slack send guard hook reads it before every
     Slack send. Full schema: "Messages" in the docs. Replace the owner and the example people,
     and delete these comments when done.

Front matter (between the two --- lines at the top):

  key        required  value
  ---------  --------  ----------------------------------------------------------------
  owner      yes       your name, as it appears in the auto-send note
  auto-note  no        the note that ends every auto-sent message; {owner} is replaced
                       (default: "(Sent automatically by Claude on behalf of {owner},
                       without their review; please treat with care.)")

Persons (one level-2 heading per person, then a list of `key: value` lines):

  key        required  value
  ---------  --------  ----------------------------------------------------------------
  name       yes       how to address the person in messages
  github     no        GitHub login, without @
  slack      yes*      Slack member ID (U… or W…), from slack_search_users
  handle     no        Slack display name, for humans reading this file
  dm         no        ID of your direct-message channel with them (D…), if you know it
  email      no        only if you message them by email
  roles      no        what they do for you: repositories, responsibilities (see profiles.yaml)
  language   no        language for messages (default: yours)
  send       yes       draft | auto
  notes      no        anything an agent should know before writing to them

  * needed for Slack drafts; without it agents look the ID up and propose adding it.

send: who may press "send"
  draft  (default) agents only prepare an unsent draft (a Slack draft); you review and send it.
  auto   agents may send messages to this person directly, without your review.

Rules for `send: auto`:
  - Only for single persons listed here. NEVER for channels, groups, mailing lists or group
    chats: those are always drafts, whatever this file says, and the hook blocks them.
  - Every automatically sent message ends with the auto-send note.
  - A missing or unknown `send` value means `draft`; a person listed twice is `auto` only if
    every entry says so; a key given twice in one section blocks auto-sending.
  - Keep each person's fields directly under their level-2 heading: any other heading ends
    the person.
-->

## Alice Example

- name: Alice
- github: alice
- slack: U0000000001
- handle: alice
- roles: blessed maintainer of example-org/example-site
- send: draft
- notes: prefers one message with all questions over several small ones

## Bob Example

- name: Bob
- github: bob
- slack: U0000000002
- handle: bob
- dm: D0000000002
- roles: co-owner of example-org/example-sdk
- send: auto
- notes: may receive status updates directly; decisions still go through you
