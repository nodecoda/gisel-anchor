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
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <csignal>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
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

/// Compute one opcode's coverage rows.  Runs in a child process when called
/// from `witness`, so an upstream assertion costs one opcode, not the run.
template <typename SigmaT>
void witnessOpcode(
    const SigmaT &S, StringRef ConfigName, unsigned Opcode,
    ArrayRef<std::pair<std::string, ParsedQuery>> Group, unsigned Budget,
    raw_ostream &Out) {
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;
  const LegalizeRuleSet &RS = S.L->getActionDefinitions(Opcode);
  const unsigned RulesetOpcode = First + S.L->getActionDefinitionsIdx(Opcode);
  ArrayRef<LegalizeRule> Rules = RS.getRules();
  if (Rules.empty())
    return;

  std::vector<unsigned> FirstHits(Rules.size(), 0);
  std::vector<unsigned> MatchHits(Rules.size(), 0);
  std::vector<std::string> WitnessLine(Rules.size());
  std::vector<int> ShadowedBy(Rules.size(), -1);
  std::vector<std::string> ShadowLine(Rules.size());
  unsigned Evaluated = 0;
  for (auto &Candidate : Group) {
    if (Budget && Evaluated >= Budget)
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
        << ",\"candidates\":" << Group.size()
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

template <typename SigmaT>
void emitAsserted(const SigmaT &S, StringRef ConfigName, unsigned Opcode,
                  size_t Candidates, raw_ostream &Out) {
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;
  const LegalizeRuleSet &RS = S.L->getActionDefinitions(Opcode);
  const unsigned RulesetOpcode = First + S.L->getActionDefinitionsIdx(Opcode);
  ArrayRef<LegalizeRule> Rules = RS.getRules();
  for (unsigned I = 0, E = Rules.size(); I != E; ++I) {
    Out << "{\"config\":" << jsonString(ConfigName)
        << ",\"opcode\":" << jsonString(opcodeName(Opcode))
        << ",\"ruleset\":" << jsonString(opcodeName(RulesetOpcode))
        << ",\"rule_index\":" << I
        << ",\"status\":\"asserted\",\"first_hits\":0,\"match_hits\":0"
        << ",\"evaluated\":0,\"candidates\":" << Candidates
        << ",\"shadowed_by\":-1,\"witness\":null,\"shadow_query\":null}\n";
  }
}

/// Coverage-guided witness search for one configuration.
///
/// `Lines` are candidate queries (universe order is preserved); `Parse` fills a
/// ParsedQuery for one line.  For each opcode we stop as soon as every rule has
/// a first match, or after `BudgetPerOpcode` candidates (0 = unlimited).
///
/// One row per rule:
///   status = witnessed | shadowed | unexercised | asserted
///   first_hits = times this rule was the first match
///   match_hits = times this rule matched at all (first or shadowed)
///
/// Each opcode runs in a forked child.  An upstream assertion (scalable size,
/// bad LLT index) costs only the offending candidates: the child crash is
/// bisected down to individual queries, those are dropped, and the surviving
/// set is evaluated again.  Only when even that cannot produce rows does the
/// opcode become `asserted`.
/// Run one candidate range in a child.  `true` means the child exited cleanly.
template <typename SigmaT>
bool runWitnessChild(const SigmaT &S, StringRef ConfigName, unsigned Opcode,
                     ArrayRef<std::pair<std::string, ParsedQuery>> Group,
                     unsigned Budget, const char *Path) {
  pid_t Pid = fork();
  if (Pid == 0) {
    // Expected crashes are reported by the parent as dropped candidates; keep
    // the child's abort message out of the run log.  RLIMIT_CORE=0 also keeps
    // the kernel from piping each SIGABRT through apport, which dominates the
    // bisection cost otherwise.
    // `abort()` runs the kernel's core_pattern pipe, and on this host that is
    // apport: ~170 ms per crashed child.  A pipe handler ignores RLIMIT_CORE,
    // so turn the abort into an immediate _exit instead.
    struct rlimit NoCore = {0, 0};
    setrlimit(RLIMIT_CORE, &NoCore);
    std::signal(SIGABRT, [](int) { _exit(3); });
    if (FILE *Null = fopen("/dev/null", "w"))
      dup2(fileno(Null), STDERR_FILENO);
    std::error_code EC;
    raw_fd_ostream Child(Path, EC, sys::fs::OF_Text);
    if (!EC)
      witnessOpcode(S, ConfigName, Opcode, Group, Budget, Child);
    Child.flush();
    _exit(0);
  }
  int Status = 0;
  waitpid(Pid, &Status, 0);
  return WIFEXITED(Status) && WEXITSTATUS(Status) == 0;
}

/// Mark every single candidate that aborts the child.  Recursion keeps this at
/// O(k log n) forks when k candidates are bad.
template <typename SigmaT>
void findCrashers(const SigmaT &S, StringRef ConfigName, unsigned Opcode,
                  ArrayRef<std::pair<std::string, ParsedQuery>> Group,
                  unsigned Budget, const char *Path, size_t Begin, size_t End,
                  std::vector<char> &Bad) {
  if (Begin >= End)
    return;
  // Budget 0 == evaluate the whole slice.  A probe's slice is small, and
  // re-applying the opcode budget here would make bisection O(n^2).
  if (runWitnessChild(S, ConfigName, Opcode, Group.slice(Begin, End - Begin),
                      0, Path))
    return;
  if (End - Begin == 1) {
    Bad[Begin] = 1;
    return;
  }
  size_t Mid = Begin + (End - Begin) / 2;
  findCrashers(S, ConfigName, Opcode, Group, Budget, Path, Begin, Mid, Bad);
  findCrashers(S, ConfigName, Opcode, Group, Budget, Path, Mid, End, Bad);
}

template <typename SigmaT, typename ParseT>
int witness(const SigmaT &S, StringRef ConfigName, ArrayRef<std::string> Lines,
            unsigned BudgetPerOpcode, ParseT Parse, raw_ostream &Out) {
  // Group in file order; std::map keeps opcode order deterministic.
  std::map<unsigned, std::vector<std::pair<std::string, ParsedQuery>>> Groups;
  for (const std::string &Line : Lines) {
    ParsedQuery P;
    int Parsed = Parse(Line, P);
    if (Parsed == 1)
      continue;  // caller asked to skip this shape (e.g. known-unanswerable)
    if (Parsed != 0) {
      errs() << "bad universe line: " << Line << "\n";
      return 2;
    }
    Groups[P.Opcode].push_back({Line, std::move(P)});
  }

  for (auto &Group : Groups) {
    const unsigned Opcode = Group.first;
    ArrayRef<LegalizeRule> Rules = S.L->getActionDefinitions(Opcode).getRules();
    if (Rules.empty())
      continue;

    char Path[] = "/tmp/oracle-witness-XXXXXX";
    int FD = mkstemp(Path);
    if (FD < 0) {
      errs() << "cannot create witness temp file\n";
      return 2;
    }
    close(FD);

    bool Clean = runWitnessChild(S, ConfigName, Opcode, Group.second,
                                 BudgetPerOpcode, Path);
    if (!Clean) {
      std::vector<char> Bad(Group.second.size(), 0);
      findCrashers(S, ConfigName, Opcode, Group.second, BudgetPerOpcode, Path,
                   0, Group.second.size(), Bad);
      std::vector<std::pair<std::string, ParsedQuery>> Good;
      Good.reserve(Group.second.size());
      for (size_t I = 0; I != Group.second.size(); ++I)
        if (!Bad[I])
          Good.push_back(Group.second[I]);
      size_t Skipped = Group.second.size() - Good.size();
      errs() << "witness " << ConfigName << " " << opcodeName(Opcode)
             << ": dropped " << Skipped << " crashing candidate"
             << (Skipped == 1 ? "" : "s") << "\n";
      Clean = !Good.empty() &&
              runWitnessChild(S, ConfigName, Opcode, Good, BudgetPerOpcode,
                              Path);
    }
    if (Clean) {
      std::ifstream In(Path);
      std::string Content((std::istreambuf_iterator<char>(In)),
                          std::istreambuf_iterator<char>());
      Out << StringRef(Content);
    } else {
      emitAsserted(S, ConfigName, Opcode, Group.second.size(), Out);
    }
    unlink(Path);
  }
  return 0;
}

} // namespace rule_dump

#endif
