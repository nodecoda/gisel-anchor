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
#include "llvm/CodeGen/TargetInstrInfo.h"
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
#include "rule_dump.h"

#include <algorithm>
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

// Parse the parts shared by query files and the witness universe.  The query
// owns all storage, so the JSON value can go away on return.
static bool parseBody(json::Object &Obj, unsigned PtrBits, std::string &Name,
                      rule_dump::ParsedQuery &Out) {
  auto NameV = Obj.getString("name");
  json::Array *TypeArr = Obj.getArray("types");
  if (!NameV || !TypeArr)
    return false;
  Name = NameV->str();
  Out.Types.clear();
  for (const json::Value &V : *TypeArr) {
    auto Text = V.getAsString();
    if (!Text)
      return false;
    auto Ty = parseLLT(*Text, PtrBits);
    if (!Ty)
      return false;
    Out.Types.push_back(*Ty);
  }

  // Immediate operands are part of the query: a rule may read Q.Immediates
  // (e.g. LegalizePredicates::immIs).  Dropping them makes the rule index the
  // empty array and abort, which would look like an answer difference.
  Out.Imms.clear();
  if (json::Array *ImmArr = Obj.getArray("imms"))
    for (const json::Value &V : *ImmArr) {
      auto I = V.getAsInteger();
      if (!I)
        return false;
      Out.Imms.push_back(*I);
    }

  Out.MMOs.clear();
  Out.HasMem = false;
  Out.OrderingText.clear();
  Out.FailureText.clear();
  if (json::Array *Mem = Obj.getArray("mem")) {
    if (Mem->size() != 2 && Mem->size() != 4)
      return false;
    auto TyText = (*Mem)[0].getAsString();
    auto Align = (*Mem)[1].getAsInteger();
    if (!TyText || !Align)
      return false;
    auto Ty = parseLLT(*TyText, PtrBits);
    if (!Ty)
      return false;
    AtomicOrdering Ordering = AtomicOrdering::NotAtomic;
    AtomicOrdering Failure = AtomicOrdering::NotAtomic;
    if (Mem->size() == 4) {
      auto OrderingText = (*Mem)[2].getAsString();
      auto FailureText = (*Mem)[3].getAsString();
      auto ParsedOrdering = OrderingText ? parseOrdering(*OrderingText)
                                         : std::nullopt;
      auto ParsedFailure = FailureText ? parseOrdering(*FailureText)
                                       : std::nullopt;
      if (!ParsedOrdering || !ParsedFailure)
        return false;
      Ordering = *ParsedOrdering;
      Failure = *ParsedFailure;
      Out.OrderingText = OrderingText->str();
      Out.FailureText = FailureText->str();
    }
    Out.MMOs.push_back(LegalityQuery::MemDesc(*Ty, *Align, Ordering, Failure));
    Out.HasMem = true;
  }

  Out.Opcode = opcodeFor(Name);
  return Out.Opcode != 0;
}

static int parseQuery(StringRef Line, const StringMap<Sigma *> &Sigmas,
                      Sigma *&SOut, std::string &ConfigName,
                      std::string &Name, rule_dump::ParsedQuery &Out) {
  Expected<json::Value> Parsed = json::parse(Line);
  if (!Parsed)
    return 2;
  json::Object *Obj = Parsed->getAsObject();
  if (!Obj)
    return 2;
  auto Config = Obj->getString("config");
  if (!Config)
    return 2;
  auto It = Sigmas.find(*Config);
  if (It == Sigmas.end())
    return 2;
  SOut = It->second;
  ConfigName = Config->str();
  unsigned PtrBits = SOut->STI->isTarget64BitLP64() ? 64 : 32;
  return parseBody(*Obj, PtrBits, Name, Out) ? 0 : 2;
}

static std::string immText(const rule_dump::ParsedQuery &P) {
  std::string Text = "[";
  for (size_t I = 0; I != P.Imms.size(); ++I) {
    if (I)
      Text += ",";
    Text += std::to_string(P.Imms[I]);
  }
  return Text + "]";
}

static std::string memText(const rule_dump::ParsedQuery &P) {
  if (!P.HasMem)
    return "null";
  const LegalityQuery::MemDesc &M = P.MMOs.front();
  std::string Text = "[" + jsonString(lltStr(M.MemoryTy)) + "," +
                     std::to_string(M.AlignInBits);
  if (!P.OrderingText.empty())
    Text += "," + jsonString(P.OrderingText) + "," +
            jsonString(P.FailureText);
  return Text + "]";
}

static int answerLine(StringRef Line, const StringMap<Sigma *> &Sigmas,
                      raw_ostream &Out) {
  Sigma *S = nullptr;
  std::string ConfigName, Name;
  rule_dump::ParsedQuery P;
  if (int Rc = parseQuery(Line, Sigmas, S, ConfigName, Name, P))
    return Rc;

  std::string Tj = "[";
  for (size_t I = 0; I != P.Types.size(); ++I) {
    if (I)
      Tj += ",";
    Tj += jsonString(lltStr(P.Types[I]));
  }
  Tj += "]";
  std::string MemText = memText(P);
  std::string ImmsText = immText(P);

  if (isKnownUnanswerable(Name, P.Types)) {
    Out << "{\"config\":" << jsonString(ConfigName)
        << ",\"name\":" << jsonString(Name) << ",\"types\":" << Tj
        << ",\"mem\":" << MemText << ",\"imms\":" << ImmsText
        << ",\"status\":\"unanswerable\",\"reason\":\"asserted\"}\n";
    return 0;
  }

  LegalityQuery Q = P.query();
  LegalizeActionStep Step = S->L->getAction(Q);
  Out << "{\"config\":" << jsonString(ConfigName)
      << ",\"name\":" << jsonString(Name) << ",\"types\":" << Tj
      << ",\"mem\":" << MemText << ",\"imms\":" << ImmsText
      << ",\"action\":" << jsonString(actionName(Step.Action))
      << ",\"type_idx\":" << (unsigned)Step.TypeIdx
      << ",\"new_type\":" << jsonString(lltStr(Step.NewType)) << "}\n";
  return 0;
}

static int hitsLine(StringRef Line, const StringMap<Sigma *> &Sigmas,
                    raw_ostream &Out) {
  Sigma *S = nullptr;
  std::string ConfigName, Name;
  rule_dump::ParsedQuery P;
  if (int Rc = parseQuery(Line, Sigmas, S, ConfigName, Name, P))
    return Rc;
  rule_dump::hitsLine(*S, ConfigName, P.query(), Line, Out);
  return 0;
}

static int runWitness(StringRef ConfigName, StringRef UniversePath,
                      unsigned Budget, StringRef OutPath,
                      const StringMap<Sigma *> &Sigmas) {
  auto It = Sigmas.find(ConfigName);
  if (It == Sigmas.end()) {
    errs() << "unknown config " << ConfigName << "\n";
    return 2;
  }
  Sigma &S = *It->second;
  std::ifstream In(UniversePath.str());
  if (!In) {
    errs() << "cannot open " << UniversePath << "\n";
    return 2;
  }
  std::vector<std::string> Lines;
  std::string Line;
  while (std::getline(In, Line))
    if (!Line.empty())
      Lines.push_back(Line);

  unsigned PtrBits = S.STI->isTarget64BitLP64() ? 64 : 32;
  auto Parse = [PtrBits](StringRef Text, rule_dump::ParsedQuery &P) {
    Expected<json::Value> Parsed = json::parse(Text);
    if (!Parsed)
      return 2;
    json::Object *Obj = Parsed->getAsObject();
    if (!Obj)
      return 2;
    std::string Name;
    if (!parseBody(*Obj, PtrBits, Name, P))
      return 2;
    return isKnownUnanswerable(Name, P.Types) ? 1 : 0;
  };

  std::error_code EC;
  raw_fd_ostream Out(OutPath, EC);
  if (EC) {
    errs() << "cannot write " << OutPath << ": " << EC.message() << "\n";
    return 2;
  }
  return rule_dump::witness(S, ConfigName, Lines, Budget, Parse, Out);
}

static int runFilter(StringRef ConfigName, StringRef InPath, StringRef OutPath,
                     StringRef DropPath, const StringMap<Sigma *> &Sigmas) {
  auto It = Sigmas.find(ConfigName);
  if (It == Sigmas.end()) {
    errs() << "unknown config " << ConfigName << "\n";
    return 2;
  }
  Sigma &S = *It->second;
  std::ifstream In(InPath.str());
  if (!In) {
    errs() << "cannot open " << InPath << "\n";
    return 2;
  }
  std::vector<std::string> Lines;
  std::string Line;
  while (std::getline(In, Line))
    if (!Line.empty())
      Lines.push_back(Line);

  unsigned PtrBits = S.STI->isTarget64BitLP64() ? 64 : 32;
  auto Parse = [PtrBits](StringRef Text, rule_dump::ParsedQuery &P) {
    Expected<json::Value> Parsed = json::parse(Text);
    if (!Parsed)
      return 2;
    json::Object *Obj = Parsed->getAsObject();
    if (!Obj)
      return 2;
    std::string Name;
    if (!parseBody(*Obj, PtrBits, Name, P))
      return 2;
    return isKnownUnanswerable(Name, P.Types) ? 1 : 0;

  };

  std::error_code EC1, EC2;
  raw_fd_ostream Kept(OutPath, EC1);
  raw_fd_ostream Dropped(DropPath, EC2);
  if (EC1 || EC2) {
    errs() << "cannot write " << (EC1 ? OutPath : DropPath) << "\n";
    return 2;
  }
  return rule_dump::filterAnswerable(S, Lines, Parse, Kept, Dropped);
}

} // namespace

static const char *opcodeName(unsigned Op);

// Type-slot and immediate-slot counts, computed exactly like
// LegalizerInfo::verify(): dense indices from the generic operand kinds.
static int dumpArity(Sigma &S, StringRef ConfigName, raw_ostream &Out) {
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
  StringRef ArityPath;
  StringRef HitsPath;
  StringRef WitnessConfig;
  StringRef UniversePath;
  StringRef WitnessOut;
  StringRef FilterIn;
  StringRef FilterOut;
  StringRef FilterDrop;
  unsigned Budget = 20000;
  for (int I = 1; I < Argc; ++I) {
    StringRef A(Argv[I]);
    if (A == "--queries" && I + 1 < Argc)
      Queries = Argv[++I];
    else if (A == "--dump-rules" && I + 1 < Argc)
      DumpPath = Argv[++I];
    else if (A == "--dump-arity" && I + 1 < Argc)
      ArityPath = Argv[++I];
    else if (A == "--dump-hits" && I + 1 < Argc)
      HitsPath = Argv[++I];
    else if (A == "--witness" && I + 1 < Argc)
      WitnessConfig = Argv[++I];
    else if (A == "--universe" && I + 1 < Argc)
      UniversePath = Argv[++I];
    else if (A == "--out" && I + 1 < Argc)
      WitnessOut = Argv[++I];
    else if (A == "--filter-in" && I + 1 < Argc)
      FilterIn = Argv[++I];
    else if (A == "--filter-out" && I + 1 < Argc)
      FilterOut = Argv[++I];
    else if (A == "--filter-drop" && I + 1 < Argc)
      FilterDrop = Argv[++I];
    else if (A == "--budget" && I + 1 < Argc)
      Budget = (unsigned)std::strtoul(Argv[++I], nullptr, 10);
    else if (A == "-h" || A == "--help") {
      outs() << "usage: runner --queries FILE | --dump-rules FILE | "
                "--dump-arity FILE | --dump-hits FILE | "
                "--witness CONFIG --universe FILE --out FILE [--budget N] | "
                "--witness CONFIG --filter-in FILE --filter-out FILE "
                "--filter-drop FILE\n";
      return 0;
    } else {
      errs() << "unknown argument " << A << "\n";
      return 2;
    }
  }
  if (Queries.empty() && DumpPath.empty() && ArityPath.empty() &&
      HitsPath.empty() && WitnessConfig.empty()) {
    errs() << "nothing to do\n";
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

  if (!FilterIn.empty()) {
    if (WitnessConfig.empty() || FilterOut.empty() || FilterDrop.empty()) {
      errs() << "--filter needs --witness CONFIG --filter-out FILE "
                "--filter-drop FILE\n";
      return 2;
    }
    return runFilter(WitnessConfig, FilterIn, FilterOut, FilterDrop, Sigmas);
  }

  if (!WitnessConfig.empty()) {
    if (UniversePath.empty() || WitnessOut.empty()) {
      errs() << "--witness needs --universe and --out\n";
      return 2;
    }
    return runWitness(WitnessConfig, UniversePath, Budget, WitnessOut, Sigmas);
  }

  StringRef Input = !HitsPath.empty() ? HitsPath : Queries;
  std::ifstream In(Input.str());
  if (!In) {
    errs() << "cannot open " << Input << "\n";
    return 2;
  }
  std::string Line;
  unsigned N = 0;
  while (std::getline(In, Line)) {
    if (Line.empty())
      continue;
    int Rc = HitsPath.empty() ? answerLine(Line, Sigmas, outs())
                              : hitsLine(Line, Sigmas, outs());
    if (Rc)
      return Rc;
    // Flush per answer: a query that makes LLVM assert aborts the process,
    // and a buffered tail would be lost.  Answers before the crash stay.
    outs().flush();
    ++N;
  }
  errs() << "answered " << N << " queries\n";
  return 0;
}
