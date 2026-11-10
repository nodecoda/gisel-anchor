# oracle（WP3）

从**上游规则表**出题，补 `U` 方向：证明上游没有 gisel 之外的规则。

与 `import` 的区别：`import` 把上游规则抄进 `.gisel`；`oracle` 不读 `.gisel`
规则字面量，直接读 pinned 构建里的 `LegalizerInfo`。

最小 patch：在 `LegalizeRuleSet` public 区暴露 `Rules`
（`ArrayRef<LegalizeRule> getRules() const`），不改谓词/变异类型。

产物：`rules.json`、`coverage.json`、`U` queries、`FREEZE.json`。
设计见 gisel 仓库 `.omx/plans/2026-11-10-upstream-oracle.md`（本地计划）。
