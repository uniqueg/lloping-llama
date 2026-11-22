#!/usr/bin/env bash
# TEMPLATE for a resumable bulk change (labels on many items, many
# repositories, board fields), for the align-repo skill. Copy it into your
# overlay (e.g. <overlay>/projects/<project>/scripts/<name>-<YYYY-MM-DD>.sh),
# fill in check_one and apply_one, generate the ACTIONS file read-only, and
# run the dry run. The main session reviews the script and the dry-run log;
# the owner approves; the main session runs --apply. Agents never run --apply.
#
# Contract:
# - dry run by default; --apply makes the changes
# - one action per line in a TSV (ACTIONS), generated read-only beforehand and
#   reviewed with the script; lines starting with # are skipped
# - state file: finished actions are recorded and skipped on the next run
#   (delete it to start over)
# - log: every change, with time and result
# - stops at the first unexpected result (non-zero exit, unexpected state)
#
# Usage: <script> [--apply]
# Needs: gh (logged in, write access), jq.
set -euo pipefail

NAME=${NAME:-$(basename "$0" .sh)}
HERE=$(cd "$(dirname "$0")" && pwd)
ACTIONS=${ACTIONS:-$HERE/$NAME.actions.tsv} # columns: id<TAB>repo<TAB>kind<TAB>arg
STATE=${STATE:-$HERE/$NAME.state}
LOG=${LOG:-$HERE/$NAME.log}
APPLY=false
[[ ${1:-} == --apply ]] && APPLY=true
touch "$STATE"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }

# check_one <owner/repo> <kind> <arg>: exit 0 if the action is still needed,
# 1 if already done, 2 if the state is unexpected (stop)
check_one() {
  local repo=$1 kind=$2 arg=$3
  case $kind in
    # example: add-label) gh issue view "${arg%%:*}" --repo "$repo" --json labels \
    #   | jq -e --arg l "${arg#*:}" 'any(.labels[]; .name == $l)' >/dev/null && return 1 || return 0 ;;
    *) echo "unknown kind for $repo: $kind ($arg)" >&2; return 2 ;;
  esac
}

# apply_one <owner/repo> <kind> <arg>: make the change; non-zero = stop
apply_one() {
  local repo=$1 kind=$2 arg=$3
  case $kind in
    # example: add-label) gh issue edit "${arg%%:*}" --repo "$repo" --add-label "${arg#*:}" ;;
    *) echo "unknown kind for $repo: $kind ($arg)" >&2; return 2 ;;
  esac
}

while IFS=$'\t' read -r id repo kind arg; do
  [[ -z $id || $id == \#* ]] && continue
  if grep -qxF "$id" "$STATE"; then continue; fi
  set +e; check_one "$repo" "$kind" "$arg"; rc=$?; set -e
  case $rc in
    0) ;;
    1) log "SKIP  $id $repo $kind $arg (already done)"; $APPLY && echo "$id" >>"$STATE"; continue ;;
    *) log "STOP  $id $repo $kind $arg (unexpected state)"; exit 1 ;;
  esac
  if ! $APPLY; then log "DRY   $id $repo $kind $arg"; continue; fi
  if apply_one "$repo" "$kind" "$arg"; then
    echo "$id" >>"$STATE"
    log "DONE  $id $repo $kind $arg"
  else
    log "FAIL  $id $repo $kind $arg"
    exit 1
  fi
done <"$ACTIONS"
log "END   $($APPLY && echo apply || echo dry-run)"
