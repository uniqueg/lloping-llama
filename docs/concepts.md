# Concepts

The building blocks of a Claude Code setup, and how lloping-llama uses them.

## Glossary

**CLAUDE.md**
:   Memory files that Claude Code loads into every session: `~/.claude/CLAUDE.md` for you, and
    `CLAUDE.md` in a repository for everyone who works on it. They can import other files with
    `@path`. Your overlay's `CLAUDE.md` is imported from your user memory.

**Skills**
:   Folders with a `SKILL.md` (instructions plus a description of when to use them) and optional
    reference files and scripts. Claude loads a skill when its description matches the task, or
    when you call it as `/<plugin>:<skill>`. Examples: `align-repo`, `message-drafts`.

**Agents**
:   Subagents: Markdown files with frontmatter (`name`, `description`, `tools`, `model`) that run
    in their own context and report back to the main session. Example: the three reviewers, which
    get read-only tools.

**Workflows**
:   Scripts for Claude Code's Workflow tool that fan work out to several agents in a fixed
    structure, e.g. one reviewer per area followed by an adversarial check of their findings.

**Hooks**
:   Commands that Claude Code runs on events, e.g. before a tool call (`PreToolUse`). They can
    block an action, so they enforce rules that instructions alone can't, e.g. "no merges in
    repositories where I am a contributor" or "no Slack messages to channels" (the Slack send
    guard, see [Messages](messages.md)).

**Profiles**
:   Per-repository rights in your overlay's `profiles.yaml`: who decides, who merges, whom to
    involve. Rights follow the repository, not the session. See [Profiles](profiles.md).

**Routines**
:   Scheduled cloud agents: Claude Code sessions that run on a schedule in the cloud, for jobs whose
    inputs and outputs live online (GitHub, chat, artifacts), e.g. watching repositories you depend
    on.

**systemd jobs**
:   Local scheduled jobs (systemd user timers on Linux; launchd or Task Scheduler elsewhere) for work
    on local files, e.g. cleaning up agent clones. Use them where a routine can't reach.

**Overlay layers**
:   Your private context, in four layers from general to specific: **kit** (this plugin) →
    **me** (you) → **org** (an organization) → **project**. The more specific layer wins. See the
    [Overlay guide](overlay.md).

## Principles

- **Generic kit, private overlay.** Nothing about you, your orgs or your projects goes into the kit;
  skills and agents read it from your overlay at run time.
- **Sessions are disposable.** A project's `state.md` and `decisions.md` are its single source of
  truth, so that any new session (or agent) can pick up the work without the old conversation.
- **Agents read, the main session decides.** Reviewers and checkers report; changes that reach other
  people (posts, merges, messages) go through the main session and the rules of your profile.
