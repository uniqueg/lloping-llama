# People

<!-- Fill in one section per person you work with. Agents read this file before drafting or sending
     a message to someone. Delete the example people and these comments when done.

Schema (one level-2 heading per person, then a list of `key: value` lines):

  key        required  values
  ---------  --------  ----------------------------------------------------------------
  name       yes       how to address the person in messages
  github     no        GitHub login, without @
  slack      no        Slack member ID (e.g. U0123456789) or handle
  email      no        only if you message them by email
  roles      no        what they do for you: repositories, responsibilities (see profiles.yaml)
  language   no        language for messages (default: yours)
  send       yes       draft | auto
  notes      no        anything an agent should know before writing to them

send: who may press "send"
  draft  (default) agents only prepare an unsent draft (e.g. a Slack draft); you review and send it.
  auto   agents may send messages to this person directly, without your review.

Rules for `send: auto`:
  - Only for single persons you list here. NEVER for channels, groups, mailing lists or group
    chats; those are always `draft`, whatever this file says.
  - Every automatically sent message ends with this note, translated if the message is in
    another language:
      (Sent automatically by Claude on behalf of <your name>, without <your name>'s review;
      please treat with care.)
  - A missing or unknown `send` value means `draft`.
-->

## Alice Example

- name: Alice
- github: alice
- slack: U0000000001
- roles: blessed maintainer of example-org/example-site
- send: draft
- notes: prefers one message with all questions over several small ones

## Bob Example

- name: Bob
- github: bob
- roles: co-owner of example-org/example-sdk
- send: auto
- notes: may receive status updates directly; decisions still go through you
