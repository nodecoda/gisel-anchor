// A predicate lambda spelled with an explicit return type.
RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST)
    : STI(ST), XLen(STI.getXLen()) {
  getActionDefinitionsBuilder(G_ADD)
      .legalIf([=](const LegalityQuery &Query) -> bool {
        return Query.Types[0].isScalar();
      });
}
