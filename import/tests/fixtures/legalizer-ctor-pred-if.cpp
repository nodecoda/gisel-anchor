// `minScalarIf(P, i, T)` and a predicate that compares against a file-scope
// integer constant.
static constexpr unsigned MaxRegisterSize = 1024;
constexpr LLT F16 = LLT::float16();

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  const LLT I16 = LLT::integer(16);
  const LLT I32 = LLT::integer(32);
  const LLT F32 = LLT::float32();

  getActionDefinitionsBuilder(G_FPTOSI_SAT)
      .legalFor({{I32, F32}})
      .minScalarIf(typeIs(1, F16), 0, I16)
      .lower();

  getActionDefinitionsBuilder(G_ADD)
      .legalIf([=](const LegalityQuery &Query) {
        return Query.Types[0].getSizeInBits() <= MaxRegisterSize;
      })
      .lower();
}
