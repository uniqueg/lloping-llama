# lloping-llama

lloping-llama is a [Claude Code](https://code.claude.com/docs/) plugin for agent-assisted software
and project work. It ships:

- **Agents** that review a branch before every push: an adversarial reviewer (correctness,
  security), a quality reviewer (maintainability, tests, docs) and a contract reviewer (API and
  spec style, ecosystem conventions, docs coverage, PR-body claims).
- **Skills** for recurring jobs: `align-repo` audits a repository against its organization's
  policy and writes one instruction set; `message-drafts` turns messages for people into unsent
  Slack drafts (see [Messages](messages.md)).
- **A hook** that blocks Slack messages to channels, groups and anyone you haven't allowed.
- **A status line** that shows the model, the directory, activity and your usage limits.
- **An overlay template**: the structure of a private repository for everything that is yours
  (preferences, machine, people, organizations, projects, to-dos), so the kit itself stays
  generic and shareable.

Start with [Getting started](getting-started.md), then read [Concepts](concepts.md) for the
vocabulary and the [Overlay guide](overlay.md) to make the kit yours.
