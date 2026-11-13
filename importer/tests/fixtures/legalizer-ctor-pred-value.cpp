// A predicate value: `isRegisterType(ST, 0)` returns a LegalityPredicate that
// delegates to the `bool` overload.  Following it recurses, so the importer
// must copy the call through `opaque(...)` instead of inlining it.
static bool isRegisterType(const RISCVSubtarget &ST, LLT Ty) {
  return Ty.isScalar();
}

static LegalityPredicate isRegisterType(const RISCVSubtarget &ST,
                                        unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    return isRegisterType(ST, Query.Types[TypeIdx]);
  };
}

RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST)
    : STI(ST) {
  getActionDefinitionsBuilder(G_ADD)
      .legalIf(isRegisterType(ST, 0))
      .lower();
}
