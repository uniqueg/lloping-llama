# Skills

One folder per skill, each with a `SKILL.md` (frontmatter `name`, `description`) and optional
reference files and scripts. Claude Code loads them when the plugin is enabled; they run as
`/lloping-llama:<name>`, or Claude picks them by their description.

```text
align-repo/   align a repository with its org's repository policy (policy lives in your overlay)
```

Skills here are generic: anything about you, your orgs or your projects belongs in your overlay
(see `templates/overlay/`), which skills read at run time.
