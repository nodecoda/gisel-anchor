# CHANGELOG

本仓库按 cohort 发布（`r1` … `r6`）。每个 cohort 的提交、gate 命令与指纹记在
`gisel-ab/RELEASE.json` 和 `gisel-ab/VERIFICATION.json`，不在这里重复。

## Unreleased

- 目录治理：代码迁到 `src/gisel_anchor/`，脚本统一到 `scripts/`，测试合并到
  `tests/`，`oracle/src/` 与 `oracle/vendor/` 取消；包名命名空间化后
  console script 入口改为 `gisel_anchor.*`。
- 构建产物移出源码树：dump 工具输出到仓库根 `out/`（gitignore）。
- sdist 现在带 `scripts/`、`tests/`、`docs/`、`pin/`，解包后可直接跑测试。
