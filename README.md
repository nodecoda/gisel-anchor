# gisel-anchor

从钉死的 LLVM 树里提取“上游事实”，供 `gisel` 与 `ab` 消费。它不是 gisel 的一部分，
也不生成 `.inc`、不跑对拍、不做归因。

两个模块，产物不同、失败模式不同、验收门不同：

| 模块 | 输入 | 输出 | 证明什么 |
|---|---|---|---|
| `gisel_anchor.importer` | pinned `*LegalizerInfo.cpp` | `.gisel` 语料 | 上游规则被逐条转录进来了 |
| `gisel_anchor.oracle` | pinned LLVM 构建 + 最小 patch | `rules.json`、`coverage.json`、`U` queries | 上游题面没有漏规则 |

数据流（单向；箭头 = 谁产出谁的输入）：

```text
gisel-anchor --(.gisel)------------------> gisel
       |
       +----------------(U queries)----------> ab
```

方向要读对：本项目**生产 gisel 的输入**，不是 gisel 的下游。唯一的代码接触是
测试期调 gisel CLI（`gen` / `lint` / `domain`）验证语料，**不 import `gisel` 内部**；
`gisel` 不得反向依赖本项目。

## 状态

| WP | 内容 | 状态 |
|---|---|---|
| WP0 | 骨架、`SCOPE.md`、`CONTRACTS.md` | 完成 |
| WP1 | `gisel_anchor.importer` 从 gisel 迁入（`import_cxx.py` + 15 个测试文件 + `PIN.json`） | 完成 |
| WP3 | `gisel_anchor.oracle`：dump-rules/arity/hits/witness + 独立题面 + 覆盖，九目标可用 | 完成 |
| WP4 | U 侧对拍与负控（在 `ab`）：九目标 `disagree=0` | 完成 |
| WP5 | 其余 6 个 target 接入 `oracle`（ARM/Mips/PPC/M68k/WebAssembly/SPIR-V） | 完成 |

## 验证

```sh
python3 -m pytest -q tests                   # 需要 ../gisel（或 GISEL_SRC）

python3 scripts/import-cxx.py --all --pin pin/PIN.json \
  --git-repo /home/ubuntu/llvm/llvm-project --check    # 9/9, 0 refused
```

CI（`.github/workflows/ci.yml`）跑同样两条：`pytest -q tests` 不需要
LLVM 构建；`--all --check` 用 `--filter=blob:none --depth 1` 只取 pin 的提交
（约 7 MB，`git show` 按需拉 blob），7 秒内完成九目标逐字节比对。

`--check` 默认读 `../gisel/tests/corpus`（语料是 gisel 的输入）；`GISEL_CORPUS`
可覆盖。测试通过 `conftest.py` 把 `../gisel` 放进 `PYTHONPATH`，供 `-m gisel`
子进程使用。

## 布局

```text
src/gisel_anchor/
  importer/       上游 C++ -> .gisel（`gisel-import`）
  oracle/         上游规则表 -> rules/coverage/U（`gisel-oracle`）
scripts/          checkout 里直接跑的入口：import、oracle 构建、pin 工具
pin/              PIN.json（上游 commit + 每个源 sha256）与 deviation patch
tests/            单一测试树：tests/{importer,oracle,pin}/ + 打包与 issue 契约
docs/             UPSTREAM-ISSUES.json（上游缺陷账本）
out/              构建产物（gitignore，不放进源码树）
CONTRACTS.md      与 gisel / ab 的接口契约（唯一出处）
SCOPE.md          做什么、不做什么
```

`src/` 布局是刻意的：包名 `gisel_anchor` 命名空间化，安装后不会往 site-packages
里丢 `importer` / `oracle` 这种通用顶层名；测试跑的是 `src/` 里的代码，而不是
工作树根目录。所有可执行脚本都在 `scripts/`，包内没有 dash 命名的 `.py`。

## 安装

```sh
pip install .           # 或 pip install git+https://github.com/nodecoda/gisel-anchor
gisel-oracle --help     # U 侧题面：universe / coverage / emit / expand，纯 Python
gisel-import --help     # C++ -> .gisel，需要 LLVM 源码或 checkout
```

两个 console script 对应两个模块，不是两个产品：`oracle` 只需要 Python；
`importer` 读的是上游 C++，所以永远需要一个 LLVM 源码树或 `--git-repo`。

pin 随包走：wheel 里带 `gisel_anchor/importer/data/PIN.json`，与 `pin/PIN.json` 逐字节相同，
由 `scripts/sync-package-data.py` 同步、CI 用 `--check` 挡漂移。因此装上之后
`gisel-import --all --git-repo /path/to/llvm-project` 不需要额外的 `--pin`。
`--pin` 仍可显式指定，显式值优先。

## 硬约束

- 只走 C++ → `.gisel`；不复活 `.td` / table 中间格式。
- 不接 AMDGPU。
- 语料必须逐字节可复现：同一 pin、同一工具版本，`--check` 0 差异。
- 冻结产物只经本项目的 refresh 脚本刷新，不手改 SHA。

## pin 政策：只跟 pin，不自动跟上游 main

`pin/PIN.json` 的 commit 是唯一事实来源。不存在“上游 main 动了就自动重导入”
的 job，这是有意的：语料在无人复核时改变，会让 `ab` 的历史证据失去意义。

pin 的更新是一次**显式迁移**，四步都要做：

1. 改 `commit`（以及 `patches`：上游已合入的 PR 从这里删掉，对应的
   `deviation_*` 条目也从语料里删掉）；
2. `python3 scripts/import-cxx.py --all --pin pin/PIN.json --git-repo … --check`，
   语料 diff 作为评审材料，不自动接受；
3. 刷新冻结产物（`oracle freeze`、`ab/freeze-oracle.sh`）；
4. `ab/replay-frozen.py` 对旧答案回放，确认改动被解释。

CI 里的 `--check` 证明的是“当前语料 == 当前 pin”，不是“当前 pin == 上游最新”。
后者要么人工定期做，要么永远不做；本项目选后者，需要时手工开迁移。
