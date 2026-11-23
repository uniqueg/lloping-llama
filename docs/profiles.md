# Profiles

A profile says how much an agent may do in a repository and whom it must involve. Profiles live in
your overlay's `profiles.yaml`; rights follow the repository, not the session.

## The six profiles

```text
Profile                    You are                     Agents may                            Decides
-------------------------  --------------------------  ------------------------------------  ---------------------------
owner-sole                 the only owner              everything your global rules allow    you
owner-blessed-maintainer   owner, with a maintainer    draft, then involve the maintainer    maintainer; you as fallback
owner-shared               one of several owners       draft proposals, wait for consensus   consensus of the owners
maintainer                 maintainer for an owner     act within listed responsibilities    you within them, else owner
developer                  a developer                 issues, PRs, feature branches         the repository's maintainers
contributor                an outside contributor      issues, PRs from a fork only          the repository's maintainers
```

- **owner-sole:** no restrictions beyond your global rules.
- **owner-blessed-maintainer:** involve the blessed maintainer in every decision. You may overrule
  when the maintainer is unavailable, under time pressure or without consensus; agents say so when
  they draft such a decision.
- **owner-shared:** decisions need consensus with the co-owners.
- **maintainer:** decide only within the responsibilities listed for the repository; everything
  else goes to the owner as a question.
- **developer:** issues and PRs per the repository's policy, feature branches allowed; extra rights
  are listed per repository.
- **contributor:** issues, and PRs from your fork only.

## profiles.yaml

```yaml
version: 1
unknown: ask            # unknown repository: ask once, then record it here

profiles:               # the six definitions above (description, decides, requires)
  owner-sole: {description: "...", decides: you}
  # ...

repositories:           # first match wins; `*` matches any characters except `/`
  - match: example-org/example-site
    profile: owner-blessed-maintainer
    maintainer: alice   # GitHub login; described in me/people.md
  - match: example-org/example-sdk
    profile: owner-shared
    co_owners: [bob]
  - match: other-org/example-service
    profile: maintainer
    owner: carol
    responsibilities: [CI and release workflows, dependency updates]
  - match: upstream-org/*
    profile: contributor
  - match: example-org/*
    profile: owner-sole
```

Fields per repository entry:

```text
Field             Required for                 Meaning
----------------  ---------------------------  ---------------------------------------------
match             all                          owner/repo, or a glob such as example-org/*
profile           all                          one of the six profiles
maintainer        owner-blessed-maintainer     the blessed maintainer's GitHub login
co_owners         owner-shared                 the other owners' GitHub logins
owner             maintainer, developer        the owner's GitHub login
responsibilities  maintainer                   what you may decide yourself
extra_rights      developer (optional)         rights beyond issues, PRs and feature branches
```

The template in `templates/overlay/profiles.yaml` has the full definitions and examples. A hook that
applies the profile to `gh` and `git` actions is planned; until then, agents look the profile up
before every GitHub action.
