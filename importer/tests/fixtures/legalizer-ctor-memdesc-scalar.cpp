// A mutation helper that reads the memory type, and a predicate lambda that
// reads the same descriptor: both must become mem_slot / mem_ty expressions.
static LegalizeMutation getScalarTypeFromMemDesc(unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    unsigned MemSize = Query.MMODescrs[0].MemoryTy.getSizeInBits();
    return std::make_pair(TypeIdx, LLT::integer(MemSize));
  };
}

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  getActionDefinitionsBuilder(G_SEXTLOAD)
      .narrowScalarIf(
          [](const LegalityQuery &Query) {
            LLT MemTy = Query.MMODescrs[0].MemoryTy;
            return MemTy.isScalar() && MemTy.getSizeInBits() > 32 &&
                   Query.Types[0].getSizeInBits() > MemTy.getSizeInBits();
          },
          getScalarTypeFromMemDesc(0))
      .lower();
}
