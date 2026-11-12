#!/usr/bin/env bash
# Generate the U side for one target: arity -> universe -> witnesses ->
# coverage -> queries -> FREEZE.  One command, one output directory.
#
#   gen-u.sh TARGET OUT [--no-gate] [--full]
#
# TARGET is x86 | aarch64 | riscv.  OUT holds every artifact; the script is
# deterministic, so running it twice into different directories must produce
# byte-identical queries.jsonl.
#
# --full additionally writes universe.full.jsonl: every universe row expanded
# across every configuration, unfiltered.  That is the input for the
# `--limit 0` U run in ab/docs/EVIDENCE-U.md (asserted rows stay in, because
# the diff classifies them as unanswerable/ref_asserted by count).
#
# Environment:
#   ORACLE_TREE   LLVM checkout          (default /home/ubuntu/llvm/trees/explore)
#   ORACLE_BUILD  matching build dir     (default /home/ubuntu/llvm/builds/explore)
#   ORACLE_BUDGET candidates per opcode  (default 20000)
#   ORACLE_LIMIT  universe cap/opcode    (default 1200)
#   ORACLE_SAMPLE sampled queries/opcode (default 8)
set -euo pipefail

TARGET=${1:?usage: gen-u.sh TARGET OUT [--no-gate]}
OUT=${2:?}
GATE=1
FULL=0
for arg in "${@:3}"; do
  case "$arg" in
    --no-gate) GATE=0 ;;
    --full) FULL=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ROOT=$(cd "$(dirname "$0")/.." && pwd)          # .../upstream-source/oracle
REPO=$(cd "$ROOT/.." && pwd)                    # .../upstream-source
TREE=${ORACLE_TREE:-/home/ubuntu/llvm/trees/explore}
BUILD=${ORACLE_BUILD:-/home/ubuntu/llvm/builds/explore}
BUDGET=${ORACLE_BUDGET:-20000}
LIMIT_DEFAULT=1200
SAMPLE=${ORACLE_SAMPLE:-8}

case "$TARGET" in
  x86)     CONFIGS=(C0 C1 C2 C3); PREFIX=x86 ;;
  aarch64) CONFIGS=(baseline small-cm bf16 lse rcpc3 outline-atomics cssc fullfp16 sve); PREFIX=a64; LIMIT_DEFAULT=2400 ;;
  riscv)   CONFIGS=(rv64gc rv32gc rv64gcv rv32gcv rv64zbb rv64zfh ristretto rv32zilsd); PREFIX=rv ;;
  arm)     CONFIGS=(armv7a-neon armv7a-nodiv armv7a-a5 armv6-arm1176 armv4t thumbv7m armv7-soft); PREFIX=arm ;;
  mips)    CONFIGS=(mips32 mips32r2 mips32r6 mips32r5-msa mips32r6-msa); PREFIX=mips ;;
  ppc)     CONFIGS=(default); PREFIX=ppc ;;
  m68k)    CONFIGS=(default); PREFIX=m68k ;;
  wasm)    CONFIGS=(wasm32 wasm32-signext wasm64 wasm64-signext); PREFIX=wasm ;;
  spirv)   CONFIGS=(spirv64-kernel spirv32-kernel spirv64-shader spirv32-shader spirv64-kernel-ext spirv32-shader-ext); PREFIX=spirv; LIMIT_DEFAULT=4000 ;;
  *) echo "unsupported target: $TARGET" >&2; exit 2 ;;
esac

LIMIT=${ORACLE_LIMIT:-$LIMIT_DEFAULT}

BIN=$ROOT/out/$PREFIX-dump
if [[ ! -x $BIN ]]; then
  if [[ -x $ROOT/scripts/build-dump-$TARGET.sh ]]; then
    "$ROOT/scripts/build-dump-$TARGET.sh" "$TREE" "$BUILD" "$BIN"
  else
    case "$TARGET" in
      arm)   LLVM_TARGET=ARM ;;
      mips)  LLVM_TARGET=Mips ;;
      ppc)   LLVM_TARGET=PowerPC ;;
      m68k)  LLVM_TARGET=M68k ;;
      wasm)  LLVM_TARGET=WebAssembly ;;
      spirv) LLVM_TARGET=SPIRV ;;
      *) echo "no LLVM target for $TARGET" >&2; exit 2 ;;
    esac
    "$ROOT/scripts/build-dump-target.sh" "$LLVM_TARGET" "$TREE" "$BUILD" "$BIN"
  fi
fi

mkdir -p "$OUT/cov"
cd "$REPO"

echo "[1/6] arity" >&2
"$BIN" --dump-arity "$OUT/arity.jsonl"

echo "[2/6] universe ($TARGET)" >&2
python3 -m oracle universe --arity "$OUT/arity.jsonl" --out "$OUT/universe.jsonl" \
  --target "$TARGET" --limit "$LIMIT"

if [[ $FULL -eq 1 ]]; then
  echo "[2b/6] full universe x ${#CONFIGS[@]} configs" >&2
  python3 -m oracle expand --universe "$OUT/universe.jsonl" \
    --configs "$(IFS=,; echo "${CONFIGS[*]}")" --out "$OUT/universe.full.jsonl"
fi

echo "[3/6] witness: main pass" >&2
MAIN=()
for cfg in "${CONFIGS[@]}"; do
  main_file=$OUT/cov/$cfg.main.jsonl
  "$BIN" --witness "$cfg" --universe "$OUT/universe.jsonl" \
    --out "$main_file" --budget "$BUDGET"
  MAIN+=(--cov "$main_file")
done

echo "[4/6] witness: salvage pass for asserted opcodes" >&2
SALVAGE_OPCODES=$(python3 - "$OUT" <<'PY'
import glob, json, sys
opcodes = set()
for path in glob.glob(sys.argv[1] + "/cov/*.main.jsonl"):
    for line in open(path, encoding="utf-8"):
        row = json.loads(line)
        if row["status"] == "asserted":
            opcodes.add(row["opcode"])
print(" ".join(sorted(opcodes)))
PY
)
SALVAGE=()
if [[ -n $SALVAGE_OPCODES ]]; then
  read -r -a OPCODES <<<"$SALVAGE_OPCODES"
  SAFE_ARGS=()
  for opcode in "${OPCODES[@]}"; do SAFE_ARGS+=(--opcodes "$opcode"); done
  python3 -m oracle universe --arity "$OUT/arity.jsonl" \
    --out "$OUT/universe.safe.jsonl" --target "$TARGET" --profile safe \
    "${SAFE_ARGS[@]}"
  for cfg in "${CONFIGS[@]}"; do
    safe_file=$OUT/cov/$cfg.safe.jsonl
    "$BIN" --witness "$cfg" --universe "$OUT/universe.safe.jsonl" \
      --out "$safe_file" --budget "$BUDGET"
    SALVAGE+=(--cov "$safe_file")
  done
else
  echo "    (no asserted opcodes)" >&2
fi

echo "[5/6] coverage" >&2
KNOWN=$ROOT/known-gaps/$TARGET.json
"$BIN" --dump-rules "$OUT/rules.jsonl"
if [[ $GATE -eq 1 ]]; then
  if [[ -f $KNOWN ]]; then
    python3 -m oracle coverage --target "$TARGET" --out "$OUT/coverage.json" \
      "${MAIN[@]}" "${SALVAGE[@]}" --rules "$OUT/rules.jsonl" \
      --gate --known-gaps "$KNOWN"
  else
    python3 -m oracle coverage --target "$TARGET" --out "$OUT/coverage.json" \
      "${MAIN[@]}" "${SALVAGE[@]}" --rules "$OUT/rules.jsonl"
    echo "gate: no $KNOWN; coverage written but not approved" >&2
    exit 1
  fi
else
  python3 -m oracle coverage --target "$TARGET" --out "$OUT/coverage.json" \
    "${MAIN[@]}" "${SALVAGE[@]}" --rules "$OUT/rules.jsonl"
fi

echo "[6/6] queries + answerable filter + FREEZE" >&2
python3 -m oracle emit --out "$OUT/queries.raw.jsonl" \
  --universe "$OUT/universe.jsonl" --sample "$SAMPLE" \
  "${MAIN[@]}" "${SALVAGE[@]}"

# Split by config so each query is filtered with the configuration it names.
# A query the pinned build cannot answer is a harness artifact, not a rule gap;
# leaving it in would show up as a fake A/B disagreement.
rm -rf "$OUT/split" "$OUT/filtered"
mkdir -p "$OUT/split" "$OUT/filtered"
python3 - "$OUT" "${CONFIGS[@]}" <<'SPLIT'
import json, os, sys
out, configs = sys.argv[1], sys.argv[2:]
handles = {c: open(os.path.join(out, "split", c + ".jsonl"), "w",
                   encoding="utf-8") for c in configs}
with open(os.path.join(out, "queries.raw.jsonl"), encoding="utf-8") as src:
    for line in src:
        if line.strip():
            handles[json.loads(line)["config"]].write(line)
for handle in handles.values():
    handle.close()
SPLIT
: > "$OUT/queries.jsonl"
: > "$OUT/queries.dropped.jsonl"
for cfg in "${CONFIGS[@]}"; do
  "$BIN" --witness "$cfg" --filter-in "$OUT/split/$cfg.jsonl" \
    --filter-out "$OUT/filtered/$cfg.jsonl" \
    --filter-drop "$OUT/filtered/$cfg.dropped.jsonl" >/dev/null
  cat "$OUT/filtered/$cfg.jsonl" >> "$OUT/queries.jsonl"
  cat "$OUT/filtered/$cfg.dropped.jsonl" >> "$OUT/queries.dropped.jsonl"
done

python3 - "$OUT" "$TARGET" "$TREE" "$REPO" "$BUDGET" "$LIMIT" "$SAMPLE" <<'PY'
import hashlib, json, os, subprocess, sys
out, target, tree, repo, budget, limit, sample = sys.argv[1:8]
pin = json.load(open(os.path.join(repo, "pin", "PIN.json"), encoding="utf-8"))
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def commit(path):
    return subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"],
                                   text=True).strip()
artifacts = {name: sha(os.path.join(out, name))
             for name in ("arity.jsonl", "universe.jsonl",
                          "coverage.json", "queries.jsonl",
                          "queries.raw.jsonl", "queries.dropped.jsonl")
             if os.path.exists(os.path.join(out, name))}
counts = {}
for name in ("queries.raw.jsonl", "queries.jsonl", "queries.dropped.jsonl"):
    path = os.path.join(out, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            counts[name] = sum(1 for line in handle if line.strip())
freeze = {"schema": 1, "target": target,
          "pin": pin["commit"],
          "pin_patches": [p["commit"] for p in pin.get("patches", [])],
          "tree_head": commit(tree),
          "tool_commit": commit(repo), "budget": int(budget),
          "limit": int(limit), "sample": int(sample), "counts": counts,
          "artifacts": artifacts}
with open(os.path.join(out, "FREEZE.json"), "w", encoding="utf-8") as handle:
    json.dump(freeze, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(json.dumps(freeze, sort_keys=True))
PY
