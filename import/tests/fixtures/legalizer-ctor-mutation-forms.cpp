// Mutations whose first argument is a constructor variable, plus `scalarize`
// and `changeElementSizeTo` used as mutation bodies.
constexpr LLT S32 = LLT::scalar(32);

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  unsigned BigTyIdx = 1;
  unsigned LitTyIdx = 0;
  getActionDefinitionsBuilder(G_ADD)
      .widenScalarIf(
          [=](const LegalityQuery &Query) {
            const LLT Ty = Query.Types[BigTyIdx];
            return Ty.getSizeInBits() % 16 != 0;
          },
          LegalizeMutations::widenScalarOrEltToNextPow2(BigTyIdx, 16));
  getActionDefinitionsBuilder(G_SUB)
      .fewerElementsIf(
          [=](const LegalityQuery &Query) {
            return Query.Types[0].isVector();
          },
          LegalizeMutations::scalarize(0));
  getActionDefinitionsBuilder(G_MUL)
      .widenScalarIf(
          [=](const LegalityQuery &Query) {
            const LLT Ty = Query.Types[LitTyIdx];
            return Ty.getSizeInBits() < 32;
          },
          changeElementSizeTo(LitTyIdx, S32));
}
