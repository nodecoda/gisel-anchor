// A constructor with statements the importer cannot translate.  It must
// refuse them with their source line, not abort the run with a traceback.
// The second statement names a builder the importer does not know.
RISCVLegalizerInfo::RISCVLegalizerInfo(const RISCVSubtarget &ST)
    : STI(ST) {
  getActionDefinitionsBuilder(G_ADD).someUnknownBuilder({s32});
  getActionDefinitionsBuilder(G_SUB).legalFor({s32});
}
