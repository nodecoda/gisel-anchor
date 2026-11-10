//===- aarch64_query_runner.cpp - answer fixed AArch64 queries ------------===//

#include "AArch64Subtarget.h"
#include "AArch64TargetMachine.h"
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
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Target/TargetMachine.h"
#include "llvm/Target/TargetOptions.h"
#include "llvm/TargetParser/Triple.h"
#include <fstream>
#include "rule_dump.h"
#include <memory>
#include <optional>
#include <string>
#include <vector>

using namespace llvm;
using namespace TargetOpcode;
using namespace LegalizeActions;
using namespace LegalizeMutations;
using namespace LegalityPredicates;

#include "GISel/AArch64LegalizerInfo.h"
using Legalizer = AArch64LegalizerInfo;

namespace {

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

struct Config {
  const char *ID;
  const char *Features;
  CodeModel::Model CM;
};

static const Config Configs[] = {
    {"baseline", "", CodeModel::Large},
    {"small-cm", "", CodeModel::Small},
    {"fullfp16", "+fullfp16", CodeModel::Large},
    {"sve", "+sve", CodeModel::Large},
    {"bf16", "+bf16", CodeModel::Large},
    {"cssc", "+cssc", CodeModel::Large},
    {"rcpc3", "+rcpc3", CodeModel::Large},
    {"lse", "+lse", CodeModel::Large},
    {"outline-atomics", "+outline-atomics", CodeModel::Large},
};

struct Sigma {
  std::unique_ptr<TargetMachine> TM;
  const AArch64Subtarget *STI = nullptr;
  std::unique_ptr<Legalizer> L;
};

static std::string jsonString(StringRef S) {
  std::string Out = "\"";
  for (char C : S) {
    if (C == '\"' || C == '\\') {
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
// it (LP64 -> 64, ILP32 -> 32), exactly as it does for RISC-V's XLen.
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
  Triple TT("aarch64");
  std::string Err;
  const Target *T = TargetRegistry::lookupTarget("", TT, Err);
  if (!T) {
    errs() << "no target: " << Err << "\n";
    return false;
  }
  TargetOptions Opts;
  S.TM.reset(T->createTargetMachine(TT, "generic", C.Features, Opts,
                                    std::nullopt, C.CM));
  if (!S.TM)
    return false;
  static LLVMContext Ctx;
  static Module M("t", Ctx);
  if (!M.getFunction("f"))
    Function::Create(FunctionType::get(Type::getVoidTy(Ctx), false),
                     Function::ExternalLinkage, "f", &M);
  S.STI = static_cast<const AArch64Subtarget *>(
      S.TM->getSubtargetImpl(*M.getFunction("f")));
  if (!S.STI)
    return false;
  S.L = std::make_unique<Legalizer>(*S.STI);
  return true;
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
    MemText = "[" + jsonString(lltStr(*Ty)) + "," +
              std::to_string(*Align);
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

int main(int Argc, char **Argv) {
  StringRef Queries;
  StringRef DumpPath;
  StringRef ArityPath;
  for (int I = 1; I < Argc; ++I) {
    StringRef A(Argv[I]);
    if (A == "--queries" && I + 1 < Argc)
      Queries = Argv[++I];
    else if (A == "--dump-rules" && I + 1 < Argc)
      DumpPath = Argv[++I];
    else if (A == "--dump-arity" && I + 1 < Argc)
      ArityPath = Argv[++I];
    else if (A == "-h" || A == "--help")
      return 0;
    else
      return 2;
  }
  if (Queries.empty() && DumpPath.empty() && ArityPath.empty())
    return 2;

  LLVMInitializeAArch64TargetInfo();
  LLVMInitializeAArch64Target();
  LLVMInitializeAArch64TargetMC();

  StringMap<Sigma *> Sigmas;
  std::vector<std::unique_ptr<Sigma>> Storage;
  for (const Config &C : Configs) {
    auto S = std::make_unique<Sigma>();
    if (!makeSigma(C, *S))
      return 2;
    Sigmas[C.ID] = S.get();
    Storage.push_back(std::move(S));
  }

  if (!ArityPath.empty()) {
    std::error_code EC;
    raw_fd_ostream Out(ArityPath, EC);
    if (EC) {
      errs() << "cannot write " << ArityPath << ": " << EC.message() << "\n";
      return 2;
    }
    for (const Config &C : Configs)
      rule_dump::arity(*Sigmas[C.ID], C.ID, Out);
    Out.flush();
    return 0;
  }

  if (!DumpPath.empty()) {
    std::error_code EC;
    raw_fd_ostream Out(DumpPath, EC);
    if (EC) {
      errs() << "cannot write " << DumpPath << ": " << EC.message() << "\n";
      return 2;
    }
    for (const Config &C : Configs)
      rule_dump::rules(*Sigmas[C.ID], C.ID, Out);
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
