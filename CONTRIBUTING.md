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
sweeper/                  Python package behind bin/agent-sweep and bin/agent-ledger, with tests
templates/                files users copy (not loaded by the plugin)
tests/                    smoke tests, the shell lint script and the hook tests (not loaded)
```

Only plugin components go into `agents/` and `bin/`: every Markdown file in `agents/` is loaded as
an agent and every file in `bin/` lands on the PATH, so these folders have no README. Describe
components in `docs/reference.md` instead.

## Checks

CI (`.github/workflows/ci.yml`) runs these; run them locally before you push. The tool versions CI
uses are pinned in the `env` block of `ci.yml`; use the same ones locally:

```sh
# Markdown
npx --yes markdownlint-cli2@<MARKDOWNLINT_CLI2_VERSION>
# Shell scripts: every tracked file with a sh, bash or zsh shebang
bash tests/lint-shell.sh
# Smoke tests of the shell scripts (status line, align-repo scripts, Linux collector)
bash tests/smoke.sh
# Plugin and marketplace manifests
npx --yes @anthropic-ai/claude-code@<CLAUDE_CODE_VERSION> plugin validate --strict .
npx --yes @anthropic-ai/claude-code@<CLAUDE_CODE_VERSION> plugin validate --strict .claude-plugin/marketplace.json
# Sweeper (Python): lint, types, tests with 100 % coverage
cd sweeper && python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/ruff check . \
  && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest; cd ..
# Docs
python3 -m venv .venv && .venv/bin/pip install '.[docs]' && .venv/bin/mkdocs build --strict
# Python hooks: lint, format, types, tests with 100% coverage
.venv/bin/pip install '.[test]'
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```

PowerShell scripts are checked with PSScriptAnalyzer in CI, and the machine-file collectors run on
Linux, macOS and Windows runners (job `smoke`).

The collectors in `templates/overlay/me/collect-machine/` share their helpers and tool list by
copy, because each must run on its own (a user copies one file): change all three together.

## Reviews

Docs, text and scaffolding changes are reviewed by a maintainer. Anything with logic (hooks,
scripts, workflows) also gets the plugin's own reviewers (`adversarial-reviewer`,
`quality-reviewer`, `contract-reviewer`) before it is merged.
