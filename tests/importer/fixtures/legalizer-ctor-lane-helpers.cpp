// The three AMDGPU lane-count helpers.  Their bodies are local declarations
// plus `return std::pair(i, <LLT expr>)`; the importer reads the shape, not
// the names, so a renamed copy still round-trips.
static LegalizeMutation oneMoreElement(unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    const LLT Ty = Query.Types[TypeIdx];
    const LLT EltTy = Ty.getElementType();
    return std::pair(TypeIdx,
                     LLT::fixed_vector(Ty.getNumElements() + 1, EltTy));
  };
}

static LegalizeMutation fewerEltsToSize64Vector(unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    const LLT Ty = Query.Types[TypeIdx];
    const LLT EltTy = Ty.getElementType();
    unsigned Size = Ty.getSizeInBits();
    unsigned Pieces = (Size + 63) / 64;
    unsigned NewNumElts = (Ty.getNumElements() + 1) / Pieces;
    return std::pair(TypeIdx, LLT::scalarOrVector(
                                  ElementCount::getFixed(NewNumElts), EltTy));
  };
}

// Increase the number of vector elements to reach the next multiple of 32-bit
// type.
static LegalizeMutation moreEltsToNext32Bit(unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    const LLT Ty = Query.Types[TypeIdx];

    const LLT EltTy = Ty.getElementType();
    const int Size = Ty.getSizeInBits();
    const int EltSize = EltTy.getSizeInBits();
    const int NextMul32 = (Size + 31) / 32;

    assert(EltSize < 32);

    const int NewNumElts = (32 * NextMul32 + EltSize - 1) / EltSize;
    return std::pair(TypeIdx, LLT::fixed_vector(NewNumElts, EltTy));
  };
}

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  const LLT S32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor({S32})
      .moreElementsIf([=](const LegalityQuery &Query) { return true; },
                      oneMoreElement(0));
  getActionDefinitionsBuilder(G_SUB)
      .moreElementsIf([=](const LegalityQuery &Query) { return true; },
                      moreEltsToNext32Bit(0));
  getActionDefinitionsBuilder(G_MUL)
      .fewerElementsIf([=](const LegalityQuery &Query) { return true; },
                       fewerEltsToSize64Vector(0));
}
