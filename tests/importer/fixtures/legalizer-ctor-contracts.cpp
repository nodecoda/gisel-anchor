// A constructor-level assert is a contract, and `sameSize` is a predicate.
RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST) {
  assert(ST.hasStdExtZbb());
  getActionDefinitionsBuilder(G_BITCAST).legalIf(sameSize(0, 1));
}
