//===- query_runner.cpp - answer a JSONL query file with one X86 legalizer -==//
//
// A/B harness for the generated legalizer .inc.  The query file is the
// answerless enumeration produced by `gisel domain`; this program answers it
// with either the shipping X86LegalizerInfo (the .inc under test) or the
// frozen X86LegalizerInfoReference (the baseline oracle).
//
// Build with -DAB_REFERENCE to select the reference class.  Everything else
// (configs, type spelling, output shape) is identical, so two builds differ
// only in the legalizer implementation.
//
//===----------------------------------------------------------------------===//

#include "X86Subtarget.h"
#include "X86TargetMachine.h"
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
#include <optional>
#include <vector>

using namespace llvm;

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

#ifdef AB_REFERENCE
#include "X86LegalizerInfoReference.h"
using Legalizer = X86LegalizerInfoReference;
#else
#include "GISel/X86LegalizerInfo.h"
using Legalizer = X86LegalizerInfo;
#endif

using namespace TargetOpcode;
using namespace LegalizeActions;

namespace {

struct Config {
  const char *ID;
  const char *Triple;
  const char *FS;
};

// The same four full-corpus configurations the frozen oracle uses.
static const Config Configs[] = {
    {"C0", "i386", "-x87"},
    {"C1", "x86_64", ""},
    {"C2", "x86_64",
     "+sse,+sse2,+sse4.1,+avx,+avx2,+avx512f,+avx512dq,+avx512bw,+avx512vl,"
     "+popcnt,+avx512vpopcntdq,+lzcnt,+bmi,+cmov"},
    {"C3", "i386", "+x87,+popcnt,+lzcnt,+bmi,+cmov"},
};

struct Sigma {
  std::unique_ptr<TargetMachine> TM;
  const X86Subtarget *STI = nullptr;
  std::unique_ptr<Legalizer> L;
};

static std::string jsonString(StringRef S) {
  std::string O = "\"";
  for (char C : S) {
    if (C == '"' || C == '\\') { O += '\\'; O += C; }
    else if (C == '\n') O += "\\n";
    else O += C;
  }
  O += "\"";
  return O;
}

// Core IR spelling, kind-preserving: s32 / i32 / f32 / bf16 / p0:32 /
// v4s32 / nxv4s32.  LLT::print drops the pointer width ("p0"), which would
// make i386 and x86_64 indistinguishable at p0.
static std::string lltStr(LLT Ty) {
  if (!Ty.isValid())
    return "LLT_invalid";
  if (Ty.isPointer())
    return "p" + std::to_string(Ty.getAddressSpace()) + ":" +
           std::to_string(Ty.getSizeInBits());
  if (Ty.isVector()) {
    std::string Elem = lltStr(Ty.getElementType());
    return (Ty.isScalable() ? "nxv" : "v") +
           std::to_string(Ty.getNumElements()) + Elem;
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

static bool isPointerLike(LLT Ty) {
  return Ty.isPointer() ||
         (Ty.isVector() && isPointerLike(Ty.getElementType()));
}

// These malformed queries currently trip an assertion inside LLVM's X86
// legalizer while it tries to change the element size of a pointer result.
// They are part of the answerless Cartesian domain, so keep them in the A/B
// stream with an explicit, symmetric status instead of aborting either side.
static bool isKnownUnanswerable(StringRef Name, ArrayRef<LLT> Types) {
  bool Opcode = StringSwitch<bool>(Name)
                    .Case("G_CTLZ", true)
                    .Case("G_CTPOP", true)
                    .Case("G_CTTZ", true)
                    .Case("G_CTTZ_ZERO_POISON", true)
                    .Default(false);
  return Opcode && !Types.empty() && isPointerLike(Types.front());
}

static unsigned opcodeFor(StringRef Name) {
  return StringSwitch<unsigned>(Name)
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
#define HANDLE_TARGET_OPCODE(OPC) .Case(#OPC, TargetOpcode::OPC)
#define HANDLE_TARGET_OPCODE_MARKER(IDENT, OPC)
#include "llvm/Support/TargetOpcodes.def"
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
      .Default(0);
}

static std::optional<LLT> parseLLT(StringRef S, unsigned PtrBits) {
  if (S == "bf16")
    return LLT::scalar(16);
  // Vector spellings: vN<elem> / nxvN<elem>.
  if (S.starts_with("nxv") || S.starts_with("v")) {
    bool Scalable = S.starts_with("nxv");
    S = S.drop_front(Scalable ? 3 : 1);
    size_t I = 0;
    while (I < S.size() && S[I] >= '0' && S[I] <= '9')
      ++I;
    unsigned N = 0;
    if (I == 0 || S.substr(0, I).getAsInteger(10, N))
      return std::nullopt;
    auto Elem = parseLLT(S.drop_front(I), PtrBits);
    if (!Elem)
      return std::nullopt;
    return LLT::vector(Scalable ? ElementCount::getScalable(N)
                                : ElementCount::getFixed(N),
                       *Elem);
  }
  if (S.starts_with("p")) {
    size_t Colon = S.find(':');
    if (Colon == StringRef::npos)
      return LLT::pointer(0, PtrBits);
    unsigned AS = 0;
    if (S.substr(1, Colon - 1).getAsInteger(10, AS))
      return std::nullopt;
    StringRef W = S.drop_front(Colon + 1);
    unsigned Bits = PtrBits;
    if (W != "*" && W.getAsInteger(10, Bits))
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
    errs() << "no target: " << Err << "\n";
    return false;
  }
  TargetOptions Opts;
  S.TM.reset(T->createTargetMachine(TT, "generic", C.FS, Opts,
                                    /*RM=*/std::nullopt));
  if (!S.TM) {
    errs() << "no target machine for " << C.ID << "\n";
    return false;
  }
  static LLVMContext Ctx;
  static Module M("t", Ctx);
  if (!M.getFunction("f"))
    Function::Create(FunctionType::get(Type::getVoidTy(Ctx), false),
                     Function::ExternalLinkage, "f", &M);
  S.STI = static_cast<const X86Subtarget *>(
      S.TM->getSubtargetImpl(*M.getFunction("f")));
  if (!S.STI) {
    errs() << "no subtarget for " << C.ID << "\n";
    return false;
  }
  S.L = std::make_unique<Legalizer>(
      *S.STI, *static_cast<const X86TargetMachine *>(S.TM.get()));
  return true;
}

static int answerLine(StringRef Line, const StringMap<Sigma *> &Sigmas,
                      raw_ostream &Out) {
  Expected<json::Value> Parsed = json::parse(Line);
  if (!Parsed) {
    errs() << "bad query JSON: " << toString(Parsed.takeError()) << "\n";
    return 2;
  }
  json::Object *Obj = Parsed->getAsObject();
  if (!Obj) {
    errs() << "query is not an object\n";
    return 2;
  }
  auto ConfigName = Obj->getString("config");
  auto Name = Obj->getString("name");
  json::Array *TypeArr = Obj->getArray("types");
  if (!ConfigName || !Name || !TypeArr) {
    errs() << "query needs config/name/types\n";
    return 2;
  }
  auto It = Sigmas.find(*ConfigName);
  if (It == Sigmas.end()) {
    errs() << "unknown config " << *ConfigName << "\n";
    return 2;
  }
  Sigma &S = *It->second;
  unsigned PtrBits = S.STI->isTarget64BitLP64() ? 64 : 32;
  SmallVector<LLT, 4> Types;
  for (const json::Value &V : *TypeArr) {
    auto Text = V.getAsString();
    if (!Text) {
      errs() << "type is not a string\n";
      return 2;
    }
    auto Ty = parseLLT(*Text, PtrBits);
    if (!Ty) {
      errs() << "bad type spelling " << *Text << "\n";
      return 2;
    }
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
    if (Mem->size() != 2 && Mem->size() != 4) {
      errs() << "mem must be [type, align] or "
                "[type, align, ordering, failure_ordering]\n";
      return 2;
    }
    auto TyText = (*Mem)[0].getAsString();
    auto Align = (*Mem)[1].getAsInteger();
    if (!TyText || !Align) {
      errs() << "mem needs string type and integer align\n";
      return 2;
    }
    auto Ty = parseLLT(*TyText, PtrBits);
    if (!Ty) {
      errs() << "bad mem type " << *TyText << "\n";
      return 2;
    }
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
      if (!ParsedOrdering || !ParsedFailure) {
        errs() << "bad memory ordering\n";
        return 2;
      }
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
  if (!Opcode) {
    errs() << "unknown opcode " << *Name << "\n";
    return 2;
  }
  std::string Tj = "[";
  for (size_t I = 0; I != Types.size(); ++I) {
    if (I)
      Tj += ",";
    Tj += jsonString(lltStr(Types[I]));
  }
  Tj += "]";

  if (isKnownUnanswerable(*Name, Types)) {
    Out << "{\"config\":" << jsonString(*ConfigName)
        << ",\"name\":" << jsonString(*Name)
        << ",\"types\":" << Tj
        << ",\"mem\":" << MemText
        << ",\"imms\":" << ImmsText
        << ",\"status\":\"unanswerable\",\"reason\":\"asserted\"}\n";
    return 0;
  }

  LegalityQuery Q(Opcode, Types, MMOs, Imms);
  LegalizeActionStep Step = S.L->getAction(Q);

  Out << "{\"config\":" << jsonString(*ConfigName)
      << ",\"name\":" << jsonString(*Name)
      << ",\"types\":" << Tj
      << ",\"mem\":" << MemText
      << ",\"imms\":" << ImmsText
      << ",\"action\":" << jsonString(actionName(Step.Action))
      << ",\"type_idx\":" << (unsigned)Step.TypeIdx
      << ",\"new_type\":" << jsonString(lltStr(Step.NewType))
      << "}\n";
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

// `getRules()`/`hasMutation()` come from the overlay header; building against
// an unpatched tree fails here on purpose.
static int dumpRules(Sigma &S, StringRef ConfigName, raw_ostream &Out) {
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

int main(int Argc, char **Argv) {
  StringRef Queries;
  StringRef DumpPath;
  for (int I = 1; I < Argc; ++I) {
    StringRef A(Argv[I]);
    if (A == "--queries" && I + 1 < Argc)
      Queries = Argv[++I];
    else if (A == "--dump-rules" && I + 1 < Argc)
      DumpPath = Argv[++I];
    else if (A == "-h" || A == "--help") {
      outs() << "usage: query_runner --queries FILE | --dump-rules FILE\n";
      return 0;
    } else {
      errs() << "unknown argument " << A << "\n";
      return 2;
    }
  }
  if (Queries.empty() && DumpPath.empty()) {
    errs() << "usage: query_runner --queries FILE | --dump-rules FILE\n";
    return 2;
  }

  LLVMInitializeX86TargetInfo();
  LLVMInitializeX86Target();
  LLVMInitializeX86TargetMC();

  StringMap<Sigma *> Sigmas;
  std::vector<std::unique_ptr<Sigma>> Storage;
  for (const Config &C : Configs) {
    auto S = std::make_unique<Sigma>();
    if (!makeSigma(C, *S))
      return 2;
    Sigmas[C.ID] = S.get();
    Storage.push_back(std::move(S));
  }

  if (!DumpPath.empty()) {
    std::error_code EC;
    raw_fd_ostream Out(DumpPath, EC);
    if (EC) {
      errs() << "cannot write " << DumpPath << ": " << EC.message() << "\n";
      return 2;
    }
    for (const Config &C : Configs)
      dumpRules(*Sigmas[C.ID], C.ID, Out);
    Out.flush();
    return 0;
  }

  std::ifstream In(Queries.str());
  if (!In) {
    errs() << "cannot open " << Queries << "\n";
    return 2;
  }
  std::string Line;
  unsigned N = 0;
  while (std::getline(In, Line)) {
    if (Line.empty())
      continue;
    if (int Rc = answerLine(Line, Sigmas, outs()))
      return Rc;
    ++N;
  }
  errs() << "answered " << N << " queries\n";
  return 0;
}
