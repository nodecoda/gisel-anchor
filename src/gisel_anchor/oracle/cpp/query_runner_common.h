//===- query_runner_common.h - one JSONL query -> getAction(), any target -===//
//
// Included exactly once by a per-target runner.  The including file must
// define, before the include:
//
//   using TargetSubtarget = <Target>Subtarget;
//   using TargetLegalizer = <Target>LegalizerInfo;
//
// and, after it:
//
//   static ArrayRef<Config> configs();
//   static void initializeTarget();
//   static std::unique_ptr<TargetLegalizer>
//       makeLegalizer(const TargetSubtarget &, TargetMachine &);
//
// Everything else -- config expansion, LLT spelling, mem/imms decoding, the
// answer JSON -- is identical for every target.  A new target is a ~30 line
// file plus a build script, not another 360 line copy.
//
// Both sides of an A/B run are the same runner: `ours` links a replacement
// constructor object first, so the class the runner names resolves to the
// generated body for that binary and to the library's body for `official`.
//===----------------------------------------------------------------------===//

#ifndef GISEL_AB_QUERY_RUNNER_COMMON_H
#define GISEL_AB_QUERY_RUNNER_COMMON_H

#include "llvm/ADT/ArrayRef.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/ADT/StringSwitch.h"
#include "llvm/CodeGen/GlobalISel/LegalizerInfo.h"
#include "llvm/CodeGen/TargetOpcodes.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Support/AtomicOrdering.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Target/TargetMachine.h"
#include "llvm/Target/TargetOptions.h"
#include "llvm/TargetParser/Triple.h"

#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <vector>

using namespace llvm;
using namespace TargetOpcode;
using namespace LegalizeActions;

namespace {

struct Config {
  const char *ID;
  const char *Triple;
  const char *CPU;
  const char *Features;
  std::optional<CodeModel::Model> CM;
};

struct Sigma {
  std::unique_ptr<TargetMachine> TM;
  const TargetSubtarget *STI = nullptr;
  std::unique_ptr<TargetLegalizer> L;
};

// Provided by the per-target file after this header.
static ArrayRef<Config> configs();
static void initializeTarget();
static std::unique_ptr<TargetLegalizer>
makeLegalizer(const TargetSubtarget &ST, TargetMachine &TM);

// Optional per-target adjustment applied to the freshly created subtarget,
// before the legalizer is built.  Most targets keep the no-op below.  SPIRV
// needs it because its execution environment and extension set are not
// encoded in the triple, so two configurations can share one triple.
// A runner that provides a real one defines GISEL_AB_TWEAK_SUBTARGET before
// including this header and defines `tweakSubtarget` after it.
#ifdef GISEL_AB_TWEAK_SUBTARGET
static void tweakSubtarget(const Config &, TargetSubtarget &);
#else
static void tweakSubtarget(const Config &, TargetSubtarget &) {}
#endif

static std::optional<AtomicOrdering> parseOrdering(StringRef Text) {
  return StringSwitch<std::optional<AtomicOrdering>>(Text)
      .Case("NotAtomic", AtomicOrdering::NotAtomic)
      .Case("Unordered", AtomicOrdering::Unordered)
      .Case("Monotonic", AtomicOrdering::Monotonic)
      .Case("Acquire", AtomicOrdering::Acquire)
      .Case("Release", AtomicOrdering::Release)
      .Case("AcquireRelease", AtomicOrdering::AcquireRelease)
      .Case("SequentiallyConsistent", AtomicOrdering::SequentiallyConsistent)
      .Default(std::nullopt);
}

static std::string jsonString(StringRef S) {
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

// Core IR spelling, kind-preserving: s32 / i32 / f32 / bf16 / p0:32 /
// v4s32 / nxv4s32.  LLT::print drops the pointer width ("p0"), which would
// make ILP32 and LP64 configurations indistinguishable at p0.
static std::string lltStr(LLT Ty) {
  if (!Ty.isValid())
    return "LLT_invalid";
  if (Ty.isPointer())
    return "p" + std::to_string(Ty.getAddressSpace()) + ":" +
           std::to_string(Ty.getSizeInBits());
  if (Ty.isVector()) {
    std::string Elem = lltStr(Ty.getElementType());
    return (Ty.isScalable() ? "nxv" : "v") +
           std::to_string(Ty.getElementCount().getKnownMinValue()) + Elem;
  }
  if (Ty.isBFloat16())
    return "bf16";
  if (Ty.isFloat())
    return "f" + std::to_string(Ty.getScalarSizeInBits());
  if (Ty.isInteger())
    return "i" + std::to_string(Ty.getScalarSizeInBits());
  return "s" + std::to_string(Ty.getScalarSizeInBits());
}

static const char *actionName(LegalizeAction A) {
  switch (A) {
  case Legal: return "Legal";
  case NarrowScalar: return "NarrowScalar";
  case WidenScalar: return "WidenScalar";
  case FewerElements: return "FewerElements";
  case MoreElements: return "MoreElements";
  case Bitcast: return "Bitcast";
  case Lower: return "Lower";
  case Libcall: return "Libcall";
  case Unsupported: return "Unsupported";
  case Custom: return "Custom";
  default: return "?";
  }
}

static unsigned opcodeFor(StringRef Name) {
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
  return StringSwitch<unsigned>(Name)
#define HANDLE_TARGET_OPCODE(OPC) .Case(#OPC, TargetOpcode::OPC)
#define HANDLE_TARGET_OPCODE_MARKER(IDENT, OP)
#include "llvm/Support/TargetOpcodes.def"
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
      .Default(0);
}

// `p0:*` is the configuration-dependent pointer width; the subtarget decides
// it, exactly as it does for every other configuration axis.
static std::optional<LLT> parseLLT(StringRef S, unsigned PointerBits = 0) {
  if (S == "bf16")
    return LLT::bfloat16();
  if (S.starts_with("nxv") || S.starts_with("v")) {
    bool Scalable = S.starts_with("nxv");
    S = S.drop_front(Scalable ? 3 : 1);
    size_t I = 0;
    while (I < S.size() && S[I] >= '0' && S[I] <= '9')
      ++I;
    unsigned N = 0;
    if (I == 0 || S.substr(0, I).getAsInteger(10, N))
      return std::nullopt;
    auto Elem = parseLLT(S.drop_front(I), PointerBits);
    if (!Elem)
      return std::nullopt;
    return LLT::vector(Scalable ? ElementCount::getScalable(N)
                                : ElementCount::getFixed(N), *Elem);
  }
  if (S.starts_with("p")) {
    size_t Colon = S.find(':');
    unsigned AS = 0;
    if (Colon == StringRef::npos ||
        S.substr(1, Colon - 1).getAsInteger(10, AS))
      return std::nullopt;
    if (S.drop_front(Colon + 1) == "*") {
      if (!PointerBits)
        return std::nullopt;
      return LLT::pointer(AS, PointerBits);
    }
    unsigned Bits = 0;
    if (S.drop_front(Colon + 1).getAsInteger(10, Bits))
      return std::nullopt;
    return LLT::pointer(AS, Bits);
  }
  char Kind = S.empty() ? '?' : S[0];
  unsigned Bits = 0;
  if (S.drop_front().getAsInteger(10, Bits))
    return std::nullopt;
  switch (Kind) {
  case 's': return LLT::scalar(Bits);
  case 'i': return LLT::integer(Bits);
  case 'f': return LLT::floatIEEE(Bits);
  default: return std::nullopt;
  }
}

static bool makeSigma(const Config &C, Sigma &S) {
  Triple TT(C.Triple);
  std::string Err;
  const Target *T = TargetRegistry::lookupTarget("", TT, Err);
  if (!T) {
    errs() << "no target for " << C.ID << " (" << C.Triple << "): " << Err
           << "\n";
    return false;
  }
  TargetOptions Opts;
  S.TM.reset(T->createTargetMachine(TT, C.CPU, C.Features, Opts,
                                    std::nullopt, C.CM));
  if (!S.TM) {
    errs() << "no target machine for " << C.ID << "\n";
    return false;
  }
  static LLVMContext Ctx;
  static Module M("t", Ctx);
  if (!M.getFunction("f"))
    Function::Create(FunctionType::get(Type::getVoidTy(Ctx), false),
                     Function::ExternalLinkage, "f", &M);
  S.STI = static_cast<const TargetSubtarget *>(
      S.TM->getSubtargetImpl(*M.getFunction("f")));
  if (!S.STI) {
    errs() << "no subtarget for " << C.ID << "\n";
    return false;
  }
  // The subtarget comes back const because TargetMachine hands out a const
  // interface, but the machine owns it and is the only writer; a per-target
  // hook may need to finish configuring it (see tweakSubtarget above).
  tweakSubtarget(C, const_cast<TargetSubtarget &>(*S.STI));
  S.L = makeLegalizer(*S.STI, *S.TM);
  return bool(S.L);
}

static int answerLine(StringRef Line, const StringMap<Sigma *> &Sigmas,
                      raw_ostream &Out) {
  Expected<json::Value> Parsed = json::parse(Line);
  if (!Parsed)
    return 2;
  json::Object *Obj = Parsed->getAsObject();
  if (!Obj)
    return 2;
  auto ConfigName = Obj->getString("config");
  auto Name = Obj->getString("name");
  json::Array *TypeArr = Obj->getArray("types");
  if (!ConfigName || !Name || !TypeArr)
    return 2;
  auto It = Sigmas.find(*ConfigName);
  if (It == Sigmas.end())
    return 2;
  Sigma &S = *It->second;
  unsigned PtrBits = S.TM->createDataLayout().getPointerSizeInBits(0);
  SmallVector<LLT, 4> Types;
  for (const json::Value &V : *TypeArr) {
    auto Text = V.getAsString();
    if (!Text)
      return 2;
    auto Ty = parseLLT(*Text, PtrBits);
    if (!Ty)
      return 2;
    Types.push_back(*Ty);
  }

  // Immediate operands are part of the query: a rule may read Q.Immediates
  // (e.g. LegalizePredicates::immIs).  Dropping them makes the rule index the
  // empty array and abort, which would look like an answer difference.
  SmallVector<int64_t, 2> Imms;
  if (json::Array *ImmArr = Obj->getArray("imms"))
    for (const json::Value &V : *ImmArr) {
      auto I = V.getAsInteger();
      if (!I)
        return 2;
      Imms.push_back(*I);
    }
  std::string ImmsText = "[";
  for (size_t I = 0; I != Imms.size(); ++I) {
    if (I)
      ImmsText += ",";
    ImmsText += std::to_string(Imms[I]);
  }
  ImmsText += "]";

  SmallVector<LegalityQuery::MemDesc, 1> MMOs;
  std::string MemText = "null";
  if (json::Array *Mem = Obj->getArray("mem")) {
    if (Mem->size() != 2 && Mem->size() != 4)
      return 2;
    auto TyText = (*Mem)[0].getAsString();
    auto Align = (*Mem)[1].getAsInteger();
    if (!TyText || !Align)
      return 2;
    auto Ty = parseLLT(*TyText, PtrBits);
    if (!Ty)
      return 2;
    AtomicOrdering Ordering = AtomicOrdering::NotAtomic;
    AtomicOrdering Failure = AtomicOrdering::NotAtomic;
    std::optional<StringRef> OrderingText;
    std::optional<StringRef> FailureText;
    if (Mem->size() == 4) {
      OrderingText = (*Mem)[2].getAsString();
      FailureText = (*Mem)[3].getAsString();
      auto ParsedOrdering = OrderingText ? parseOrdering(*OrderingText)
                                         : std::nullopt;
      auto ParsedFailure = FailureText ? parseOrdering(*FailureText)
                                       : std::nullopt;
      if (!ParsedOrdering || !ParsedFailure)
        return 2;
      Ordering = *ParsedOrdering;
      Failure = *ParsedFailure;
    }
    MMOs.push_back(LegalityQuery::MemDesc(*Ty, *Align, Ordering, Failure));
    MemText = "[" + jsonString(lltStr(*Ty)) + "," + std::to_string(*Align);
    if (Mem->size() == 4)
      MemText += "," + jsonString(*OrderingText) + "," +
                 jsonString(*FailureText);
    MemText += "]";
  }

  unsigned Opcode = opcodeFor(*Name);
  if (!Opcode)
    return 2;
  LegalityQuery Q(Opcode, Types, MMOs, Imms);
  LegalizeActionStep Step = S.L->getAction(Q);
  Out << "{\"config\":" << jsonString(*ConfigName)
      << ",\"name\":" << jsonString(*Name) << ",\"types\":[";
  for (size_t I = 0; I != Types.size(); ++I) {
    if (I)
      Out << ',';
    Out << jsonString(lltStr(Types[I]));
  }
  Out << "],\"mem\":" << MemText << ",\"imms\":" << ImmsText
      << ",\"action\":" << jsonString(actionName(Step.Action))
      << ",\"type_idx\":" << (unsigned)Step.TypeIdx
      << ",\"new_type\":" << jsonString(lltStr(Step.NewType)) << "}\n";
  return 0;
}

} // namespace

static const char *opcodeName(unsigned Op) {
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

// Export the rule chain the legalizer actually built.  `getRules()` and
// `hasMutation()` come from the overlay header (patches/llvm-rule-trace.patch);
// a build against the unpatched tree fails to compile, which is the point.
static int dumpRules(Sigma &S, StringRef ConfigName, raw_ostream &Out) {
  const unsigned First = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START;
  const unsigned Last = TargetOpcode::PRE_ISEL_GENERIC_OPCODE_END;
  for (unsigned Op = First; Op <= Last; ++Op) {
    const char *Name = opcodeName(Op);
    if (!Name)
      continue;
    const LegalizeRuleSet &RS = S.L->getActionDefinitions(Op);
    const unsigned Rep = First + S.L->getActionDefinitionsIdx(Op);
    const ArrayRef<LegalizeRule> Rules = RS.getRules();
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

int main(int Argc, char **Argv) {
  StringRef Queries;
  StringRef DumpRules;
  for (int I = 1; I < Argc; ++I) {
    StringRef A(Argv[I]);
    if (A == "--queries" && I + 1 < Argc)
      Queries = Argv[++I];
    else if (A == "--dump-rules" && I + 1 < Argc)
      DumpRules = Argv[++I];
    else if (A == "-h" || A == "--help")
      return 0;
    else
      return 2;
  }
  if (Queries.empty() && DumpRules.empty())
    return 2;

  initializeTarget();

  StringMap<Sigma *> Sigmas;
  std::vector<std::unique_ptr<Sigma>> Storage;
  for (const Config &C : configs()) {
    auto S = std::make_unique<Sigma>();
    if (!makeSigma(C, *S))
      return 2;
    Sigmas[C.ID] = S.get();
    Storage.push_back(std::move(S));
  }

  if (!DumpRules.empty()) {
    std::error_code EC;
    raw_fd_ostream Out(DumpRules, EC);
    if (EC) {
      errs() << "cannot write " << DumpRules << ": " << EC.message() << "\n";
      return 2;
    }
    for (const Config &C : configs())
      dumpRules(*Sigmas[C.ID], C.ID, Out);
    Out.flush();
    return 0;
  }

  std::ifstream In(Queries.str());
  if (!In)
    return 2;
  std::string Line;
  unsigned N = 0;
  while (std::getline(In, Line)) {
    if (Line.empty())
      continue;
    if (int Rc = answerLine(Line, Sigmas, outs()))
      return Rc;
    // Flush per answer: a query that makes LLVM assert aborts the process,
    // and a buffered tail would be lost.  Answers before the crash stay.
    outs().flush();
    ++N;
  }
  errs() << "answered " << N << " queries\n";
  return 0;
}

#endif
