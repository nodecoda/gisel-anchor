# import（WP1）

从 pinned 上游 C++ 的 legalizer 构造函数转录 `.gisel`。

迁入清单（来自 gisel）：

- `tools/import-cxx.py`（3,926 行）
- `tools/import-inventory.py`
- `tests/test_import_cxx.py` 及 12 个 importer 行为测试（共 79 个用例）：
  `test_align_lanes`, `test_bitcast_helpers`, `test_clamp_elt`, `test_host_pred`,
  `test_lane_helpers`, `test_memdesc_align`, `test_memdesc_scalar`,
  `test_mutation_forms`, `test_mutation_if`, `test_nested_set`, `test_pred_if`,
  `test_widen_multiple`
- `tests/corpus/PIN.json` → `pin/PIN.json`

测试：`python3 -m pytest -q import/tests`（79 passed）。

验收：`import --all --check` 9/9 逐字节相同，0 refused。

路径说明：`import-cxx.py` 用 `GISEL_SRC`（默认 `../gisel`）导入 `gisel.ast`
做语料解析校验；默认语料目录是 `$GISEL_SRC/tests/corpus`，可由 `GISEL_CORPUS`
覆盖。
