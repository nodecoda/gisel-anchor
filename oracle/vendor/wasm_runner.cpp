//===- wasm_runner.cpp - WebAssembly rule tools with the overlay header ---===//
//
// `generic` enables sign-ext; `mvp` is the empty baseline.  The pointer width
// is the only other axis the rules read.
//===----------------------------------------------------------------------===//

#include "GISel/WebAssemblyLegalizerInfo.h"
#include "WebAssemblySubtarget.h"
#include "WebAssemblyTargetMachine.h"
#include "llvm/Support/TargetSelect.h"

using namespace llvm;

using TargetSubtarget = WebAssemblySubtarget;
using TargetLegalizer = WebAssemblyLegalizerInfo;

#include "target_runner.h"

namespace {

static void initializeTarget() {
  LLVMInitializeWebAssemblyTargetInfo();
  LLVMInitializeWebAssemblyTarget();
  LLVMInitializeWebAssemblyTargetMC();
}

static ArrayRef<Config> configs() {
  static const Config Table[] = {
      {"wasm32", "wasm32-unknown-unknown", "mvp", "", std::nullopt},
      {"wasm32-signext", "wasm32-unknown-unknown", "generic", "", std::nullopt},
      {"wasm64", "wasm64-unknown-unknown", "mvp", "", std::nullopt},
      {"wasm64-signext", "wasm64-unknown-unknown", "generic", "", std::nullopt},
  };
  return Table;
}

static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &) {
  return std::make_unique<TargetLegalizer>(ST);
}

} // namespace
