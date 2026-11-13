//===- ppc_runner.cpp - PowerPC rule tools with the overlay header --------===//
//
// Every rule is about types, so the configuration axis is one point.  The
// legalizer assumes the 64-bit register file (p0 is `pointer(0, 64)`).
//===----------------------------------------------------------------------===//

#include "GISel/PPCLegalizerInfo.h"
#include "PPCSubtarget.h"
#include "PPCTargetMachine.h"
#include "llvm/Support/TargetSelect.h"

using namespace llvm;

using TargetSubtarget = PPCSubtarget;
using TargetLegalizer = PPCLegalizerInfo;

#include "target_runner.h"

namespace {

static void initializeTarget() {
  LLVMInitializePowerPCTargetInfo();
  LLVMInitializePowerPCTarget();
  LLVMInitializePowerPCTargetMC();
}

static ArrayRef<Config> configs() {
  static const Config Table[] = {
      {"default", "powerpc64le-unknown-linux-gnu", "", "", std::nullopt},
  };
  return Table;
}

static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &) {
  return std::make_unique<TargetLegalizer>(ST);
}

} // namespace
