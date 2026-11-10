// `legalForTypesWithMemDesc` rows whose align is a configuration-chosen
// integer, plus an integer expression.  The importer must split the table on
// the gate and fold `2 * 8` to 16.
AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  const LLT S8 = LLT::scalar(8);
  const LLT S16 = LLT::scalar(16);
  const LLT S32 = LLT::scalar(32);
  const LLT GlobalPtr = LLT::pointer(AMDGPUAS::GLOBAL_ADDRESS, 64);
  unsigned GlobalAlign8 = ST.hasUnalignedBufferAccessEnabled() ? 0 : 8;

  getActionDefinitionsBuilder(G_LOAD)
      .legalForTypesWithMemDesc({{S32, GlobalPtr, S8, GlobalAlign8},
                                 {S32, GlobalPtr, S16, 2 * 8}});
}
