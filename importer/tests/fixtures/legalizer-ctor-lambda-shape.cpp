// A lambda whose shape the importer does not know: refuse, do not recurse.
RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST) {
  getActionDefinitionsBuilder(G_ADD)
      .legalIf([=](const LegalityQuery &Query) constexpr { return true; });
}
