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

## WP3b/U1 里程碑：dump-hits / witness

`--dump-hits` 把每条查询的“第一条命中规则”和“被遮蔽的规则”打出来；
`--witness` 在给定候选宇宙上为每条规则找第一条 witness。两者都用
`rule.match`，与 `getAction` 是同一套谓词。

```sh
# 每条查询的命中链（first_match + later_matches）
./out/x86-dump --dump-hits QUERIES > hits.jsonl

# 给每条上游规则找 witness；预算按 opcode 计，0 = 不限
./out/x86-dump --witness C1 --universe universe.jsonl \
    --out coverage.raw.jsonl --budget 20000
```

字段：

```json
{"config":"C1","opcode":"G_ADD","ruleset":"G_ADD","first_match":0,
 "later_matches":[2,10],"query":{...}}
```

```json
{"config":"C1","opcode":"G_ADD","ruleset":"G_ADD","rule_index":14,
 "status":"shadowed","first_hits":0,"match_hits":1,"evaluated":3,
 "candidates":3,"shadowed_by":3,"witness":null,"shadow_query":{...}}
```

- `first_match == -1` 当且仅当答案动作是 `Unsupported`（或显式
  `unanswerable`），这是 U1 的语义门。
- `status`：`witnessed`（当过第一命中）/ `shadowed`（命中但被更早规则遮住，
  带 `shadowed_by`）/ `unexercised`（候选宇宙里从未命中）。
- `candidate` 的顺序由 universe 文件决定；同输入两遍输出字节相同。

回归门（需要已构建的 runner）：

```sh
./scripts/check-hits.py out/x86-dump tests/fixtures/x86-queries.jsonl
./scripts/check-hits.py out/a64-dump tests/fixtures/aarch64-queries.jsonl
./scripts/check-hits.py out/rv-dump  tests/fixtures/riscv-queries.jsonl
```

2026-11-10 实测（`trees/explore` @ `57587b0`，pin `9bfc850b` + 5 patches）：

| target | 查询数 | unanswerable | 有遮蔽命中 | mismatches |
|---|---:|---:|---:|---:|
| X86 | 5,632 | 16 | 854 | 0 |
| AArch64 | 9,591 | 0 | 3,722 | 0 |
| RISC-V | 493 | 0 | 246 | 0 |

答案路径回归：三个 runner 在改 `--dump-hits` 前后，同一查询文件的
`--queries` 输出逐字节相同（X86 `40150c54…`、A64 `e19f14c6…`、
RISC-V `0645f9a4…`）。

## U2/U3 里程碑：独立宇宙 → coverage → U queries

题面不读 `.gisel`：槽位数来自 `--dump-arity`，形状来自 `oracle/src/universe.py`
的固定集合，命中判据是上游 `rule.match`。一条命令出全套产物：

```sh
./scripts/gen-u.sh x86 OUT          # 也可 aarch64 / riscv
```

产物：`OUT/arity.jsonl`、`universe.jsonl`（+ `universe.safe.jsonl`）、
`cov/<config>.main.jsonl`、`cov/<config>.safe.jsonl`、`coverage.json`、
`queries.jsonl`、`FREEZE.json`。流程分六步：

1. `--dump-arity` 取每个 opcode 的类型槽/立即数槽；
2. `python3 -m oracle universe --target T` 造规则无关的候选宇宙；
3. 每个配置跑一遍 `--witness`（主 pass）；
4. 主 pass 里崩溃的 opcode 用 `--profile safe` 重造窄宇宙，再跑一遍（salvage）；
5. `python3 -m oracle coverage --gate --known-gaps oracle/known-gaps/T.json`
   合并两遍并按跨配置口径判门禁；
6. `python3 -m oracle emit` 合成 U 题面：每条被 witness 的规则一条查询
   （带 `ruleset`/`rule` 标注），再给每个 `(config, opcode)` 补固定采样。

细节：

- 崩溃隔离在 runner 内做二分：一条候选把上游断言打崩，就只丢这一条，其余
  继续评估；只有整组重跑仍失败才把 opcode 记为 `asserted`。
- `asserted` 的 opcode 不进入 U 题面（问它只会崩，不会给出可比答案）。
- `queries.jsonl` 同 seed 两遍 SHA 相同。

2026-11-10 实测（pin `9bfc850b` + 5 patches，树 `57587b0`，budget 20000/opcode）：

| target | 配置数 | 规则行 | witnessed | shadowed | unexercised | asserted | U queries |
|---|---:|---:|---:|---:|---:|---:|---:|
| X86 | 4 | 1,911 | 1,798 | 56 | 57 | 0 | 10,100 |
| AArch64 | 9 | 13,240 | 11,727 | 357 | 1,156 | 0 | 29,708 |
| RISC-V | 8 | 4,326 | 3,925 | 156 | 245 | 0 | 20,277 |

按跨配置口径（任一配置 witness 过即不算 gap）：

| target | 任何配置都没命中的规则 | 从未当过第一命中的规则 |
|---|---:|---:|
| X86 | 0 | 6 |
| AArch64 | 88 个 `(opcode, rule_index)` | 同上表内的 shadowed 部分 |
| RISC-V | 28 个 `(opcode, rule_index)` | 同上 |

这些逐条写在 `oracle/known-gaps/<target>.json`（每条 `reason` + `follow_up`）。

未做：U4 端到端对拍与负控、其余 6 个 target（U5）。
