#!/usr/bin/env bash
# Build the RISCV rule-dump tool with the overlay header.
#
#   build-dump-riscv.sh TREE BUILD OUT
#
# TREE/BUILD are an existing LLVM checkout/build (pin-checked).  The only
# difference from the A/B runner build is `-I$ORACLE/include` first: it supplies
# a LegalizerInfo.h with the two rule-trace accessors, so LLVM itself does not
# need a rebuild.
set -euo pipefail

TREE=${1:?usage: build-dump-riscv.sh TREE BUILD OUT}
BUILD=${2:?}
OUT=${3:?}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)     # the gisel-anchor checkout
ORACLE=$ROOT/src/gisel_anchor/oracle
AB=${AB_HOME:-$ROOT/../gisel-ab}
[ -d "$AB" ] || AB=$ROOT/../ab
CXX=${CXX:-g++}

CHECK_PIN=$AB/scripts/check-pin.sh
[ -f "$CHECK_PIN" ] || CHECK_PIN=$AB/check-pin.sh
"$CHECK_PIN" "$TREE"

FLAGS=(
  -DLLVM_BUILD_STATIC -D_DEBUG -D_GLIBCXX_ASSERTIONS
  -D_GLIBCXX_USE_CXX11_ABI=1 -D_GNU_SOURCE
  -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS
  -I"$ORACLE/include"
  -I"$BUILD/unittests/Target/RISCV"
  -I"$TREE/llvm/unittests/Target/RISCV"
  -I"$BUILD/include"
  -I"$TREE/llvm/include"
  -I"$TREE/llvm/lib/Target/RISCV"
  -I"$BUILD/lib/Target/RISCV"
  -fPIC -fno-exceptions -funwind-tables -fno-rtti -std=c++17
  -DNDEBUG -UNDEBUG
)

LIBS=(
  "$BUILD/lib/libLLVMRISCVCodeGen.so.24.0git"
  "$BUILD/lib/libLLVMRISCVDesc.so.24.0git"
  "$BUILD/lib/libLLVMRISCVInfo.so.24.0git"
  "$BUILD/lib/libLLVMGlobalISel.so.24.0git"
  "$BUILD/lib/libLLVMSelectionDAG.so.24.0git"
  "$BUILD/lib/libLLVMCodeGen.so.24.0git"
  "$BUILD/lib/libLLVMCodeGenTypes.so.24.0git"
  "$BUILD/lib/libLLVMTarget.so.24.0git"
  "$BUILD/lib/libLLVMAnalysis.so.24.0git"
  "$BUILD/lib/libLLVMAsmParser.so.24.0git"
  "$BUILD/lib/libLLVMCore.so.24.0git"
  "$BUILD/lib/libLLVMMC.so.24.0git"
  "$BUILD/lib/libLLVMTargetParser.so.24.0git"
  "$BUILD/lib/libLLVMSupport.so.24.0git"
  -lpthread -ldl -lm -lz
)

mkdir -p "$(dirname "$OUT")"
"$CXX" "${FLAGS[@]}" -c "$ORACLE/cpp/riscv_runner.cpp" -o "$OUT.o"
"$CXX" -fuse-ld=lld -Wl,-rpath,"$BUILD/lib" "$OUT.o" "${LIBS[@]}" -o "$OUT"
echo "$OUT: dump-rules"
