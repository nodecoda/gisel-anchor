# oracle（WP3）

从**上游规则表**出题，补 `U` 方向：证明上游没有 gisel 之外的规则。

与 `import` 的区别：`import` 把上游规则抄进 `.gisel`；`oracle` 不读 `.gisel`
规则字面量，直接读 pinned 构建里的 `LegalizerInfo`。

最小 patch：在 `LegalizeRuleSet` public 区暴露 `Rules`
（`ArrayRef<LegalizeRule> getRules() const`），不改谓词/变异类型。

产物：`rules.json`、`coverage.json`、`U` queries、`FREEZE.json`。
设计见 gisel 仓库 `.omx/plans/2026-11-10-upstream-oracle.md`（本地计划）。

## WP3a 里程碑：dump-rules（X86）

不重建 LLVM 也能读上游规则链：`include/` 是构建树
`trees/explore/llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h` 的 overlay，
只多了两个 public 访问器（`LegalizeRule::hasMutation()`、
`LegalizeRuleSet::getRules()`）。布局不变，链接仍然对现成构建。

```sh
# 1. 构建 dump 工具（pin 校验 + 现成 build）
./scripts/build-dump-x86.sh /home/ubuntu/llvm/trees/explore \
    /home/ubuntu/llvm/builds/explore out/x86-dump

# 2. 导出规则（4 个配置各一份）
./out/x86-dump --dump-rules /tmp/x86.rules.jsonl
```

2026-11-10 的实测（`trees/explore` @ `57587b0`，pin `9bfc850b` + 5 patches）：

| 项 | 值 |
|---|---|
| 行数 | 1,911（C0 411 / C1 504 / C2 556 / C3 440） |
| 有规则的 opcode | 115 |
| 带变异的规则 | 1,196 行 |
| alias 行 | 825（例如 `G_SUB` → `G_ADD`） |
| 两遍 SHA | `2d6a2fe0e9f0029b…`（相同） |

输出是 JSONL，一行一条规则：

```json
{"config":"C1","opcode":"G_ADD","rule_index":0,"action":"Legal","has_mutation":false,"alias_of":null}
```

未做：`--dump-arity`（type slot / imm 数）、AArch64/RISC-V 的 dump runner、
predicate AST。这些是 WP3a 的剩余部分。
