# Align <owner/repo> with the org policy (<YYYY-MM-DD>)

<!-- Output of the align-repo skill. Path:
<overlay>/orgs/<org>/align/<repo>/instructions-<YYYY-MM-DD>.md
Keep the sections in this order; drop empty ones. Fixed-width tables only. -->

Repo: `https://github.com/<owner/repo>` · profile: <profile from profiles.yaml> · stack: <…> ·
branching: <now> → <target> · maintainer: <none | GitHub handle> · reference: <reference repo | none>
Checked: clone `<sha>` of `<default branch>`, settings export of <date> (or "no export: items marked ?").

## Summary

```text
Item  Topic                    Now           Target        Class  Step
B1    Branching model          undefined     main + dev    U      1.1
...
```

## 0. Export the settings (only if no export is listed above)

```sh
# Read-only export of the settings agents can't read (GET requests only;
# secret values never leave GitHub)
bash <plugin>/skills/align-repo/scripts/export-settings.sh <owner/repo> <overlay>/orgs/<org>/align/<repo>/settings
# Expect: one "ok <file>" line per item; last line "done <dir> (0 errors)"
# "error ... 403/404": note the file name and report it; the rest is still usable
```

## 1. You first (settings, in this order)

Each step: what and why (one line), the click path or the command block, `# Expect:`, what to do if it
deviates.

## 2. Issues and PRs (agents)

Existing issues found: <#n title: reuse / extend / link>.

```text
#  Issue  Title (<= 50 chars)                 Lane   Owner  Blocked by  Items
1  new    ci: add the CI passed gate          light  ours   -           C1
2  new    ci: align the release workflow      full   ours   1           C2
```

Then one block per issue: title, body (problem, target with reference paths, acceptance criteria, docs to
update), labels, milestone if a release is planned.

Bulk changes: <script path, what it does, dry-run log path; "main runs --apply after your OK">.

## 3. After the PRs (you)

<Steps that depend on merged PRs, e.g. require the gate check after it reported once; docs hosting import
after the docs scaffold; DOI setup before the first release.>

## 4. Product work for the maintainer (listed only)

```text
Issue  What                                    Items  Existing issue
```

## 5. Questions

One per item: context, options, recommendation.

## 6. Verification checklist

- [ ] `git ls-remote --heads origin`: only the policy's long-lived branches plus feature branches
- [ ] export again: settings, environments, rulesets and secret names match the policy
- [ ] a PR shows exactly the required checks of the policy, and they pass
- [ ] labels match the org's label set; open issues carry the required labels
- [ ] the next Dependabot PR targets the default branch with the policy's label
- [ ] community profile complete; README, docs and tests layout match the policy
- [ ] docs hosting builds; coverage and DOI services show the repository
- [ ] no open issues of this alignment left, blockers lifted, board status current
