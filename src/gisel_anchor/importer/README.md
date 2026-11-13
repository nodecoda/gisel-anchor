# import（WP1）

从 pinned 上游 C++ 的 legalizer 构造函数转录 `.gisel`。

迁入清单（来自 gisel）：

- `src/gisel_anchor/importer/import_cxx.py`（3,926 行）
- `src/gisel_anchor/importer/inventory.py`
- `tests/test_import_cxx.py` 及 12 个 importer 行为测试（共 80 个用例）：
  `test_align_lanes`, `test_bitcast_helpers`, `test_clamp_elt`, `test_host_pred`,
  `test_lane_helpers`, `test_memdesc_align`, `test_memdesc_scalar`,
  `test_mutation_forms`, `test_mutation_if`, `test_nested_set`, `test_pred_if`,
  `test_widen_multiple`
- `tests/corpus/PIN.json` → `pin/PIN.json`

测试：`python3 -m pytest -q tests/importer`。

验收：`import --all --check` 9/9 逐字节相同，0 refused。

路径说明：`import-cxx.py` 通过 `GISEL_SRC`（默认 `../gisel`）找到 gisel，
用稳定命令 `gisel gen --emit=core` 做语料校验（不再导入 `gisel.ast`）；
默认语料目录是 `$GISEL_SRC/tests/corpus`，可由 `GISEL_CORPUS` 覆盖。

## import-report.json（分母的机器可读副本）

`--all` 在语料目录写 `import-report.json`（schema 1，`sort_keys`，文件末尾一个换行）。
它是“这次导入到底看见了什么”的完整记录，和 `.gisel` 一起进版本控制：

```json
{"schema": 1, "targets": {"X86": {
  "target": "X86",
  "opcode_source": "lib/Target/X86/GISel/X86LegalizerInfo.cpp",
  "source_sha256": "...", "corpus_name": "X86LegalizerInfo.gisel",
  "corpus_sha256": "...",
  "rulesets": 63, "rules": 328,
  "refused": [{"line": 1, "why": "..."}],
  "skipped": [], "ignored": [], "opaque": [],
  "counts": {"rulesets": 63, "rules": 328, "refused": 0,
             "skipped": 0, "ignored": 0, "opaque": 0},
  "pinned": {"rulesets": 63, "rules": 328}, "drift": false}}}
```

- 生成文件头注释里的 `refusals[:40]` 只是给人看的前 40 条；report **不截断**，
  `counts.refused` 与 `len(refused)` 必须相等。
- `--check` 同时比 `.gisel` 字节与 `import-report.json`；任一不同即 rc=1。
- `corpus_sha256` 是 `.gisel` 文本的 sha256，消费者（`ab/tests/test_import_report.py`）
  用它把分母钉在随仓发布的语料上。
- report 里没有绝对路径：`opcode_source` 是 pin 中的相对路径。
