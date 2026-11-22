#!/usr/bin/env bash
# Smoke tests of the plugin's shell scripts: the status line, the align-repo
# scripts and the Linux machine-file collector. Needs bash, jq; touches no
# network and no GitHub (a stub `gh` is put first on the PATH).
#
# Usage: bash tests/smoke.sh   (from the repository root; CI job `smoke`)
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "${work:?}"' EXIT
failures=0

pass() { printf 'ok    %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
# fails <command...>: the command exits non-zero
fails() { ! "$@" >/dev/null 2>&1; }
# lacks <text> <output>: the output doesn't contain the text (any case)
lacks() { ! grep -qiF -- "$1" <<<"$2"; }

# Status line: renders model, directory and usage, and saves the status JSON
status='{"session_id":"s1","model":{"display_name":"Opus"},"workspace":{"current_dir":"/x/repo"},"rate_limits":{"five_hour":{"used_percentage":42.5,"resets_at":4102444800}}}'
out=$(CLAUDE_STATUS_DIR="$work/status" bash "$root/bin/claude-statusline" <<<"$status")
check "statusline shows model, directory and usage" grep -q 'Opus · repo · 5h .*42%' <<<"$out"
check "statusline saves the session status" test -s "$work/status/s1.json"
check "statusline saves latest.json" cmp -s "$work/status/s1.json" "$work/status/latest.json"
out=$(CLAUDE_STATUS_DIR="$work/status" bash "$root/bin/claude-statusline" <<<'not json')
check "statusline survives invalid input" grep -qx '.* status unavailable' <<<"$out"
ln -s "$work/status" "$work/status-link"
out=$(CLAUDE_STATUS_DIR="$work/status-link" bash "$root/bin/claude-statusline" <<<"$status")
check "statusline refuses a symlinked directory" grep -q 'unsafe' <<<"$out"

# export-settings.sh: usage errors without arguments or without an owner
scripts=$root/skills/align-repo/scripts
check "export-settings needs arguments" fails bash "$scripts/export-settings.sh"
check "export-settings needs owner/repo" fails bash "$scripts/export-settings.sh" repo-only "$work/export"

# export-settings.sh against a stub gh: writes files, counts errors
mkdir -p "$work/bin"
cat >"$work/bin/gh" <<'EOF'
#!/usr/bin/env bash
# Stub: rulesets and environments are denied (403), vulnerability alerts
# fail (500), classic protection is absent (404) or not on the plan (403);
# list calls print what their --jq filter would
case "$*" in
  *rulesets*|*environments*) echo "gh: Forbidden (HTTP 403)" >&2; exit 1 ;;
  *vulnerability-alerts*) echo "gh: Server Error (HTTP 500)" >&2; exit 1 ;;
  *branches/main/protection*) echo "gh: Branch not protected (HTTP 404)" >&2; exit 1 ;;
  *branches/dev/protection*) echo "gh: Upgrade to GitHub Pro or make this repository public (HTTP 403)" >&2; exit 1 ;;
  *--paginate*actions/secrets*) printf 'A\nB\n' ;;
  *--paginate*) ;;
  *default_branch*) echo '{"default_branch": "main"}' ;;
  *) echo '{}' ;;
esac
EOF
chmod +x "$work/bin/gh"
set +e
out=$(PATH="$work/bin:$PATH" bash "$scripts/export-settings.sh" example-org/example-repo "$work/export" 2>&1)
rc=$?
set -e
check "export-settings fails on denied reads" test "$rc" -ne 0
check "export-settings reports 3 errors" grep -q '(3 errors)' <<<"$out"
check "export-settings records no alert state on a 500" test ! -e "$work/export/dependabot-alerts.json"
check "export-settings treats 404 and plan 403 as no classic rule" \
  fails ls "$work/export"/classic-protection-*
check "export-settings lists all names" test "$(jq -c . "$work/export/actions-secrets.json")" = '["A","B"]'
check "export-settings lists empty names as []" test "$(jq -c . "$work/export/actions-variables.json")" = '[]'
check "export-settings writes general.json" test -s "$work/export/general.json"

# bulk-template.sh: dry run changes nothing, stops on an unknown kind
cp "$scripts/bulk-template.sh" "$work/bulk.sh"
printf '# id\trepo\tkind\targ\n1\texample-org/r\tunknown-kind\tx\n' >"$work/bulk.actions.tsv"
set +e
out=$(bash "$work/bulk.sh" 2>&1)
rc=$?
set -e
check "bulk-template stops on an unexpected state" test "$rc" -ne 0
check "bulk-template logs the stop" grep -q '^.* STOP  1 ' "$work/bulk.log"
check "bulk-template dry run records no state" test ! -s "$work/bulk.state"

# bulk-template.sh: a kind whose first step fails is not recorded as done,
# and a last line without a newline is still read
awk '/case \$kind in/ { print; n++; print (n == 1 ? "    ok|two) return 0 ;;\n    bad) out=$(false); return 10 ;;" : "    ok) : ;;\n    two) false; echo second ;;"); next } { print }' \
  "$scripts/bulk-template.sh" >"$work/bulk2.sh"
printf '1\texample-org/r\tok\tx\n2\texample-org/r\ttwo\tx' >"$work/bulk2.actions.tsv"
bash "$work/bulk2.sh" >/dev/null 2>&1 || true
check "bulk-template reads a last line without newline" grep -q 'DRY   2 ' "$work/bulk2.log"
set +e
bash "$work/bulk2.sh" --apply >/dev/null 2>&1
rc=$?
set -e
check "bulk-template stops when an earlier step fails" test "$rc" -ne 0
check "bulk-template records only the action that succeeded" test "$(cat "$work/bulk2.state")" = 1
printf '3\texample-org/r\tbad\tx\n' >"$work/bulk2.actions.tsv"
set +e
bash "$work/bulk2.sh" --apply >/dev/null 2>&1
rc=$?
set -e
check "bulk-template stops when a check fails" test "$rc" -ne 0
check "bulk-template doesn't skip a failed check" fails grep -qx 3 "$work/bulk2.state"

# Linux collector: no identity by default, identity on request
if [[ $(uname -s) == Linux ]]; then
  collector=$root/templates/overlay/me/collect-machine/linux.sh
  out=$(bash "$collector")
  check "collector prints the OS" grep -q '^- OS: ' <<<"$out"
  check "collector hides the home path" lacks "$HOME" "$out"
  check "collector hides the host name" lacks "$(hostname)" "$out"
  out=$(bash "$collector" --with-identity)
  check "collector shows identity on request" grep -qF -- "- User: $(id -un)" <<<"$out"
  check "collector rejects unknown options" fails bash "$collector" --bogus
fi

# profile-gate wrapper: silent without a profiles file and for ungated
# commands; deny for a gated one with PyYAML (ask naming PyYAML without it
# and without uv); ask when the interpreter fails
gate=$root/hooks/profile-gate
profiles=$root/templates/overlay/profiles.yaml
merge='{"tool_name":"Bash","tool_input":{"command":"gh pr merge 1 -R upstream-org/x"},"cwd":"/","session_id":"smoke"}'
out=$(env -u LLOPING_LLAMA_PROFILES bash "$gate" <<<"$merge")
check "profile-gate is silent without profiles" test -z "$out"
out=$(LLOPING_LLAMA_PROFILES=$profiles bash "$gate" <<<'{"tool_name":"Bash","tool_input":{"command":"ls"}}')
check "profile-gate is silent for ungated commands" test -z "$out"
out=$(LLOPING_LLAMA_PROFILES=$profiles LLOPING_LLAMA_STATE_DIR=$work/gate bash "$gate" <<<"$merge")
if python3 -c 'import yaml' >/dev/null 2>&1 || command -v uv >/dev/null 2>&1; then
  check "profile-gate denies a contributor merge" grep -q '"permissionDecision": "deny"' <<<"$out"
else
  check "profile-gate asks for PyYAML" grep -q 'PyYAML is missing' <<<"$out"
fi
mkdir -p "$work/nopython"
printf '#!/bin/sh\nexit 1\n' >"$work/nopython/python3"
chmod +x "$work/nopython/python3"
out=$(PATH="$work/nopython:/usr/bin:/bin" LLOPING_LLAMA_PROFILES=$profiles bash "$gate" <<<"$merge")
check "profile-gate asks when python3 fails" grep -q 'the hook failed to run' <<<"$out"

echo "$failures failure(s)"
[[ $failures -eq 0 ]]
