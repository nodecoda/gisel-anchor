# 贡献

这个仓库是 `gisel` 的**上游事实源**：它产出 `.gisel` 语料和 U 侧题面，自己不生成
`.inc`、不跑对拍。改动前先读 `SCOPE.md`（边界）和 `CONTRACTS.md`（接口）。

## 本地检查

```sh
python3 -m pytest -q tests          # 需要同级 ../gisel（或 GISEL_SRC）
python3 scripts/sync-package-data.py --check
python3 scripts/import-cxx.py --all --pin pin/PIN.json --git-repo <llvm> --check
```

三条都要绿。第三条要求一个包含 pin commit 的 LLVM checkout；CI 用
`--filter=blob:none --depth 1` 只取那一个提交。

## 硬规则

- **不手改语料**：`.gisel` 只由 `scripts/import-cxx.py` 重写；`--check` 必须逐字节通过。
- **不手改 freeze**：`pin/PIN.json` 的 sha256 只由工具刷新。
- **不做隐式迁移**：pin 的更新是四步显式迁移，写在 `README.md`「pin 政策」。
- **不加通用 C++ 解析器**：`import_cxx` 只认九个构造函数用到的那一小撮写法，别的一律带行号拒绝。
- **不改包名/布局**：`src/gisel_anchor/` 是安装后的命名空间；脚本只放 `scripts/`。
- 语法或谓词扩展要仓库所有者批准，本仓库不定义 DSL。

## 提交

一笔提交做一件事，信息形如 `anchor: name one thing`。改了对外可见的东西
（pin、语料、题面、gate 命令）要同步 `gisel` / `gisel-ab` 并起新 cohort。
