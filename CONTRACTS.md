# CONTRACTS：upstream-source 对外接口

状态：WP0 草案。所有消费者（gisel、ab）只依赖本文和
`gisel/spec/cases-format.md`，不依赖实现细节。

规范版本：query/answer JSONL = `cases-format v0.1`（下称 **schema v1**）。
canonical 副本在 gisel 仓库 `spec/cases-format.md`；本项目不得单方面扩展字段。

## 1 pin

`pin/PIN.json`：

```json
{
  "schema": 1,
  "repo": "llvm/llvm-project",
  "commit": "<40-hex>",
  "sources": {
    "X86": {"path": "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp",
            "sha256": "<64-hex>"}
  },
  "patches": [
    {"name": "...", "url": "...", "commit": "..."}
  ]
}
```

- `commit` 是纯上游 commit；本地 worktree 的 patch 不能改变导入结果。
- `patches` 列出本项目为对拍在官方侧携带的未合并 PR；语料若要求 post-fix 行为，
  必须同时登记 `deviations`（见 `gisel/tests/corpus/*.gisel` 现状）。
- `sha256` 针对 `sources[*].path` 的原始字节。
- 消费者只读。改动 pin 是一次显式迁移：重跑 `import --all --check` 并更新
  `expected_counts.json`。

## 2 语料（import 输出）

- 格式：`.gisel` 文本，UTF-8，LF，文件末尾一个换行。
- 可复现：同一 pin + 同一 importer 版本，`import --check` 逐字节相同。
- 命名：`<Target>LegalizerInfo.gisel`。
- 不承诺：不承诺覆盖上游全部 opcode；覆盖范围由 importer 的拒绝清单定义，
  拒绝必须 exit 1 并给出 C++ 位置，不允许静默跳过。
- 目标集（当前）：X86、AArch64、RISCV、M68k、WebAssembly、PowerPC、ARM、
  Mips、SPIRV。AMDGPU 不在范围。

## 3 查询（U 输出）

schema v1，一行一个 JSON 对象：

```json
{"config":"x86-64-v3","name":"G_ADD","types":["s32"],"mem":null,"imms":[]}
```

- `config`：非空字符串，必须在 consumer 的 feature 表里存在。
- `name`：opcode 文本（`G_*`）。
- `types`：LLT 文本数组，按 slot 顺序；`p0:*` 只允许作为通配出现在查询里。
- `mem`：`null` 或 `[mty, align]` 或 `[mty, align, ordering, failure]`；
  缺省补 `("NotAtomic","NotAtomic")`。
- `imms`：整数数组；缺省 `[]`。
- 允许附加标注字段（如 `kind` / `rule` / `source`）；consumer 必须忽略。

`U` 的额外要求：

- 候选宇宙不得读取 `.gisel` 规则字面量；gisel 产物只可作为 seed，并标注来源。
- 同一 seed 两遍生成，文件 SHA 必须相同。

## 4 规则清单（oracle 输出）

`rules.json`（草案，WP3 定稿）：

```json
{"schema": 1, "target": "X86", "pin": "<commit>",
 "rules": [{"opcode": "G_ADD", "rule_index": 0, "action": "Legal",
            "has_mutation": false, "alias_of": null}]}
```

- `rule_index` 是 `getActionDefinitions(opcode)` 规则链中的下标。
- `alias_of` 非空表示该 opcode 解析到另一个 ruleset。

## 4.1 命中（oracle 内部产物）

`--dump-hits` 一行一条查询：

```json
{"config":"C1","opcode":"G_ADD","ruleset":"G_ADD","first_match":0,
 "later_matches":[2,10],"query":{"config":"C1","name":"G_ADD",
 "types":["s32"],"mem":null,"imms":[]}}
```

- `ruleset` 是 `getActionDefinitions(opcode)` 解析后的代表 opcode；
  同一个 ruleset 可能被多个 alias opcode 命中。
- `first_match` 是规则链中第一条 `match` 为真的下标，`-1` 表示没有规则命中。
- `later_matches` 是同样命中但输给更早规则的规则（覆盖里的 `shadowed` 原料）。
- `query` 原样嵌入输入行，保证 hits 与答案能按行对齐。
- 语义门：`first_match == -1` 当且仅当 `getAction` 返回 `Unsupported`
  （或 runner 的显式 `unanswerable`）。由 `oracle/scripts/check-hits.py` 校验。

## 4.2 witness（oracle 内部产物）

`--witness CONFIG --universe FILE --out FILE` 一行一条规则：

```json
{"config":"C1","opcode":"G_ADD","ruleset":"G_ADD","rule_index":14,
 "status":"shadowed","first_hits":0,"match_hits":1,"evaluated":3,
 "candidates":3,"shadowed_by":3,"witness":null,"shadow_query":{...}}
```

- `status ∈ {witnessed, shadowed, unexercised}`，定义见 §5。
- `first_hits` / `match_hits`：当过第一命中的次数 / 命中总次数。
- `witness`：第一条把它当第一命中的查询；`shadowed_by` + `shadow_query`：
  遮蔽它的规则下标和第一条证据查询。
- `evaluated` / `candidates`：该 opcode 实际评估数 / 宇宙里的候选总数，
  用于判断“没覆盖”是宇宙不够还是预算不够。
- 候选顺序由 universe 文件决定；同输入两遍输出字节相同。

## 5 覆盖（oracle 输出）

`coverage.json`：

```json
{"schema": 1, "target": "X86", "config": "x86-64-v3",
 "counts": {"rules": 0, "witnessed": 0, "shadowed": 0, "unexercised": 0},
 "rules": [{"opcode": "G_ADD", "rule_index": 0,
            "status": "witnessed", "witness": "<query key or null>"}]}
```

- `status ∈ {witnessed, shadowed, unexercised}`。
- `unexercised` 必须为 0，或在 `known_gaps.json` 有逐条理由。
- `shadowed` 表示规则存在但被前面的规则遮蔽；`shadowed_by` 必须给出遮蔽者，
  `shadow_query` 给出证据查询（来自 §4.2）。

## 6 冻结

`FREEZE.json` 引用：工具 tag/commit、pin commit、patch sha256、各产物 sha256。
由 `verify-freeze.sh` 复现；不接受手工编辑的数字。

## 7 版本演进

- 加字段：先改 gisel `spec/cases-format.md` 并升版本，再改本项目和 ab。
- 改 key 五元组：破坏性变更，三个项目必须同一 release 内对齐。
- 删字段：至少保留一个 release 的读取兼容。
