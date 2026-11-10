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
#include <cstdint>
#include <map>
#include <string>
#include <utility>
#include <vector>

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


/// One parsed query, owning the storage that LegalityQuery points at.
///
/// `HasMem` distinguishes "no mem operand" from a mem desc whose fields happen
/// to be zero; `OrderingText`/`FailureText` keep the input spelling so the
/// answer path can echo it byte for byte.
struct ParsedQuery {
  unsigned Opcode = 0;
  SmallVector<LLT, 4> Types;
  SmallVector<LegalityQuery::MemDesc, 1> MMOs;
  SmallVector<int64_t, 2> Imms;
  bool HasMem = false;
  std::string OrderingText;
  std::string FailureText;

  LegalityQuery query() const {
    return LegalityQuery(Opcode, Types, MMOs, Imms);
  }
};

/// Evaluate every rule for `Q`, in first-match order.
///
/// `FirstMatch` is -1 when no rule matches.  `LaterMatches` are the rules that
/// also match but lose to an earlier one: those are "shadowed" for coverage
/// purposes and must not be counted as witnessed.
template <typename SigmaT>
void matchRules(const SigmaT &S, const LegalityQuery &Q, unsigned &RulesetOpcode,
                int &FirstMatch, SmallVectorImpl<unsigned> &LaterMatches) {
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;
  const LegalizeRuleSet &RS = S.L->getActionDefinitions(Q.Opcode);
  RulesetOpcode = First + S.L->getActionDefinitionsIdx(Q.Opcode);
  FirstMatch = -1;
  LaterMatches.clear();
  ArrayRef<LegalizeRule> Rules = RS.getRules();
  for (unsigned I = 0, E = Rules.size(); I != E; ++I) {
    if (!Rules[I].match(Q))
      continue;
    if (FirstMatch < 0)
      FirstMatch = static_cast<int>(I);
    else
      LaterMatches.push_back(I);
  }
}

/// Emit one hits row for a query.  `QueryLine` is the raw input JSON and is
/// embedded as the `query` field, so the caller keeps one source of truth.
template <typename SigmaT>
void hitsLine(const SigmaT &S, StringRef ConfigName, const LegalityQuery &Q,
              StringRef QueryLine, raw_ostream &Out) {
  unsigned RulesetOpcode = 0;
  int FirstMatch = -1;
  SmallVector<unsigned, 4> LaterMatches;
  matchRules(S, Q, RulesetOpcode, FirstMatch, LaterMatches);
  Out << "{\"config\":" << jsonString(ConfigName)
      << ",\"opcode\":" << jsonString(opcodeName(Q.Opcode))
      << ",\"ruleset\":" << jsonString(opcodeName(RulesetOpcode))
      << ",\"first_match\":" << FirstMatch << ",\"later_matches\":[";
  for (size_t I = 0; I != LaterMatches.size(); ++I) {
    if (I)
      Out << ',';
    Out << LaterMatches[I];
  }
  Out << "],\"query\":" << QueryLine.rtrim() << "}\n";
}

/// Coverage-guided witness search for one configuration.
///
/// `Lines` are candidate queries (universe order is preserved); `Parse` fills a
/// ParsedQuery for one line.  For each opcode we stop as soon as every rule has
/// a first match, or after `BudgetPerOpcode` candidates (0 = unlimited).
///
/// One row per rule:
///   status = witnessed | shadowed | unexercised
///   first_hits = times this rule was the first match
///   match_hits = times this rule matched at all (first or shadowed)
template <typename SigmaT, typename ParseT>
int witness(const SigmaT &S, StringRef ConfigName, ArrayRef<std::string> Lines,
            unsigned BudgetPerOpcode, ParseT Parse, raw_ostream &Out) {
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;

  // Group in file order; std::map keeps opcode order deterministic.
  std::map<unsigned, std::vector<std::pair<std::string, ParsedQuery>>> Groups;
  for (const std::string &Line : Lines) {
    ParsedQuery P;
    if (!Parse(Line, P)) {
      errs() << "bad universe line: " << Line << "\n";
      return 2;
    }
    Groups[P.Opcode].push_back({Line, std::move(P)});
  }

  for (auto &Group : Groups) {
    const unsigned Opcode = Group.first;
    const LegalizeRuleSet &RS = S.L->getActionDefinitions(Opcode);
    const unsigned RulesetOpcode =
        First + S.L->getActionDefinitionsIdx(Opcode);
    ArrayRef<LegalizeRule> Rules = RS.getRules();
    if (Rules.empty())
      continue;

    std::vector<unsigned> FirstHits(Rules.size(), 0);
    std::vector<unsigned> MatchHits(Rules.size(), 0);
    std::vector<std::string> WitnessLine(Rules.size());
    std::vector<int> ShadowedBy(Rules.size(), -1);
    std::vector<std::string> ShadowLine(Rules.size());
    unsigned Evaluated = 0;
    for (auto &Candidate : Group.second) {
      if (BudgetPerOpcode && Evaluated >= BudgetPerOpcode)
        break;
      ++Evaluated;
      unsigned GotRuleset = 0;
      int FirstMatch = -1;
      SmallVector<unsigned, 4> LaterMatches;
      LegalityQuery Q = Candidate.second.query();
      matchRules(S, Q, GotRuleset, FirstMatch, LaterMatches);
      (void)GotRuleset;
      if (FirstMatch >= 0) {
        ++FirstHits[FirstMatch];
        ++MatchHits[FirstMatch];
        if (WitnessLine[FirstMatch].empty())
          WitnessLine[FirstMatch] = Candidate.first;
      }
      for (unsigned I : LaterMatches) {
        ++MatchHits[I];
        if (ShadowedBy[I] < 0) {
          ShadowedBy[I] = FirstMatch;
          ShadowLine[I] = Candidate.first;
        }
      }

      bool AllWitnessed = std::all_of(
          FirstHits.begin(), FirstHits.end(), [](unsigned N) { return N > 0; });
      if (AllWitnessed)
        break;
    }

    for (unsigned I = 0, E = Rules.size(); I != E; ++I) {
      const char *Status = FirstHits[I] ? "witnessed"
                            : MatchHits[I] ? "shadowed"
                                           : "unexercised";
      Out << "{\"config\":" << jsonString(ConfigName)
          << ",\"opcode\":" << jsonString(opcodeName(Opcode))
          << ",\"ruleset\":" << jsonString(opcodeName(RulesetOpcode))
          << ",\"rule_index\":" << I
          << ",\"status\":" << jsonString(Status)
          << ",\"first_hits\":" << FirstHits[I]
          << ",\"match_hits\":" << MatchHits[I]
          << ",\"evaluated\":" << Evaluated
          << ",\"candidates\":" << Group.second.size()
          << ",\"shadowed_by\":" << ShadowedBy[I]
          << ",\"witness\":";
      if (WitnessLine[I].empty())
        Out << "null";
      else
        Out << WitnessLine[I];
      Out << ",\"shadow_query\":";
      if (ShadowLine[I].empty())
        Out << "null";
      else
        Out << ShadowLine[I];
      Out << "}\n";
    }
  }
  return 0;
}

} // namespace rule_dump

#endif
