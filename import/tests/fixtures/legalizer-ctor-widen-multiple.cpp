// `widenScalarToNextMultipleOf` rounds a scalar up to a multiple, not to the
// next power of two (LegalizerInfo.h:989 -> changeElementSize(alignTo(ssz, N))).
// The importer must keep it as `widen(slot(i), multiple_of(N))`.
AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  const LLT S16 = LLT::scalar(16);
  const LLT S32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor({S32})
      .widenScalarToNextMultipleOf(0, 32)
      .clampScalar(0, S32, S32);
}
