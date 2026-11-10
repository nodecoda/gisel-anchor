//===- rule_dump.h - dump one legalizer's rule chain and arity -------------===//
//
// Shared by the per-target dump runners.  Include it after the target runner's
// Config/Sigma definitions; it only needs `S.L` (LegalizerInfo) and `S.STI`
// (TargetSubtargetInfo).
//
// `getRules()` / `hasMutation()` come from the overlay header built from
// patches/llvm-rule-trace.patch.  Building without that overlay is a compile
// error, not a silent empty dump.
//===----------------------------------------------------------------------===//

#ifndef ORACLE_RULE_DUMP_H
#define ORACLE_RULE_DUMP_H

#include "llvm/CodeGen/GlobalISel/LegalizerInfo.h"
#include "llvm/CodeGen/TargetInstrInfo.h"
#include "llvm/CodeGen/TargetOpcodes.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>
#include <string>

namespace rule_dump {

using namespace llvm;
using namespace LegalizeActions;

inline std::string jsonString(StringRef S) {
  std::string Out = "\"";
  for (char C : S) {
    if (C == '"' || C == '\\') {
      Out += '\\';
      Out += C;
    } else if (C == '\n') {
      Out += "\\n";
    } else {
      Out += C;
    }
  }
  Out += '"';
  return Out;
}

inline const char *actionName(LegalizeAction A) {
  switch (A) {
  case Legal: return "Legal";
  case NarrowScalar: return "NarrowScalar";
  case WidenScalar: return "WidenScalar";
  case FewerElements: return "FewerElements";
  case MoreElements: return "MoreElements";
  case Bitcast: return "Bitcast";
  case Lower: return "Lower";
  case Libcall: return "Libcall";
  case Custom: return "Custom";
  case Unsupported: return "Unsupported";
  }
  return "?";
}

inline const char *opcodeName(unsigned Op) {
  switch (Op) {
#define HANDLE_TARGET_OPCODE(OPC) case TargetOpcode::OPC: return #OPC;
#define HANDLE_TARGET_OPCODE_MARKER(IDENT, OP)
#include "llvm/Support/TargetOpcodes.def"
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
  default:
    return nullptr;
  }
}

template <typename SigmaT>
int rules(const SigmaT &S, StringRef ConfigName, raw_ostream &Out) {
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;
  const unsigned Last = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_END;
  for (unsigned Op = First; Op <= Last; ++Op) {
    const char *Name = opcodeName(Op);
    if (!Name)
      continue;
    const LegalizeRuleSet &RS = S.L->getActionDefinitions(Op);
    const unsigned Rep = First + S.L->getActionDefinitionsIdx(Op);
    ArrayRef<LegalizeRule> Rules = RS.getRules();
    for (unsigned I = 0, E = Rules.size(); I != E; ++I) {
      const LegalizeRule &R = Rules[I];
      Out << "{\"config\":" << jsonString(ConfigName)
          << ",\"opcode\":" << jsonString(Name)
          << ",\"rule_index\":" << I
          << ",\"action\":" << jsonString(actionName(R.getAction()))
          << ",\"has_mutation\":" << (R.hasMutation() ? "true" : "false")
          << ",\"alias_of\":";
      if (Rep == Op)
        Out << "null";
      else
        Out << jsonString(opcodeName(Rep));
      Out << "}\n";
    }
  }
  return 0;
}

template <typename SigmaT>
int arity(const SigmaT &S, StringRef ConfigName, raw_ostream &Out) {
  const MCInstrInfo &MII = *S.STI->getInstrInfo();
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;
  const unsigned Last = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_END;
  for (unsigned Op = First; Op <= Last; ++Op) {
    const char *Name = opcodeName(Op);
    if (!Name)
      continue;
    const MCInstrDesc &MCID = MII.get(Op);
    unsigned NumTypes = 0, NumImms = 0;
    for (const MCOperandInfo &OI : MCID.operands()) {
      if (OI.isGenericType())
        NumTypes = std::max(NumTypes, OI.getGenericTypeIndex() + 1U);
      if (OI.isGenericImm())
        NumImms = std::max(NumImms, OI.getGenericImmIndex() + 1U);
    }
    Out << "{\"config\":" << jsonString(ConfigName)
        << ",\"opcode\":" << jsonString(Name)
        << ",\"type_slots\":" << NumTypes
        << ",\"imm_slots\":" << NumImms << "}\n";
  }
  return 0;
}

} // namespace rule_dump

#endif
