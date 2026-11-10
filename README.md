# upstream-source

从钉死的 LLVM 树里提取“上游事实”，供 `gisel` 与 `ab` 消费。它不是 gisel 的一部分，
也不生成 `.inc`、不跑对拍、不做归因。

两个模块，产物不同、失败模式不同、验收门不同：

| 模块 | 输入 | 输出 | 证明什么 |
|---|---|---|---|
| `import/` | pinned `*LegalizerInfo.cpp` | `.gisel` 语料 | 上游规则被逐条转录进来了 |
| `oracle/` | pinned LLVM 构建 + 最小 patch | `rules.json`、`coverage.json`、`U` queries | 上游题面没有漏规则 |

依赖方向（单向）：

```text
upstream-source --(.gisel)------------------> gisel
       |
       +----------------(U queries)----------> ab
```

本项目只调 gisel CLI（`gen` / `lint` / `domain`）验证语料，**不 import `gisel` 内部**。
`gisel` 不得反向依赖本项目。

## 状态

| WP | 内容 | 状态 |
|---|---|---|
| WP0 | 骨架、`SCOPE.md`、`CONTRACTS.md` | 完成 |
| WP1 | `import/` 从 gisel 迁入（`import-cxx.py` + 13 个测试文件 + `PIN.json`） | 完成 |
| WP3 | `oracle/`：dump-rules/arity 三目标可用；hits/独立题面/覆盖待做 | 部分完成 |

## 验证

```sh
python3 -m pytest -q import/tests          # 79 passed；需要 ../gisel（或 GISEL_SRC）

python3 import/import-cxx.py --all --pin pin/PIN.json \
  --git-repo /home/ubuntu/llvm/llvm-project --check    # 9/9, 0 refused
```

`--check` 默认读 `../gisel/tests/corpus`（语料是 gisel 的输入）；`GISEL_CORPUS`
可覆盖。测试通过 `conftest.py` 把 `../gisel` 放进 `PYTHONPATH`，供 `-m gisel`
子进程使用。

## 布局

```text
CONTRACTS.md      与 gisel / ab 的接口契约（唯一出处）
SCOPE.md          做什么、不做什么
pin/              PIN.json：上游 commit 与每个源的 sha256（WP1）
import/           上游 C++ -> .gisel（WP1）
oracle/           上游规则表 -> rules/coverage/U（WP3）
```

## 硬约束

- 只走 C++ → `.gisel`；不复活 `.td` / table 中间格式。
- 不接 AMDGPU。
- 语料必须逐字节可复现：同一 pin、同一工具版本，`--check` 0 差异。
- 冻结产物只经本项目的 refresh 脚本刷新，不手改 SHA。
