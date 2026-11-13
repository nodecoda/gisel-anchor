// A mutation lambda with a local variable and a nested `if` that reassigns it.
static constexpr unsigned MaxRegisterSize = 1024;

AMDGPULegalizerInfo::AMDGPULegalizerInfo(const GCNSubtarget &ST)
    : ST(ST) {
  getActionDefinitionsBuilder(G_MERGE_VALUES)
      .widenScalarIf(
          [](const LegalityQuery &Query) {
            return Query.Types[0].getSizeInBits() % 16 != 0;
          },
          [](const LegalityQuery &Query) {
            // Pick the next power of 2, or a multiple of 64 over 128.
            // Whichever is smaller.
            const LLT &Ty = Query.Types[0];
            unsigned NewSizeInBits = 1 << Log2_32_Ceil(Ty.getSizeInBits() + 1);
            if (NewSizeInBits >= 256) {
              unsigned RoundedTo = alignTo<64>(Ty.getSizeInBits() + 1);
              if (RoundedTo < NewSizeInBits)
                NewSizeInBits = RoundedTo;
            }
            return std::pair(0, LLT::scalar(NewSizeInBits));
          })
      .lower();
}
