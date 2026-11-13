# docs

| 文件 | 用途 |
|---|---|
| `UPSTREAM-ISSUES.json` | 上游缺陷账本：每条带 issue 链接、可达性、是否已修。由 `tests/test_upstream_issues.py` 校验 |

契约与范围说明留在仓库根目录，因为它们是这份代码的对外接口，不是文档附录：

- `SCOPE.md` —— 做什么、不做什么（含“不接 AMDGPU”“不复活 .td”）
- `CONTRACTS.md` —— 与 `gisel` / `gisel-ab` 的接口契约（唯一出处）

运行手册见根目录 `README.md`；生成的证据报告不放在本仓库：
U 侧证据在 `gisel-ab/docs/`，语料报告在 `gisel/docs/`。
