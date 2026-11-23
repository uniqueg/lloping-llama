# Reviewers

The plugin ships three reviewer agents. Each one reads a branch's diff from a different angle, so
that together they catch what a single reviewer misses. They only read: they never edit, commit,
push or post.

## The three reviewers

```text
Agent                 Model    Asks                                     Input
--------------------  -------  ---------------------------------------  ---------------------------
adversarial-reviewer  inherit  How does this change break or get        repository path, base ref
                               abused?
quality-reviewer      sonnet   Is this change easy to maintain, tested  repository path, base ref
                               and documented?
contract-reviewer     inherit  Does this change keep the promises the   repository path, base ref,
                               repository already makes?                PR number or draft PR body
```

All three have the tools `Read`, `Grep`, `Glob` and `Bash` (for `git diff` and for running checks
read-only), review only `git diff <base>...HEAD`, verify each finding before they report it and
report at most 10 findings with a severity (high, medium, low).

### adversarial-reviewer

Tries to break the change: wrong results, failure modes, concurrency, security, resource
exhaustion, secrets in build artefacts, behaviour that disagrees with specs or docs, breaking
changes, tests that can't fail. Each finding comes with a concrete failure scenario and how it was
verified (code path, test run, script). Ignores style.

### quality-reviewer

Reviews maintainability, similar to automated PR reviewers: duplication, dead code, missing or
weak tests, consistency with the surrounding code, missing docs, needless complexity, commit
hygiene. Runs on a smaller model, because it needs less depth than the other two.

### contract-reviewer

Checks the change against what the repository and its ecosystem already promise. Each finding names
both sides: the existing contract (with its source) and the changed line that breaks it.

```text
Area                   Checks
---------------------  ----------------------------------------------------------------------
API and spec style     additions to OpenAPI, JSON Schema, Protobuf, GraphQL or config schemas
                       follow the spec's existing style: field case, naming, enums, error
                       shapes, pagination, versioning, required vs optional
Breaking changes       removed or renamed fields, endpoints, config keys, flags, variables;
                       changed types, defaults, status codes; stricter validation; each one
                       called out as breaking (commit type `!`, upgrade notes)
Ecosystem conventions  e.g. Helm values keys in camelCase, Kubernetes labels, Conventional
                       Commits, GitHub Actions naming and gate jobs, semantic versioning,
                       pyproject.toml, Dockerfile and .dockerignore, the repo's own
                       CONTRIBUTING.md, AGENTS.md or CLAUDE.md
Docs coverage          every change of behaviour, configuration, API, CLI, UI, deployment or
                       upgrade path has matching docs in the same branch
PR-body claims         title and body match the diff: what changed, how it was verified (and
                       on which commit), Closes #<n>, declined review findings, scope
```

Give it the PR number (it reads the body with `gh pr view`, read-only) or, before the PR exists,
the path of the draft PR body. Without either it skips the PR-body checks.

## How to combine them

By default, run them as **2 parallel + 1**: start the adversarial reviewer, and at the same time
run the quality reviewer followed by the contract reviewer as one chain.

```text
adversarial-reviewer  ────────────────────────────────►
quality-reviewer ──► contract-reviewer ──►
```

Why this arrangement:

- The adversarial reviewer is the long pole: it traces code paths and runs tests. In measurements
  on a small diff (1 file) and a large one (48 files), it took 20 to 55 minutes, while quality
  followed by contract took 15 to 25 minutes and fits inside it.
- So 2 + 1 finishes about as fast as running all three at once, while using one agent slot less,
  and in less than half the wall time of running the three one after another.
- On the large diff, the contract reviewer was the only one to find spec and PR-body problems
  (2 medium findings), so it earns its place. It uses the most tokens of the three, about 1.2 to 2
  times the quality reviewer's.

These numbers come from one run per arrangement; the variance between runs is large. If you have
slots to spare, running all three at once is fine too.

Ask Claude, for example:

```text
Review this branch against origin/main: run the adversarial-reviewer, and in parallel the
quality-reviewer followed by the contract-reviewer (PR #12). Then fix confirmed high and medium
findings.
```

## After the review

- Fix confirmed high and medium findings, and low ones that affect behaviour or take minutes.
- Review again only if the first round found a high or medium finding, or the fixes changed logic;
  then rerun the adversarial reviewer, plus any other reviewer that had a medium or high finding.
  Stop after two rounds.
- List findings you decline, with reasons, in the PR body. The contract reviewer checks those
  reasons against the diff on the next round.
