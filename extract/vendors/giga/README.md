# GigaDevice 厂家处理

批量脚本使用 `--vendor GIGA`；接口上传时传 `vendor=GIGA`。
同时接受 `GigaDevice` 等英文别名，大小写不敏感。

## 本轮修复范围

- 对已经确认的物理引脚表，优先依据完整封装列头或
  `Pin/Ball/Pad Description for ... Package` 表题建立槽位。
  TFBGA、WLCSP 等明确的表内范围不再受无关订购目录的弱标签覆盖。
- 同一完整封装范围的续表合并到同一槽位；球阵列限定词参与范围识别。
  一张共用映射表中的 SOP/USON/WSON 等别名不机械拆成多份相同数据。
- 同时出现 Pin Name 和 Ball Name 时，只有 Ball Name 的多数有效值为球位
  坐标，才将该列识别为编号。正常的 Ball No. + Ball Name 表保持原义。
- `Multiple / NC` 是未列具体球位的汇总说明，不生成名为 Multiple 的引脚。
  有明确编号的 NC、DNU、EPAD 均保留；不根据封装标称脚数补造 NC。
- 不聚合相同编号的不同功能行，不拼接名称或类型；一条原始功能记录的
  编号、名称、类型、描述仍保持对应。

没有文件名、型号、预期数量或缺失编号的硬编码。
没有修改共享提取流程，也没有调用 Elan/ABOV/ST 的厂家专用修复。

## 样本核对情况

云端本轮提供的是统计表中六份异常样本，不包含另外四份原标正确的样本。
修复后仅运行小表单元测试和实际解析表格的片段验证，批量由用户运行。

当前解析中，`0D864A020F7A_4_4_5.pdf` 已有 3 号 EN，不再按旧统计补号。
该 PDF 原文实际为 onsemi NCP161，不是 GigaDevice；其多封装列仍可按明确
列头分别绑定。EPAD 只出现在原文指定的 XDFN4 列，不能转移给别的封装。

`C2940F6D1668B7296_8.pdf` 当前 ST 基线已有八条数据，本轮补的是确定性的
字段识别，避免依赖模型把 Ball Name 猜成编号，或误借 SOP 目录名称。

现有解析结果可复用：

```bash
python batch_extract.py -i pdf_Giga -o outputs_Giga_fixed \
  --parse-output-dir outputs_Giga/_mineru_parse \
  --skip-parse --vendor GIGA --semantic-classify
```
