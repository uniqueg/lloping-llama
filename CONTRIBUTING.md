# Contributing

Thanks for helping improve lloping-llama. Issues and pull requests are welcome.

## Ground rules

- **Generic only.** The kit must work for anyone. No names of people, organizations' internals,
  project names, host names, e-mail addresses or machine details. Examples use placeholders such as
  `example-org`, `example-repo` and `alice`. Anything personal belongs in an overlay, never here.
- **No secrets**, not even in examples: no tokens, passwords, kubeconfigs or `.env` files.
- **One issue per pull request.** Open or pick an issue first; the PR body says `Closes #<n>`.
- **Conventional Commits** for commit messages and PR titles: `<type>[(scope)]: <description>`,
  subject at most 50 characters, imperative mood, no trailing period.
- **Docs in the same PR.** Every change of behaviour updates the docs under `docs/` and the
  README where they describe it.

## Layout

```text
agents/<name>.md          subagents: frontmatter name, description, tools, model
skills/<name>/SKILL.md    skills, with reference files and scripts in the same folder
hooks/hooks.json          event hooks; scripts next to it, via ${CLAUDE_PLUGIN_ROOT}
workflows/<name>.js       Workflow tool scripts
bin/<name>                executables, on the Bash tool's PATH while the plugin is enabled
templates/                files users copy (not loaded by the plugin)
```

Only plugin components go into `agents/` and `bin/`: every Markdown file in `agents/` is loaded as
an agent and every file in `bin/` lands on the PATH, so these folders have no README. Describe
components in `docs/reference.md` instead.

## Checks

CI (`.github/workflows/ci.yml`) runs these; run them locally before you push:

```sh
# Markdown
npx --yes markdownlint-cli2@0.24.0
# Shell scripts
shellcheck bin/claude-statusline skills/*/scripts/*.sh templates/overlay/me/collect-machine/*.sh
# Plugin and marketplace manifests
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/marketplace.json
# Docs
python3 -m venv .venv && .venv/bin/pip install '.[docs]' && .venv/bin/mkdocs build --strict
```

PowerShell scripts are checked with PSScriptAnalyzer in CI.

## Reviews

Docs, text and scaffolding changes are reviewed by a maintainer. Anything with logic (hooks,
scripts, workflows) also gets the plugin's own reviewers (`adversarial-reviewer`,
`quality-reviewer`, `contract-reviewer`) before it is merged.
