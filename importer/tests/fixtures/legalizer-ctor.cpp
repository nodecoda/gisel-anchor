// A miniature legalizer constructor exercising the importer's hard cases.
RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST)
    : STI(ST), XLen(STI.getXLen()), sXLen(LLT::scalar(XLen)) {
  const LLT s32 = LLT::scalar(32);
  auto &A = getActionDefinitionsBuilder(G_CTLZ);
  if (ST.hasStdExtZbb()) {
    A.legalFor({{s32, s32}}).clampScalar(0, s32, s32);
  } else {
    A.maxScalar(0, s32);
  }
  getActionDefinitionsBuilder(G_ADD)
      .minScalar(ST.hasStdExtZbb(), 0, s32)
      .lower();
}
