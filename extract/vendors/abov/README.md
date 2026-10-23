# ABOV extraction profile

Select `--vendor ABOV` in the CLI/batch script, or form field `vendor=ABOV`
in the existing API. TI remains the default; ST rules are not imported here.

## Supported source structures

- Shared pin tables with multiple physical package columns. Repeated package
  bases carrying distinct `(1)` / `(2)` pinout variants remain separate slots.
- Each source function row retains its own pin number, name, type and
  description. Repeated numbers do not cause ABOV to combine rows or join
  names/types. Shared exact-record deduplication remains unchanged.
- Explicit ascending/descending numeric ranges, `to`, and Unicode dashes.
  Parallel signal ranges expand only when their lengths agree with pin ranges.
- Wrapped numeric digits, only when both outer sequence anchors confirm a
  complete consecutive run. Explicit parallel signal lines prevent joining.
- Parenthesized alternative number mappings, only when the complete table
  proves two distinct, equal-coverage bijections. Both mappings are retained;
  generic labels distinguish them if package names are unavailable locally.

The display-driver table using primary and parenthesized package numbers
contains two mappings even when a test filename lists just one pin count.
Do not discard one mapping or synthesize pins to match filename expectations.

No rule depends on filenames, model names, expected counts or missing-pin lists.
Uncertain digit joins and range-to-name correspondences are not guessed.
Tests use small in-memory tables without MinerU, LLM or network calls.

## 换行编号修复补充（2026-10-23）

旧规则按相邻功能行计算编号序列；rowspan 展开后，一个物理引脚的多条功能行
会重复编号，因此阻断换行数字校验。表头或表尾缺少一侧锚点时也无法修复。

本次只修改 ABOV 的 `_join_wrapped_numbers`：

- 按连续相同的全部封装编号轴分组，用物理引脚组检查双侧递增或递减序列。
  分组只用于校验，不删除、聚合或拼接功能记录。
- 缺少双侧锚点时，要求其他封装列提供至少两个邻近单值映射，支持同一偏移，
  其中至少一个必须紧邻当前组；映射矛盾或只重复同一引脚的功能行均不成立。
- 并列名称、明确名称范围、已被另一组占用的编号及证据不足的断行不强行合并。
- 所有修改仅作用于 ABOV，未改共享主流程或其他厂家规则。

验证包括 11 项新增回归测试，以及其他厂家和共享逻辑回归，共 115 项通过。
另外从一份原 PDF 第 24 页读取三个邻近片段的 19 条功能行，模拟现有结果所反映
的三处数字断行；修复后七个封装列的编号、名称、类型逐项与原表一致。
未找到当时的 MinerU 中间文件，因此这是原表对照和故障重建验证，
不是对原始解析文件的重放，也没有运行真实整文件、批量解析或模型调用。

旧机器存在手工复制但尚未入库的其他厂家文件与共享文件改动，本次只提交
ABOV 规则、ABOV 测试和本说明，不将其他未提交改动混入修复提交。
