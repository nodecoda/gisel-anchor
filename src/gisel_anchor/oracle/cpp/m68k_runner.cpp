//===- m68k_runner.cpp - M68k rule tools with the overlay header ----------===//
//
// The constructor reads no subtarget feature, so the configuration axis is
// one point.  `M68040` is the CPU the GlobalISel MIR tests run on.
//===----------------------------------------------------------------------===//

#include "GISel/M68kLegalizerInfo.h"
#include "M68kSubtarget.h"
#include "M68kTargetMachine.h"
#include "llvm/Support/TargetSelect.h"

using namespace llvm;

using TargetSubtarget = M68kSubtarget;
using TargetLegalizer = M68kLegalizerInfo;

#include "target_runner.h"

namespace {

static void initializeTarget() {
  LLVMInitializeM68kTargetInfo();
  LLVMInitializeM68kTarget();
  LLVMInitializeM68kTargetMC();
}

static ArrayRef<Config> configs() {
  static const Config Table[] = {
      {"default", "m68k", "M68040", "", std::nullopt},
  };
  return Table;
}

static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &) {
  return std::make_unique<TargetLegalizer>(ST);
}

} // namespace
