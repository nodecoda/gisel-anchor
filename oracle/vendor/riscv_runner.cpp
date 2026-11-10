//===- riscv_query_runner.cpp - answer fixed RISC-V queries --------------===//

#include "RISCVSubtarget.h"
#include "RISCVTargetMachine.h"
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

#include "GISel/RISCVLegalizerInfo.h"
using Legalizer = RISCVLegalizerInfo;

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
  const char *Triple;
  const char *Features;
};

// Feature strings must produce the predicates in
// tests/cases/riscv-features.json; `ST.hasStdExtZmmul()` is implied by M.
static const Config Configs[] = {
    {"rv64gc", "riscv64", "+m,+a,+f,+d,+c"},
    {"rv32gc", "riscv32", "+m,+a,+f,+d,+c"},
    {"rv64gcv", "riscv64", "+m,+a,+f,+d,+c,+v"},
    {"rv32gcv", "riscv32", "+m,+a,+f,+d,+c,+v"},
    {"rv64zbb", "riscv64", "+m,+a,+f,+d,+c,+zbb,+zbc,+zbkc"},
    {"rv64zfh", "riscv64", "+m,+a,+f,+d,+c,+zfh"},
    {"ristretto", "riscv64", "+m,+a,+f,+d,+c,+zbb,+zfa,+zfh,+v"},
    {"rv32zilsd", "riscv32", "+m,+a,+f,+d,+c,+zilsd"},
};

struct Sigma {
  std::unique_ptr<TargetMachine> TM;
  const RISCVSubtarget *STI = nullptr;
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
// it (rv32 -> 32, rv64 -> 64).
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
    auto Elem = parseLLT(S.drop_front(I));
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
    errs() << "no target: " << Err << "\n";
    return false;
  }
  TargetOptions Opts;
  S.TM.reset(T->createTargetMachine(TT, "generic", C.Features, Opts,
                                    std::nullopt, std::nullopt));
  if (!S.TM)
    return false;
  static LLVMContext Ctx;
  static Module M("t", Ctx);
  if (!M.getFunction("f"))
    Function::Create(FunctionType::get(Type::getVoidTy(Ctx), false),
                     Function::ExternalLinkage, "f", &M);
  S.STI = static_cast<const RISCVSubtarget *>(
      S.TM->getSubtargetImpl(*M.getFunction("f")));
  if (!S.STI)
    return false;
  S.L = std::make_unique<Legalizer>(*S.STI);
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
  unsigned PtrBits = SOut->STI->getXLen();
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

  unsigned PtrBits = S.STI->getXLen();
  auto Parse = [PtrBits](StringRef Text, rule_dump::ParsedQuery &P) {
    Expected<json::Value> Parsed = json::parse(Text);
    if (!Parsed)
      return 2;
    json::Object *Obj = Parsed->getAsObject();
    if (!Obj)
      return 2;
    std::string Name;
    return parseBody(*Obj, PtrBits, Name, P) ? 0 : 2;
  };

  std::error_code EC;
  raw_fd_ostream Out(OutPath, EC);
  if (EC) {
    errs() << "cannot write " << OutPath << ": " << EC.message() << "\n";
    return 2;
  }
  return rule_dump::witness(S, ConfigName, Lines, Budget, Parse, Out);
}

} // namespace

int main(int Argc, char **Argv) {
  StringRef Queries;
  StringRef DumpPath;
  StringRef ArityPath;
  StringRef HitsPath;
  StringRef WitnessConfig;
  StringRef UniversePath;
  StringRef WitnessOut;
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
    else if (A == "--budget" && I + 1 < Argc)
      Budget = (unsigned)std::strtoul(Argv[++I], nullptr, 10);
    else if (A == "-h" || A == "--help") {
      outs() << "usage: runner --queries FILE | --dump-rules FILE | "
                "--dump-arity FILE | --dump-hits FILE | "
                "--witness CONFIG --universe FILE --out FILE [--budget N]\n";
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

  LLVMInitializeRISCVTargetInfo();
  LLVMInitializeRISCVTarget();
  LLVMInitializeRISCVTargetMC();

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
