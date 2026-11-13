// `bitcast` targets computed from the query.  Two are named helpers; the third
// is an inline lambda with a configuration-selected element width.
static LLT getBitcastRegisterType(const LLT Ty) {
  const unsigned Size = Ty.getSizeInBits();

  if (Size <= 32) {
    // <2 x i8> -> i16
    // <4 x i8> -> i32
    return LLT::integer(Size);
  }

  return LLT::fixed_vector(Size / 32, LLT::integer(32));
}

static LegalizeMutation bitcastToRegisterType(unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    const LLT Ty = Query.Types[TypeIdx];
    return std::pair(TypeIdx, getBitcastRegisterType(Ty));
  };
}

static LegalizeMutation bitcastToVectorElement32(unsigned TypeIdx) {
  return [=](const LegalityQuery &Query) {
    const LLT Ty = Query.Types[TypeIdx];
    unsigned Size = Ty.getSizeInBits();
    assert(Size % 32 == 0);
    return std::pair(TypeIdx,
                     LLT::scalarOrVector(ElementCount::getFixed(Size / 32),
                                         LLT::integer(32)));
  };
}

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  unsigned EltTypeIdx = 0;
  unsigned VecTypeIdx = 1;
  const LLT S32 = LLT::scalar(32);
  getActionDefinitionsBuilder(G_ADD)
      .legalFor({S32})
      .bitcastIf([=](const LegalityQuery &Query) { return true; },
                 bitcastToRegisterType(0));
  getActionDefinitionsBuilder(G_SUB)
      .bitcastIf([=](const LegalityQuery &Query) { return true; },
                 bitcastToVectorElement32(0));
  getActionDefinitionsBuilder(G_MUL)
      .bitcastIf([=](const LegalityQuery &Query) { return true; },
                 [=](const LegalityQuery &Query) {
                   // For > 64-bit element types, try to turn this into a
                   // 64-bit element vector since we may be able to do better
                   // indexing if this is scalar. If not, fall back to 32.
                   const LLT EltTy = Query.Types[EltTypeIdx];
                   const LLT VecTy = Query.Types[VecTypeIdx];
                   const unsigned DstEltSize = EltTy.getSizeInBits();
                   const unsigned VecSize = VecTy.getSizeInBits();

                   const unsigned TargetEltSize =
                       DstEltSize % 64 == 0 ? 64 : 32;
                   return std::pair(VecTypeIdx,
                                    LLT::fixed_vector(VecSize / TargetEltSize,
                                                      TargetEltSize));
                 });
}
