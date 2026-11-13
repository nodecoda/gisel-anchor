// `clampNumElements(i, V16S32, V32S32)`: the element type is the element of a
// vector constant, whose spelling may be upper case (`V16S32`).
constexpr LLT S32 = LLT::scalar(32);
constexpr LLT S64 = LLT::scalar(64);
constexpr LLT V16S32 = LLT::fixed_vector(16, 32);
constexpr LLT V32S32 = LLT::fixed_vector(32, 32);
constexpr LLT V2S64 = LLT::fixed_vector(2, 64);
constexpr LLT V16S64 = LLT::fixed_vector(16, 64);

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  getActionDefinitionsBuilder(G_IMPLICIT_DEF)
      .clampNumElements(0, V16S32, V32S32)
      .clampNumElements(0, V2S64, V16S64)
      .lower();
}
