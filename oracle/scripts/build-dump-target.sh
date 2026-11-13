#!/usr/bin/env bash
# Build one target's rule-dump tool with the overlay header.
#
#   build-dump-target.sh TARGET TREE BUILD OUT
#
# TARGET is the LLVM target directory name (M68k, WebAssembly, PowerPC, ...).
# TREE/BUILD are an existing LLVM checkout/build (pin-checked).  `-I$ROOT/include`
# comes first: it supplies a LegalizerInfo.h with the two rule-trace accessors,
# so LLVM itself does not need a rebuild.
set -euo pipefail

TARGET=${1:?usage: build-dump-target.sh TARGET TREE BUILD OUT}
TREE=${2:?}
BUILD=${3:?}
OUT=${4:?}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
AB=${AB_HOME:-$ROOT/../../gisel-ab}
[ -d "$AB" ] || AB=$ROOT/../../ab
CXX=${CXX:-g++}

case "$TARGET" in
  ARM)         SOURCE=arm ;;
  Mips)        SOURCE=mips ;;
  PowerPC)     SOURCE=ppc ;;
  M68k)        SOURCE=m68k ;;
  WebAssembly) SOURCE=wasm ;;
  SPIRV)       SOURCE=spirv ;;
  *) echo "build-dump-target: no runner for $TARGET" >&2; exit 2 ;;
esac

"$AB/check-pin.sh" "$TREE"

FLAGS=(
  -DLLVM_BUILD_STATIC -D_DEBUG -D_GLIBCXX_ASSERTIONS
  -D_GLIBCXX_USE_CXX11_ABI=1 -D_GNU_SOURCE
  -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS
  -I"$ROOT/include"
  -I"$BUILD/unittests/Target/$TARGET"
  -I"$TREE/llvm/unittests/Target/$TARGET"
  -I"$BUILD/include"
  -I"$TREE/llvm/include"
  -I"$TREE/llvm/lib/Target/$TARGET"
  -I"$BUILD/lib/Target/$TARGET"
  -fPIC -fno-exceptions -funwind-tables -fno-rtti -std=c++17
  -DNDEBUG -UNDEBUG
)

LIBS=(
  "$BUILD/lib/libLLVM${TARGET}CodeGen.so.24.0git"
  "$BUILD/lib/libLLVM${TARGET}Desc.so.24.0git"
  "$BUILD/lib/libLLVM${TARGET}Info.so.24.0git"
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
"$CXX" "${FLAGS[@]}" -c "$ROOT/vendor/${SOURCE}_runner.cpp" -o "$OUT.o"
"$CXX" -fuse-ld=lld -Wl,-rpath,"$BUILD/lib" "$OUT.o" "${LIBS[@]}" -o "$OUT"
echo "$OUT: $TARGET"
