// Host predicates inside a rule: a subtarget helper in a conjunction, a
// constructor-local bool lambda, and a file-local LegalityPredicate helper
// whose slot index is a constructor variable.
static constexpr unsigned MaxRegisterSize = 1024;

static unsigned maxSizeForAddrSpace(const GCNSubtarget &ST, unsigned AS) {
  return AS == 0 ? 512 : 128;
}

static LegalityPredicate isIllegalRegisterType(const GCNSubtarget &ST,
                                               unsigned TypeIdx) {
  return [=, &ST](const LegalityQuery &Query) {
    LLT Ty = Query.Types[TypeIdx];
    return isRegisterType(ST, Ty) &&
           !SIRegisterInfo::getSGPRClassForBitWidth(Ty.getSizeInBits());
  };
}

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  const auto needToSplit = [=](const LegalityQuery &Query,
                               bool IsLoad) -> bool {
    return IsLoad && Query.MMODescrs[0].MemoryTy.getSizeInBits() > 64;
  };

  for (unsigned Op : {G_EXTRACT_VECTOR_ELT, G_INSERT_VECTOR_ELT}) {
    unsigned VecTypeIdx = Op == G_EXTRACT_VECTOR_ELT ? 1 : 0;

    getActionDefinitionsBuilder(Op)
        .customIf([=](const LegalityQuery &Query) {
          const LLT VecTy = Query.Types[VecTypeIdx];
          const bool isLegalVecType =
              !!SIRegisterInfo::getSGPRClassForBitWidth(VecTy.getSizeInBits());
          if (VecTy.isPointer() && VecTy.getSizeInBits() > 64)
            return true;
          return VecTy.getSizeInBits() == 32 &&
                 VecTy.getSizeInBits() <= MaxRegisterSize &&
                 isLegalVecType;
        })
        .moreElementsIf(isIllegalRegisterType(ST, VecTypeIdx),
                        changeElementCountTo(0, ElementCount::getFixed(2)))
        .lower();
  }

  getActionDefinitionsBuilder(G_LOAD)
      .customIf([=](const LegalityQuery &Query) {
        return needToSplit(Query, true);
      })
      .lower();
}
