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
./scripts/gen-u.sh x86 OUT   # x86|aarch64|riscv|arm|mips|ppc|m68k|wasm|spirv
```

产物：`OUT/arity.jsonl`、`universe.jsonl`（+ `universe.safe.jsonl`）、
`cov/<config>.main.jsonl`、`cov/<config>.safe.jsonl`、`coverage.json`、
`queries.raw.jsonl`、`queries.jsonl`、`queries.dropped.jsonl`、`FREEZE.json`。
流程分七步：

1. `--dump-arity` 取每个 opcode 的类型槽/立即数槽；
2. `python3 -m oracle universe --target T` 造规则无关的候选宇宙；
3. 每个配置跑一遍 `--witness`（主 pass）；
4. 主 pass 里崩溃的 opcode 用 `--profile safe` 重造窄宇宙，再跑一遍（salvage）；
5. `python3 -m oracle coverage --gate --known-gaps oracle/known-gaps/T.json`
   合并两遍并按跨配置口径判门禁；
6. `python3 -m oracle emit` 合成 U 题面：每条被 witness 的规则一条查询
   （带 `ruleset`/`rule` 标注），再给每个 `(config, opcode)` 补固定采样；
7. 逐 config 过滤上游自己不能作答的查询（runner `--filter-in/out/drop`），
   保留的写 `queries.jsonl`，丢弃的写 `queries.dropped.jsonl` 并带
   `reason ∈ {known-unanswerable, parse-error, asserted}`。契约见
   `CONTRACTS.md` §3.1。

细节：

- 崩溃隔离在 runner 内做二分：一条候选把上游断言打崩，就只丢这一条，其余
  继续评估；只有整组重跑仍失败才把 opcode 记为 `asserted`。
- `asserted` 的 opcode 不进入 U 题面（问它只会崩，不会给出可比答案）。
- `queries.jsonl` 同 seed 两遍 SHA 相同。
- `FREEZE.json` 的 `counts` 同时记录 raw / kept / dropped，三者对不上即失败。

2026-11-10 实测（pin `9bfc850b` + 5 patches，树 `57587b0`，budget 20000/opcode）：

| target | 配置数 | 规则行 | witnessed | shadowed | unexercised | asserted | raw | kept | dropped |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| X86 | 4 | 1,911 | 1,798 | 56 | 57 | 0 | 10,100 | 10,100 | 0 |
| AArch64 | 9 | 13,240 | 12,001 | 231 | 1,008 | 0 | 30,126 | 29,561 | 565 |
| RISC-V | 8 | 4,326 | 4,021 | 156 | 149 | 0 | 20,533 | 20,445 | 88 |
| ARM | 7 | 741 | 741 | 0 | 0 | 0 | 15,639 | 15,639 | 0 |
| Mips | 5 | 765 | 736 | 0 | 29 | 0 | 11,341 | 11,341 | 0 |
| PPC | 1 | 49 | 49 | 0 | 0 | 0 | 2,192 | 2,176 | 16 |
| M68k | 1 | 33 | 33 | 0 | 0 | 0 | 2,180 | 2,180 | 0 |
| WebAssembly | 4 | 922 | 922 | 0 | 0 | 0 | 9,290 | 9,274 | 16 |
| SPIR-V | 6 | 3,766 | 3,228 | 194 | 344 | 0 | 15,588 | 15,580 | 8 |

按跨配置口径（任一配置 witness 过即不算 gap）：

| target | 任何配置都没命中的规则 | 从未当过第一命中的规则 |
|---|---:|---:|
| X86 | 0 | 6 |
| AArch64 | 59 个 `(opcode, rule_index)`（41 unexercised + 18 shadowed） | 同上 |
| RISC-V | 13 个 `(opcode, rule_index)`（全部是被更早规则永久遮住的冗余子句） | 同上 |
| ARM / PPC / M68k / WebAssembly | 0 | 0 |
| Mips | 20 个（4 条是 `.legalIf([](){return false;})` 占位，见 `known-gaps/mips.json`） | 0 |
| SPIR-V | 0 | 194（全部 `cross_config` 命中过） |

这些逐条写在 `oracle/known-gaps/<target>.json`（每条 `reason` + `follow_up`）。

2026-11-11 RISC-V 复核：universe 增加 scalable `nxv*` 形状（含 i1 mask、
pointer vector）、三槽 opcode 的 `slot 2 = p0`、以及 `{s128, s32}` /
`{s128, s64}` 对。原先 28 条 gap 中 16 条是题面太窄，已在 V/Zfa 配置下
witness；剩余 13 条是死规则（Zfa legal 遮蔽 libcall、`{s64,s32}` 遮蔽
`legalFor(D)`、`widenScalarIf` 先改写 f16），逐条写在
`known-gaps/riscv.json`。本轮只重跑了 RISC-V；其他 target 的已冻结 U 题面
仍可复放，但用新 universe 重新生成时形状集合会变大。

2026-11-11 AArch64 复核：两处题面修正。其一，2400 个候选的上限
（`gen-u.sh` 里 `LIMIT_DEFAULT=2400`）：1200 时新形状把 24 条已 witness 的旧
规则挤出了预算，提到 2400 后全部回来。其二，原子 ordering 轴：AArch64 的
s128 load/store 与窄标量 store 按 ordering 分支，而通用 mem 扫描只给
NotAtomic；`candidates()` 现在为 AArch64 的 pointer 形状追加
Monotonic/Acquire/Release/AcquireRelease/SequentiallyConsistent。结果
witnessed 11,727 → 12,001，跨配置 gap 88 → 59。6000 候选探针只多恢复 4 条
`G_EXTRACT_VECTOR_ELT` 退化形状，代价约 2.5 倍状态数，没有采用，写进了
`known-gaps/aarch64.json` 的 reason。ordering 轴目前只对 AArch64 生效，其他
target 等各自重跑 U 时再决定是否打开。

## U4 里程碑：U 侧端到端对拍与负控

题面交给 `ab/run-u-end-to-end.sh`：官方 runner 与 include 后的生成 runner 读
同一份 JSONL。2026-11-10 实测，AArch64 / RISC-V 两行于 2026-11-11 随新题面
刷新（同上 pin/树，证据与 SHA 见 `ab/docs/EVIDENCE-U.md`）：

| target | queries | disagree | unanswerable | 两侧答案文件 |
|---|---:|---:|---:|---|
| X86 | 10,100 | 0 | 0 | 逐字节相同 |
| AArch64 | 29,561 | 0 | 0 | 逐字节相同 |
| RISC-V | 20,445 | 0 | 0 | 逐字节相同 |
| ARM | 15,639 | 0 | 0 | 逐字节相同 |
| Mips | 11,341 | 0 | 0 | 逐字节相同 |
| PPC | 2,176 | 0 | 0 | 逐字节相同 |
| M68k | 2,180 | 0 | 0 | 逐字节相同 |
| WebAssembly | 9,274 | 0 | 0 | 逐字节相同 |
| SPIR-V | 15,580 | 0 | 0 | 逐字节相同 |

负控把某个 opcode 的整条 builder 链换成 `.unsupported()`（单条规则的身份在
builder 链里已经不存在），三 target 的差异全部落在被注入的 ruleset 内，
`unknown_keys=0`、`outside_expected` 为空。

这一轮还抓到一处真实的生成语义不等价：AArch64 `G_ICMP (v2s32, p0:32)` 与
`G_FCMP (nxv4f32, s8)` 各 9 个配置，官方答 `Custom` / `Unsupported`，生成的
inc 在 `coreMinScalarSameAs` 里先跑用户谓词、对 `p0` / `s8` 调
`getElementType()` 而断言。上游 `minScalarEltSameAsIf` 是先比位宽、再调谓词，
两个形状位宽相等所以上游短路。修在 core shim（`trees/explore` @ `700e551`），
`.gisel`、语料、emitter 都没动，inc SHA 不变。修后 AArch64 29,359 → 0 差异。

U5 把同一套流程接到 ARM / Mips / PPC / M68k / WebAssembly / SPIR-V（AMDGPU 不接）。
负控：ARM `G_LOAD` 148、Mips `G_CTPOP` 55、PPC `G_SUB` 12、M68k `G_MUL` 35、
WebAssembly `G_SELECT` 48、SPIR-V `G_UMIN` 798，全部 `unknown_keys=0`、
`outside_expected` 为空。数字与 SHA 见 `ab/docs/EVIDENCE-U.md`。
