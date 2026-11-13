//===- spirv_runner.cpp - SPIRV rule tools with the overlay header --------===//
//
// SPIRV's configuration is not in the triple: the legalizer reads the
// execution environment and four extension queries, so `tweakSubtarget`
// finishes the subtarget after construction, exactly as `SPIRVAPI.cpp` does.
//===----------------------------------------------------------------------===//

#include "MCTargetDesc/SPIRVBaseInfo.h"
#include "SPIRVLegalizerInfo.h"
#include "SPIRVSubtarget.h"
#include "llvm/Support/TargetSelect.h"

using namespace llvm;

using TargetSubtarget = SPIRVSubtarget;
using TargetLegalizer = SPIRVLegalizerInfo;

#define GISEL_ORACLE_TWEAK_SUBTARGET
#include "target_runner.h"

namespace {

static void initializeTarget() {
  LLVMInitializeSPIRVTargetInfo();
  LLVMInitializeSPIRVTarget();
  LLVMInitializeSPIRVTargetMC();
}

static ArrayRef<Config> configs() {
  static const Config Table[] = {
      {"spirv64-kernel", "spirv64-unknown-unknown", "", "", std::nullopt},
      {"spirv32-kernel", "spirv32-unknown-unknown", "", "", std::nullopt},
      {"spirv64-shader", "spirv64-unknown-unknown", "", "", std::nullopt},
      {"spirv32-shader", "spirv32-unknown-unknown", "", "", std::nullopt},
      {"spirv64-kernel-ext", "spirv64-unknown-unknown", "", "", std::nullopt},
      {"spirv32-shader-ext", "spirv32-unknown-unknown", "", "", std::nullopt},
  };
  return Table;
}

struct EnvSpec {
  const char *ID;
  bool Shader;
  bool AlteraInts;
  bool LongVector;
  bool BitInstructions;
};

static void tweakSubtarget(const Config &C, TargetSubtarget &ST) {
  static const EnvSpec Table[] = {
      {"spirv64-kernel", false, false, false, false},
      {"spirv32-kernel", false, false, false, false},
      {"spirv64-shader", true, false, false, false},
      {"spirv32-shader", true, false, false, false},
      {"spirv64-kernel-ext", false, true, true, true},
      {"spirv32-shader-ext", true, false, true, true},
  };
  const EnvSpec *Spec = nullptr;
  for (const EnvSpec &E : Table)
    if (StringRef(E.ID) == C.ID)
      Spec = &E;
  if (!Spec) {
    errs() << "no environment spec for " << C.ID << "\n";
    return;
  }
  ExtensionSet Exts;
  if (Spec->AlteraInts)
    Exts.insert(SPIRV::Extension::SPV_ALTERA_arbitrary_precision_integers);
  if (Spec->LongVector)
    Exts.insert(SPIRV::Extension::SPV_EXT_long_vector);
  if (Spec->BitInstructions)
    Exts.insert(SPIRV::Extension::SPV_KHR_bit_instructions);
  ST.initAvailableExtensions(Exts);
  ST.setEnv(Spec->Shader ? SPIRVSubtarget::Shader : SPIRVSubtarget::Kernel);
}

static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &) {
  return std::make_unique<TargetLegalizer>(ST);
}

} // namespace
