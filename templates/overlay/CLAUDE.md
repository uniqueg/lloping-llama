# Overlay

<!-- Fill in: the entry point of your overlay. Import it from ~/.claude/CLAUDE.md with
     @~/path/to/agent-overlay/CLAUDE.md
     Keep this file short: imports and pointers, no project state. -->

Overlay root: `~/path/to/agent-overlay` (skills of the lloping-llama plugin call it `<overlay>`).

## Always loaded

@me/preferences.md
@me/machine.md

## Read when needed

- `profiles.yaml`: before any GitHub action in a repository (issues, PRs, merges, releases), look up
  the repository's profile. Unknown repository: ask once, then record it there.
- `me/people.md`: before drafting or sending a message to a person.
- `orgs/<org>/`: before working in a repository of that org.
- `projects/<project>/state.md` and `decisions.md`: at the start of every session on that project;
  they are the single source of the project's state, not your memory.

## Layering

kit → me → org → project: the more specific layer wins.
