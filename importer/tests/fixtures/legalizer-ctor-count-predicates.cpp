// `numElementsNotPow2` and `vectorElementCountIsGreaterThan`: both read the
// element count, and both are false for a scalable vector.  Only the second
// used to carry that guard in the translation.

XLegalizerInfo::XLegalizerInfo(const XSubtarget &ST)
    : ST(ST) {
  getActionDefinitionsBuilder({G_UREM, G_SREM})
      .scalarizeIf(numElementsNotPow2(0), 0)
      .fewerElementsIf(vectorElementCountIsGreaterThan(0, 4),
                       LegalizeMutations::changeElementCountTo(
                           0, ElementCount::getFixed(4)))
      .custom();
}
