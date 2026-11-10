// `clampMaxNumElementsStrict` is `alignNumElementsTo` + `clampMaxNumElements`
// (LegalizerInfo.h:1327); the importer must keep both rules.
constexpr LLT S16 = LLT::scalar(16);
constexpr LLT V2S16 = LLT::fixed_vector(2, 16);

RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST)
    : STI(ST) {
  getActionDefinitionsBuilder({G_ADD, G_SUB})
      .legalFor({S16, V2S16})
      .clampMaxNumElementsStrict(0, S16, 2)
      .scalarize(0);
}
