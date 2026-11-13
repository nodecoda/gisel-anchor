# CHANGELOG

本仓库按 cohort 发布（`r1` … `r6`）。每个 cohort 的提交、gate 命令与指纹记在
`gisel-ab/RELEASE.json` 和 `gisel-ab/VERIFICATION.json`，不在这里重复。

## Unreleased — usability (cohort r12)

- 新增统一入口 `gisel-anchor <verb>`（`import` / `oracle`）、`--version` 与
  分组帮助；`gisel-import` / `gisel-oracle` 保留为别名，旧脚本不受影响。
- `--git-repo` 现在接受 URL 与 `file://`：非目录时按 commit 做 blobless
  clone 到 `$GISEL_ANCHOR_CACHE`（默认 `~/.cache/gisel-anchor`），之后
  `git show` 按需取 blob；pin 的 sha256 校验位置不变，缓存命中时不再联网。
- 版本 `0.2.0`。

## Unreleased

- 目录治理：代码迁到 `src/gisel_anchor/`，脚本统一到 `scripts/`，测试合并到
  `tests/`，`oracle/src/` 与 `oracle/vendor/` 取消；包名命名空间化后
  console script 入口改为 `gisel_anchor.*`。
- 构建产物移出源码树：dump 工具输出到仓库根 `out/`（gitignore）。
- sdist 现在带 `scripts/`、`tests/`、`docs/`、`pin/`，解包后可直接跑测试。
