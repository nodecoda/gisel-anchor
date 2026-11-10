# oracle（WP3）

从**上游规则表**出题，补 `U` 方向：证明上游没有 gisel 之外的规则。

与 `import` 的区别：`import` 把上游规则抄进 `.gisel`；`oracle` 不读 `.gisel`
规则字面量，直接读 pinned 构建里的 `LegalizerInfo`。

最小 patch：在 `LegalizeRuleSet` public 区暴露 `Rules`
（`ArrayRef<LegalizeRule> getRules() const`），不改谓词/变异类型。

产物：`rules.json`、`coverage.json`、`U` queries、`FREEZE.json`。
设计见 gisel 仓库 `.omx/plans/2026-11-10-upstream-oracle.md`（本地计划）。

## WP3a 里程碑：dump-rules / dump-arity（X86 / AArch64 / RISC-V）

不重建 LLVM 也能读上游规则链：`include/` 是构建树
`trees/explore/llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h` 的 overlay，
只多了两个 public 访问器（`LegalizeRule::hasMutation()`、
`LegalizeRuleSet::getRules()`）。布局不变，链接仍然对现成构建。

```sh
# 构建（pin 校验 + 现成 build；每个目标一个脚本）
./scripts/build-dump-x86.sh     TREE BUILD out/x86-dump
./scripts/build-dump-aarch64.sh TREE BUILD out/a64-dump
./scripts/build-dump-riscv.sh   TREE BUILD out/rv-dump

# 导出：一行一条规则 / 每个 opcode 的槽位数
./out/x86-dump --dump-rules /tmp/x86.rules.jsonl
./out/x86-dump --dump-arity /tmp/x86.arity.jsonl
```

2026-11-10 的实测（`trees/explore` @ `57587b0`，pin `9bfc850b` + 5 patches）：

| target | 配置数 | 有规则的 opcode | 规则行 | alias 行 | 两遍 SHA |
|---|---:|---:|---:|---:|---|
| X86 | 4 | 115 | 1,911 | 825 | `2d6a2fe0e9f0029b…` |
| AArch64 | 9 | 225 | 13,240 | 6,822 | `fd7073a696427f3f…` |
| RISC-V | 8 | 186 | 4,326 | 1,993 | `d567bb020ff8577a…` |

arity 交叉检查（与 gisel 的 `data/opcode-slots.json` 比）：

| target | 比较的 opcode | 值不一致 | 快照里没有 | variadic（跳过） |
|---|---:|---:|---:|---:|
| X86 | 280 | 0 | 40 | 12 |
| AArch64 | 280 | 0 | 40 | 12 |
| RISC-V | 280 | 0 | 40 | 12 |

“快照里没有”是 gisel 的 `opcode-slots.json` 冻结在更早的 LLVM 上，不是本工具
漏读：两边都有的 opcode 上，type/imm 槽位数**逐条相同**。

输出是 JSONL，一行一条：

```json
{"config":"C1","opcode":"G_ADD","rule_index":0,"action":"Legal","has_mutation":false,"alias_of":null}
{"config":"C1","opcode":"G_ADD","type_slots":1,"imm_slots":0}
```

未做：predicate AST（计划 v2）、独立题面与覆盖引导（WP3b）、
`rules.json`/`coverage.json` 的汇总器。oracle 目前产出的是**原料**，
不是最终 U 题面。
