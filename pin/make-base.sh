#!/usr/bin/env bash
set -euo pipefail

# make-base.sh <llvm-repo> <dest> [--emit-json|--verify]
#
# Rebuild the official side of the A/B harness: the pin commit plus every
# unmerged patch listed in PIN.json `patches`, then the three local deviation
# patches listed in `official_base.deviations`.
#
# Why this script exists: our fork's patch commits also carry TableGen files
# that do not exist at the pin, so a plain `git cherry-pick` conflicts on the
# first patch.  Only the files that exist at the pin are applied; the rest are
# reported and skipped.  Each patch becomes its own commit, so the base is
# auditable with `git log` and diffable against a hand-built tree.
#
# Without a mode the script prints the actual hash of every file that can
# change a legalizer answer.  `--verify` compares that map with PIN.json
# `official_base.files` and exits non-zero on a mismatch; `--emit-json` prints
# the map as JSON.  Both modes are implemented once, in verify-base.py, so
# ab/check-pin.sh and this script cannot disagree.

HERE=$(cd "$(dirname "$0")" && pwd)
PIN=${PIN:-$HERE/PIN.json}
PIN_DIR=${PIN_DIR:-$HERE}
REPO=${1:?usage: make-base.sh <llvm-repo> <dest> [--emit-json|--verify]}
DEST=${2:?usage: make-base.sh <llvm-repo> <dest> [--emit-json|--verify]}
MODE=${3:-}
case "$MODE" in
  ""|--emit-json|--verify) ;;
  *) echo "make-base: unknown mode $MODE" >&2; exit 2 ;;
esac

COMMIT=$(python3 -c "import json;print(json.load(open('$PIN'))['commit'])")
[ -e "$DEST" ] && { echo "make-base: $DEST already exists" >&2; exit 1; }

git -C "$REPO" worktree add --detach "$DEST" "$COMMIT"
cd "$DEST"
# Stay detached: a fixed branch name collides with the long-lived pin tree on
# a machine that already has one.  `git log` in $DEST is still the audit trail.

python3 -c "
import json
for p in json.load(open('$PIN'))['patches']:
    print('%s\t%s\t%s' % (p['commit'], p['pr'], p['title']))
" | while IFS=$'\t' read -r commit pr title; do
  files=()
  while IFS= read -r f; do
    [ -n "$f" ] && [ -e "$f" ] && files+=("$f")
  done < <(git show --name-only --format='' "$commit")
  skipped=()
  while IFS= read -r f; do
    [ -n "$f" ] && [ ! -e "$f" ] && skipped+=("$f")
  done < <(git show --name-only --format='' "$commit")
  if [ ${#files[@]} -eq 0 ]; then
    echo "pin: #$pr touches nothing present at the pin, skipped" >&2
    continue
  fi
  echo "pin: #$pr $title" >&2
  [ ${#skipped[@]} -gt 0 ] && echo "pin:   skipped (absent at pin): ${skipped[*]}" >&2
  git show "$commit" -- "${files[@]}" | git apply -3 --index
  git -c user.name=pin-base -c user.email=pin-base@localhost commit -q \
      -m "pin: apply #$pr $title" -m "Applied from $commit, restricted to files that exist at $COMMIT."
done

python3 "$HERE/recipe.py" "$PIN" "$PIN_DIR" | while IFS= read -r patch; do
  echo "pin: deviation $(basename "$patch")" >&2
  git apply "$patch"
  git add -A
  git -c user.name=pin-base -c user.email=pin-base@localhost commit -q \
      -m "pin: apply deviation $(basename "$patch")"
done

python3 "$HERE/verify-base.py" "$DEST" "$PIN" ${MODE:+"$MODE"}
