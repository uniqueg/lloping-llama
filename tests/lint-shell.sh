#!/usr/bin/env bash
# Run shellcheck on every tracked file with a sh, bash or zsh shebang, so
# that new scripts are checked wherever they live (CI job `lint`).
#
# Usage: bash tests/lint-shell.sh   (from anywhere inside the repository)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
scripts=()
while IFS= read -r f; do
  [[ -f $f ]] && head -n 1 "$f" | grep -qE '^#!.*\b(ba|z)?sh\b' && scripts+=("$f")
done < <(git ls-files)
printf 'checking %s\n' "${scripts[@]}"
shellcheck "${scripts[@]}"
