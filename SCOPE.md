# SCOPE：upstream-source 做什么，不做什么

这份文件是本项目的边界。判断标准只有两条：**读的是不是上游**、
**产物是不是交给别人消费**。

## 1 做

| 功能 | 入口（规划） | 输入 → 输出 |
|---|---|---|
| 语料导入 | `importer/import-cxx.py` | pinned `llvm/lib/Target/*/GISel/*LegalizerInfo.cpp` → `.gisel` |
| 语料校验 | `import --check` | 重新转录并与 checked-in `.gisel` 逐字节比对 |
| 规则清单 | `oracle dump-rules` | pinned 构建 → `rules.json`（opcode/rule/action/mutation/alias） |
| 题面生成 | `oracle gen-queries` | 独立 LLT 宇宙 + 规则覆盖引导 → `U` queries |
| 覆盖报告 | `oracle coverage` | `--dump-hits` 结果 → `coverage.json`（witnessed/shadowed/unexercised） |
| 冻结 | `oracle freeze` | 上述产物 + pin + patch sha → `FREEZE.json` |

## 2 不做

| 不做 | 归属 |
|---|---|
| 生成 `inc` / `check` / `core` | gisel |
| 从 `.gisel` 生成测试题面 | gisel（`cases` / `domain`） |
| 构建 runner、跑查询、比较答案、归因 | ab |
| 修改 `.gisel` 语法或谓词 | gisel，且需项目负责人批准 |
| 向上游提 PR / 社区评审 | 人 |
| AMDGPU 语料 | 明确排除 |

## 3 依赖规则

- 允许：调 gisel CLI 验证语料能被 `gen --emit=core` 接受。
- 禁止：`import` / `oracle` 的运行时 import `gisel.*` 内部模块。
- 禁止：gisel 的 CI 依赖本项目的 LLVM 构建。
- `PIN.json` 由本项目持有；`ab` 只读。

## 4 验收门

| 门 | 条件 |
|---|---|
| G1（import） | `import --all --check`：9/9 逐字节相同，0 refused |
| G3（oracle） | 同一 pin/seed 两遍 SHA 相同；`unexercised=0` 或逐条 allowlist 理由 |
| 隔离 | gisel 产品代码零改动；gisel 测试全绿 |
