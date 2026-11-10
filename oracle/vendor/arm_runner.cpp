//===- arm_runner.cpp - ARM rule tools with the overlay header -------------===//
//
// Configurations are the subtarget axes the constructor itself reads
// (softfp/NEON/VFP4/V5T/hardware divide/AEABI).  Thumb1 is absent for the
// same reason it is absent from the A/B corpus: that subtarget registers no
// rules, which a table cannot spell.
//===----------------------------------------------------------------------===//

#include "ARMLegalizerInfo.h"
#include "ARMSubtarget.h"
#include "llvm/Support/TargetSelect.h"

using namespace llvm;

using TargetSubtarget = ARMSubtarget;
using TargetLegalizer = ARMLegalizerInfo;

#include "target_runner.h"

namespace {

static void initializeTarget() {
  LLVMInitializeARMTargetInfo();
  LLVMInitializeARMTarget();
  LLVMInitializeARMTargetMC();
}

static ArrayRef<Config> configs() {
  static const Config Table[] = {
      {"armv7a-neon", "armv7-unknown-linux-gnueabihf", "cortex-a7", "", std::nullopt},
      {"armv7a-nodiv", "armv7-unknown-linux-gnueabihf", "cortex-a7", "-hwdiv,-hwdiv-arm", std::nullopt},
      {"armv7a-a5", "armv7-unknown-linux-gnueabihf", "cortex-a5", "", std::nullopt},
      {"armv6-arm1176", "armv6-unknown-linux-gnueabihf", "arm1176jzf-s", "", std::nullopt},
      {"armv4t", "armv4t-unknown-linux-gnueabi", "arm7tdmi", "+soft-float", std::nullopt},
      {"thumbv7m", "thumbv7m-unknown-none-eabi", "cortex-m3", "", std::nullopt},
      {"armv7-soft", "armv7-unknown-linux-gnueabi", "generic", "+soft-float", std::nullopt},
  };
  return Table;
}

static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &) {
  return std::make_unique<TargetLegalizer>(ST);
}

} // namespace
