# NEXPERIA 厂家适配

批量与命令行使用 `--vendor NEXPERIA`，接口每次上传使用 `vendor=NEXPERIA`。
默认厂家保持原样，不将 NXP 或 ST 别名映射到 NEXPERIA。

## 适配范围

- 只规范表题明确为 `Pinning`、`Pinning information`，且同时具有编号、描述字段的表。
  `Pin` 规范为编号，`Symbol` 规范为名称；图形符号、外形图和脚注列不作为引脚字段。
- 支持只有 `Pin / Description` 的定义表。没有名称或类型来源时不补造字段，
  不将 anode/cathode 等描述冒充名称，也不输出共享层原有的 Reserved 占位名称。
- 表内全宽标题提供明确的型号或封装范围时按范围拆分。
  同一标题内列出的多个型号共用一个映射槽位，不按型号数量复制；不凭编号重置猜分组。
  相同明确范围的续表仍绑定同一槽位。
- 保留具体编号的 not connected 记录，以及 `mb`、`tab`、`ep` 等明确物理端子标识。
  不按文件名、标称引脚数或缺失编号列表补齐数据。
- 对 Symbol 中以分号明确列出的多个名称拆成独立记录，编号相同也不聚合。
  只有 A/K 标识和原描述的阳极/阴极及二极管序号共同匹配，才按描述分句配对；
  否则每个名称保留完整原描述，不猜测分句关系。普通斜杠复合名称不任意拆分。

## 与共享代码的边界

厂家规则在本目录，注册表仅增加 NEXPERIA。
共享 `VendorProfile` 新增默认关闭的 `record_finalizer` 钩子；
`normalize_pin_record` 在原有清洗后仅调用已配置钩子。
只有 NEXPERIA 配置此钩子，用原始字段判断缺省名称，其他厂家仍保持原有 Reserved 行为。
没有修改其他厂家的代码或字段语义。

## 原统计的两个边界问题

- BAS32L 原引脚表只有 1、2 两个引脚，旧统计写 4，不能以旧统计为准补出四个。
- BUK7Y4R4 原表除了 1–4 脚，还有 `mb / D` 安装底座端子，所以定义表应保留五条。
  BAS70XY 的 3、6 脚各有两个名称，输出八条功能记录但只有六个不同编号，这是正常结果。

## 验证与运行

新增 20 项无模型单元测试；包含其他厂家、缺省字段、描述及共享去重回归，共 159 项通过。
另只读取 BAS16H、BC51PA、BUK7Y4R4、BAS70XY 四个已有引脚表片段，
验证了分组、描述、非数字端子和独立名称；表分类及目录模型调用均被断言禁止。
未跑整文件、批量或 MinerU，未覆盖既有输出，完整结果由用户自行运行验证。

复用现有解析结果：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate

python batch_extract.py -i pdf_Nexperia -o outputs_Nexperia_fixed \
  --parse-output-dir outputs_Nexperia/_mineru_parse \
  --skip-parse --vendor NEXPERIA --semantic-classify
```

接口已在运行时，新增厂家后需重启接口进程，以重新加载厂家注册表。
