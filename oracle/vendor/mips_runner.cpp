//===- mips_runner.cpp - Mips rule tools with the overlay header ----------===//
//
// The constructor reads `hasMSA()`, `hasMips32r2()` and
// `systemSupportsUnalignedAccess()`; the configurations below are those axes
// on the 32-bit flavours the legalizer assumes.
//===----------------------------------------------------------------------===//

#include "MipsLegalizerInfo.h"
#include "MipsSubtarget.h"
#include "llvm/Support/TargetSelect.h"

using namespace llvm;

using TargetSubtarget = MipsSubtarget;
using TargetLegalizer = MipsLegalizerInfo;

#include "target_runner.h"

namespace {

static void initializeTarget() {
  LLVMInitializeMipsTargetInfo();
  LLVMInitializeMipsTarget();
  LLVMInitializeMipsTargetMC();
}

static ArrayRef<Config> configs() {
  static const Config Table[] = {
      {"mips32", "mips-unknown-linux-gnu", "mips32", "", std::nullopt},
      {"mips32r2", "mips-unknown-linux-gnu", "mips32r2", "", std::nullopt},
      {"mips32r6", "mips-unknown-linux-gnu", "mips32r6", "", std::nullopt},
      {"mips32r5-msa", "mips-unknown-linux-gnu", "mips32r5", "+msa,+fp64", std::nullopt},
      {"mips32r6-msa", "mips-unknown-linux-gnu", "mips32r6", "+msa,+fp64", std::nullopt},
  };
  return Table;
}

static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &) {
  return std::make_unique<TargetLegalizer>(ST);
}

} // namespace
