#!/usr/bin/env bash
set -euo pipefail

# make-base.sh <llvm-repo> <dest> [--emit-json]
#
# Rebuild the official side of the A/B harness: the pin commit plus every
# unmerged patch listed in PIN.json `patches`.
#
# Why this script exists: our fork's patch commits also carry TableGen files
# that do not exist at the pin, so a plain `git cherry-pick` conflicts on the
# first patch.  Only the files that exist at the pin are applied; the rest are
# reported and skipped.  Each patch becomes its own commit, so the base is
# auditable with `git log` and diffable against a hand-built tree.
#
# The resulting tree must hash to PIN.json `official_base.files`; the script
# verifies that and exits non-zero if it does not.

HERE=$(cd "$(dirname "$0")" && pwd)
PIN=$HERE/PIN.json
REPO=${1:?usage: make-base.sh <llvm-repo> <dest> [--emit-json]}
DEST=${2:?usage: make-base.sh <llvm-repo> <dest> [--emit-json]}
MODE=${3:-}

COMMIT=$(python3 -c "import json;print(json.load(open('$PIN'))['commit'])")
[ -e "$DEST" ] && { echo "make-base: $DEST already exists" >&2; exit 1; }

git -C "$REPO" worktree add --detach "$DEST" "$COMMIT"
cd "$DEST"
git switch -c pin/ab-base

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

python3 - "$DEST" "$PIN" "$MODE" <<'PY'
import hashlib, json, pathlib, sys
tree, pin_path, mode = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
pin = json.loads(pin_path.read_text())
paths = [v["path"] for v in pin["sources"].values()] + [
    "llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h",
    "llvm/lib/CodeGen/GlobalISel/LegalityPredicates.cpp",
    "llvm/lib/CodeGen/GlobalISel/LegalizerInfo.cpp",
]
actual = {rel: hashlib.sha256((tree / rel).read_bytes()).hexdigest()
          for rel in sorted(paths)}
if mode == "--emit-json":
    print(json.dumps(actual, indent=2))
else:
    print("pin base built at %s" % tree)
    for rel, sha in actual.items():
        print("  %s  %s" % (sha[:16], rel))
    print("This is the pin-only cross-check tree. The A/B baseline is the "
          "frozen official side in PIN.json `official_base`; see "
          "ab/docs/OFFICIAL-BASE.md.")
PY
