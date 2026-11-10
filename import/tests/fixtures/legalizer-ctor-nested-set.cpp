// `{{S32}, {S64}}` is `initializer_list<LLT>` in C++ overload resolution: the
// one-type overload, so it constrains slot 0 only.  It must not be read as a
// type pair.
AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  const LLT S16 = LLT::scalar(16);
  const LLT S32 = LLT::scalar(32);
  const LLT S64 = LLT::scalar(64);
  const LLT V2S16 = LLT::fixed_vector(2, 16);

  getActionDefinitionsBuilder(G_SEXT_INREG)
      .legalFor({{S32}, {S64}})
      .lowerFor({{V2S16}});

  getActionDefinitionsBuilder(G_ADD)
      .lowerFor({{S32}, {S64}, {S16}});
}
