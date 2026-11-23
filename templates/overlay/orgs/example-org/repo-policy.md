# Repository policy of example-org

<!-- Fill in: the desired state of every repository of this org. The align-repo skill checks each
     item in order and classifies gaps. One entry per item; keep the IDs stable (they appear in
     instruction sets). Delete the examples that don't apply and add your own.

Entry format:

  ### <ID> <topic>
  - target: what "aligned" means
  - check: how to check it (file, command, API endpoint, or "settings export")
  - fix: class (A agent issue+PR, C owner command, U UI step, P maintainer product work,
         X exception) and review lane (light, full, feather)
  - exceptions: repositories or kinds of repositories this item doesn't apply to
-->

Reference repositories: <!-- e.g. example-org/example-service for Python services -->

## Items

### B1 Branching model

- target: `main + dev` for released software; `main` only for repositories without releases
- check: `git ls-remote --heads origin`; other long-lived branches make it "undefined"
- fix: U (renames, see the skill's `branch-rename.md`)
- exceptions: docs and website repositories use `main` only

### C1 CI gate

- target: `.github/workflows/ci.yml` named `CI`, with a gate job `passed` / `CI passed` that needs
  every other job; the gate is the only required check of the workflow
- check: the workflow file; a PR's checks list
- fix: A, light lane

### D1 Docs

- target: MkDocs Material, `.readthedocs.yaml`, strict build in CI
- check: `mkdocs.yml`, `.readthedocs.yaml`, the CI docs job
- fix: A, light lane (scaffold); U (docs hosting import)

### L1 Labels

- target: the org's label set, no default or bot labels
- check: `gh label list --repo <r>`
- fix: C (the org's label sync script)

## Status

<!-- Optional: one row per repository, updated by align-repo after an alignment. -->

```text
Repo           B1  C1  D1  L1  Last checked
example-repo   ok  gap ok  ok  YYYY-MM-DD
```
