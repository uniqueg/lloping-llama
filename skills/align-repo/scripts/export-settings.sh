#!/usr/bin/env bash
# Export the GitHub settings of one repository that agents often can't read
# (general and security settings, Actions permissions, rulesets, classic
# branch protection, environments, names of secrets and variables, community
# profile), for the align-repo skill. Read-only: GET requests only. Secret
# values are never returned by the API; variables are exported by name only.
#
# Usage: export-settings.sh <owner/repo> <out dir>
#   <owner/repo>  e.g. example-org/example-repo
#   <out dir>     e.g. <overlay>/orgs/example-org/align/example-repo/settings
# Needs: gh (logged in as a repository admin), jq. Run by the repository
# owner, not by agents.
set -uo pipefail

R=${1:?usage: export-settings.sh <owner/repo> <out dir>}
OUT=${2:?usage: export-settings.sh <owner/repo> <out dir>}
[[ $R == */* ]] || { echo "expected <owner/repo>, got: $R" >&2; exit 2; }
mkdir -p "$OUT/rulesets" "$OUT/environments" || exit 2
ERRORS=0

# get <file> <endpoint> [jq filter]: write the response (or the error) to a file
get() {
  local file=$1 endpoint=$2 filter=${3:-.}
  if gh api "$endpoint" --jq "$filter" >"${OUT:?}/${file:?}" 2>"${OUT:?}/${file:?}.err"; then
    rm -f "${OUT:?}/${file:?}.err"
    echo "ok     $file"
  else
    echo "error  $file: $(head -c 200 "${OUT:?}/${file:?}.err")"
    ERRORS=$((ERRORS + 1))
  fi
}

get general.json "repos/$R" '{default_branch, description, homepage, topics,
  visibility, archived, has_issues, has_projects, has_wiki, has_discussions,
  allow_squash_merge, allow_merge_commit, allow_rebase_merge,
  squash_merge_commit_title, squash_merge_commit_message,
  delete_branch_on_merge, allow_update_branch, allow_auto_merge,
  web_commit_signoff_required, license: .license.spdx_id}'
get security.json "repos/$R" '.security_and_analysis'
get private-vulnerability-reporting.json "repos/$R/private-vulnerability-reporting"
get automated-security-fixes.json "repos/$R/automated-security-fixes"
get code-scanning-default-setup.json "repos/$R/code-scanning/default-setup"
# 204 = Dependabot alerts on, 404 = off
if gh api "repos/$R/vulnerability-alerts" --silent 2>/dev/null; then
  echo '{"dependabot_alerts": true}' >"$OUT/dependabot-alerts.json"
else
  echo '{"dependabot_alerts": false}' >"$OUT/dependabot-alerts.json"
fi
echo "ok     dependabot-alerts.json"
get actions-permissions.json "repos/$R/actions/permissions"
get actions-workflow-permissions.json "repos/$R/actions/permissions/workflow"
get community.json "repos/$R/community/profile" '{health_percentage, files: (.files | map_values(. != null))}'

# Rulesets in the import format (Settings > Rules > Rulesets > Import)
if ids=$(gh api "repos/$R/rulesets?includes_parents=false" --jq '.[].id' 2>"$OUT/rulesets.err"); then
  rm -f "${OUT:?}/rulesets.err"
  for id in $ids; do
    name=$(gh api "repos/$R/rulesets/$id" --jq '.name' | tr ' :/' '-__')
    get "rulesets/$name.json" "repos/$R/rulesets/$id"
  done
else
  echo "error  rulesets: $(head -c 200 "$OUT/rulesets.err")"
  ERRORS=$((ERRORS + 1))
fi

# Classic branch protection (should not exist when rulesets are used; 404 = none)
default=$(jq -r .default_branch "$OUT/general.json" 2>/dev/null || echo main)
for b in $(printf '%s\n' "$default" main dev | sort -u); do
  if gh api "repos/$R/branches/$b/protection" >"$OUT/classic-protection-$b.json" 2>/dev/null; then
    echo "found  classic-protection-$b.json (classic rule exists)"
  else
    rm -f "${OUT:?}/classic-protection-${b:?}.json"
  fi
done

# Environments: protection rules, branch/tag policies, secret and variable names
if envs=$(gh api "repos/$R/environments" --jq '.environments[].name' 2>"$OUT/environments.err"); then
  rm -f "${OUT:?}/environments.err"
  while IFS= read -r env; do
    [[ -n $env ]] || continue
    enc=$(jq -rn --arg e "$env" '$e | @uri')
    f="environments/$(echo "$env" | tr ' /' '__')"
    get "$f.json" "repos/$R/environments/$enc" '{name, protection_rules, deployment_branch_policy}'
    get "$f.policies.json" "repos/$R/environments/$enc/deployment-branch-policies" '[.branch_policies[] | {name, type}]'
    get "$f.secrets.json" "repos/$R/environments/$enc/secrets" '[.secrets[].name]'
    get "$f.variables.json" "repos/$R/environments/$enc/variables" '[.variables[].name]'
  done <<<"$envs"
else
  echo "error  environments: $(head -c 200 "$OUT/environments.err")"
  ERRORS=$((ERRORS + 1))
fi

# Secrets and variables: names only
get actions-secrets.json "repos/$R/actions/secrets" '[.secrets[].name]'
get actions-variables.json "repos/$R/actions/variables" '[.variables[].name]'
get dependabot-secrets.json "repos/$R/dependabot/secrets" '[.secrets[].name]'
get org-secrets-visible.json "repos/$R/actions/organization-secrets" '[.secrets[].name]'
get org-variables-visible.json "repos/$R/actions/organization-variables" '[.variables[].name]'

echo "done   $OUT ($ERRORS errors)"
[[ $ERRORS -eq 0 ]]
