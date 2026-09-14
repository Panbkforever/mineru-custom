# MinerU Custom TI 引脚/封装抽取项目交接文档

本文档记录当前项目的目标背景、整体流程、核心处理思想、关键模块、主要算法、输入输出数据、运行方式、调试方式、已解决问题和未解决问题。它面向后续继续维护该项目的人，重点不是复述源码，而是说明为什么代码现在这样设计、哪些边界不能破坏、出现问题时应该从哪里排查。

本文档编写时的业务代码基线：

- 本地仓库：`/Users/panbaokun/Documents/Codex/2026-06-05/autodl/qfp-package-fix-20260804`
- AutoDL 仓库：`/root/autodl-tmp`
- GitHub：`git@github.com:Panbkforever/mineru-custom.git`
- 同步提交：`d07b1d540e3b28ed556e83e5562272461c53b3ef`

## 1. 项目目标和背景

本项目基于 MinerU 做二次开发，目标是从 TI 等半导体 datasheet PDF 中自动解析并抽取“物理引脚/球号/端子编号、引脚名称、类型、封装信息”，最终生成结构化 JSON。

原始 MinerU 的能力偏通用文档解析：它可以把 PDF 转成 Markdown、HTML 表格、中间 JSON、模型 JSON 等。但 TI datasheet 的引脚表存在大量工程化难点：

- 一个 datasheet 可能包含一个或多个物理封装；
- 多封装可能共用同一张表，也可能拆成多张表；
- 包装信息表、订购信息表、器件族对比表、引脚说明表、复用矩阵表经常混在一起；
- 表头经常有 rowspan/colspan，多层父子表头决定列语义；
- 引脚编号可能是 `A1, A2`、`1-5`、`A1-A5`、`L[7:12]` 等多种形式；
- 有些表有 pin number 但不是物理 pin list，例如寄存器表里的 `PIN AFFECTED`；
- 有些表是 board-level 连接建议，例如 `Connections for Unused Pins`，不应进入正式 pin list；
- 一些 MinerU 表格解析错误会发生在最早的表格 HTML 生成阶段，例如超长单元格错列、漏列、OCR 字符污染。

所以本项目的核心目标不是“把所有看起来像引脚的文本都抽出来”，而是：

1. 只抽取真实的物理引脚/球号/端子记录；
2. 正确判断文档有几个独立物理封装槽位；
3. 正确把每张目标引脚表、每个表内分支绑定到对应封装；
4. 尽量避免把说明表、订购表、特性表、寄存器表误当成 pin list；
5. 在模型参与时，只允许模型判断结构，不允许模型生成最终 pin 记录或改写封装值；
6. 保留可调试的中间结果，便于定位错误到底来自 MinerU、后处理、字段模型、封装模型还是最终行抽取。

## 2. 总体设计原则

### 2.1 两阶段模型，确定性代码负责最终值

项目中有两次可选的大模型调用：

1. 第一次模型调用：判断候选表是否是可抽取的物理引脚表，并映射 `pin_no / pin_name / type` 列。
2. 第二次模型调用：判断候选封装总述表的结构，只返回表角色、表头行、列角色。

模型不能做以下事情：

- 不能输出最终 pin JSON；
- 不能生成或改写 pin_no、pin_name、type；
- 不能生成或改写公开 `pkg` 名；
- 不能决定某个数据行应该属于哪个封装；
- 不能把多张表的结果合并；
- 不能把 description 或普通正文当成封装绑定依据。

模型只做结构判断。真正的值必须由确定性代码从原始表格行中读取。

这个约束非常重要。项目之前大量问题都来自“模型看到了像封装的词，于是多返回/误返回了一些东西”，例如把 `Family Members` 器件族对比表误判成封装目录，导致封装槽位膨胀。

### 2.2 先判断，后提取

整个抽取链路必须遵守：

```text
候选表收集
  -> 表头/结构分析
  -> 特殊表直通过滤
  -> 字段模型或规则判断
  -> 表内多封装分析
  -> 文档级封装目录解析
  -> 目标表绑定封装槽位
  -> 逐行生成 pin 记录
  -> 最终清理和汇总
```

不能在字段判断之前生成 pin 记录，也不能在封装槽位冻结之前随意创建新 pkg。

### 2.3 文件名不能参与业务判断

测试 PDF 文件名常带有期望 pin 数量，例如：

- `AM335x_324_298.pdf`
- `am62a3-AMB_484_484.pdf`
- `CC430F614x、CC430F514x、CC430F512x_64_48.pdf`

这些文件名只用于人工测试和结果核对，不能参与抽取逻辑。代码里的封装判断必须来自 PDF 表格、表题、章节标题、封装目录、表头结构等内容证据。

### 2.4 description 是只读附加字段

`description` 只能作为补充说明输出，不能参与：

- 封装数量判断；
- 封装绑定；
- 表内多封装分支判断；
- 行是否属于某个封装的判断。

这是为了避免描述文本中出现 package、pin、ball 等词时污染结构判断。

### 2.5 不按 pin_no 全局去重

不同封装中可以有相同 `pin_no`；同一封装内也可能有同一编号在不同上下文中被补充描述。当前策略是：

- 不跨 pkg 去重；
- 最终只在同一 pkg 内按 `pin_no + pin_name` 精确去重；
- 去重时保留第一次出现的 type；
- description 可按首次出现顺序合并。

## 3. 输入、输出和目录结构

### 3.1 常用输入目录

本地测试 PDF 主要位于：

```text
/Users/panbaokun/Documents/PDF解析/测试pdf/
  多封装公用表格/
  多封装多张表/
  多封装混合表/
  TI测试PDF/
```

AutoDL 常用输入目录：

```text
/root/autodl-tmp/Multi_package_TIpdf/
```

### 3.2 解析输出

`batch_extract.py` 默认输出：

```text
ex_outputs/
  <pdf_stem>.json              # 最终 pin/package JSON
  <pdf_stem>_info.json         # 单文件汇总信息
  <pdf_stem>_debug.json        # 表级判断和绑定调试信息
  extraction_summary.json      # 批量汇总
  _logs/
    <pdf_stem>.log             # 单 PDF 完整运行日志
  _llm_locks/
    slot-0.lock                # 跨进程 LLM 并发锁文件
  _mineru_parse/
    <pdf_stem>/
      hybrid_auto/
        <pdf_stem>.md
        <pdf_stem>.json
        <pdf_stem>_middle.json
        <pdf_stem>_model.json
        <pdf_stem>_layout.pdf
        <pdf_stem>_content_list.json
        <pdf_stem>_content_list_v2.json
```

关键文件说明：

- `*_model.json`：MinerU 模型原始输出。表格 HTML 如果在这里已经错，说明问题在 MinerU 表格识别阶段。
- `*_middle.json`：MinerU 中间结构，本项目的页眉页脚过滤、MIN/MAX 修复、单元格换行恢复会写回此文件。
- `<pdf_stem>.md`：经过 MinerU 和本项目 Markdown 后处理后的最终 Markdown。
- `<pdf_stem>.json`：与最终 Markdown 对应的结构化副本，包含 `markdown` 字段；后续抽取优先读取它。
- `ex_outputs/<pdf_stem>.json`：最终业务输出，外层为 package 列表。
- `ex_outputs/<pdf_stem>_debug.json`：排查时最重要，记录每张表的状态、字段决策、封装计划、绑定结果等。

## 4. 运行方式

所有 AutoDL 运行命令都必须先进入项目目录并激活项目环境。不要绕过
`MinerU/.venv` 直接使用 `/root/miniconda3/bin/python` 启动解析、批处理或接口，
否则 `extract.py -> parse_doc.py -> MinerU` 会继承错误的 Python 环境。

标准前置命令：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODEL
```

### 4.1 单文件完整解析和抽取

```bash
python extract.py Multi_package_TIpdf/AM335x_324_298.pdf \
  -o ex_outputs/_mineru_parse \
  --extract-output ex_outputs/AM335x_324_298.json \
  --summary-output ex_outputs/AM335x_324_298_info.json \
  --debug-output ex_outputs/AM335x_324_298_debug.json \
  --semantic-classify
```

`extract.py` 默认会先调用 `parse_doc.py`，再做 pin/package 抽取。只有显式加 `--skip-parse` 才会复用已有解析结果。当前项目标准流程不依赖 `--skip-parse`。

### 4.2 批量串行

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODEL
python batch_extract.py --semantic-classify
```

默认：

```text
--workers 1
--llm-workers 1
```

这仍是一个 PDF 完整跑完后再跑下一个 PDF。与旧串行行为基本一致，只是现在会产生单文件日志。

### 4.3 批量多并发

推荐 AutoDL 单卡先从：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODEL
python batch_extract.py \
  -i Multi_package_TIpdf \
  -o ex_outputs \
  --workers 2 \
  --llm-workers 1 \
  --semantic-classify \
  --continue-on-error
```

含义：

- `--workers 2`：最多两个 PDF 同时完整执行 parse + extract；
- `--llm-workers 1`：所有 PDF 子进程共享同一个 DeepSeek 请求并发额度；
- `--continue-on-error`：某个 PDF 失败后继续处理后续文件；
- 每个 PDF 的完整 stdout/stderr 写到 `ex_outputs/_logs/<pdf_stem>.log`。

如果直接把 `--workers` 开大，而不限制 `--llm-workers`，实际 API 并发会被文档级并发和表级模型并发叠加放大，容易造成超时、限流或结果不稳定。

如果有多个 DeepSeek API key，可以用 `DEEPSEEK_API_KEYS` 配置多个 key，并把
`--llm-workers` 调到相同数量。例如两个 key：

```bash
export DEEPSEEK_API_KEYS="key_1,key_2"
python batch_extract.py \
  -i Multi_package_TIpdf \
  -o ex_outputs \
  --workers 2 \
  --llm-workers 2 \
  --semantic-classify \
  --continue-on-error
```

LLM 并发 slot 会按顺序选择 key：slot 0 使用 `key_1`，slot 1 使用 `key_2`。
如果两个 API 的 base URL 或模型也不同，可以同时配置：

```bash
export DEEPSEEK_BASE_URLS="https://api.deepseek.com,https://api.deepseek.com"
export DEEPSEEK_MODELS="deepseek-v4-flash,deepseek-v4-flash"
```

### 4.4 接口层并发

`extract_api.py` 是 PDF -> pin/package JSON 的 Flask 接口。它和 CLI 批处理使用同一套 `extract.py` 子进程链路。

当前接口采用“一个 HTTP 请求上传多个 PDF，返回一个 zip 包”的批量模式：

```bash
curl -X POST http://127.0.0.1:5002/api/extract-pdf-json-batch \
  -F "files=@/root/autodl-tmp/pdfs/a.pdf" \
  -F "files=@/root/autodl-tmp/pdfs/b.pdf" \
  -o extract_results.zip
```

返回的 zip 中包含：

- 每个成功 PDF 对应的 `<stem>.json`；
- `_batch_report.json`，记录每个输入文件的成功、失败、跳过原因。

原单文件接口 `/api/extract-pdf-json` 已从 Flask 路由中注释掉，代码仅保留作回滚参考。

服务级并发配置：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODEL
export EXTRACT_API_WORKERS=2
export EXTRACT_API_LLM_WORKERS=1
python extract_api.py
```

如果接口下有两个 DeepSeek API key，可以这样启动 LLM 两路并发：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
echo $DEEPSEEK_MODEL
export DEEPSEEK_API_KEYS="key_1,key_2"
export EXTRACT_API_WORKERS=2
export EXTRACT_API_LLM_WORKERS=2
python extract_api.py
```

含义：

- `EXTRACT_API_WORKERS`：接口服务同时允许几个完整 parse + extract 任务进入执行；
- `EXTRACT_API_LLM_WORKERS`：接口服务下所有 extract 子进程共享的 DeepSeek 请求并发；
- `EXTRACT_API_LOCK_DIR`：接口完整任务锁目录，默认在系统临时目录；
- `EXTRACT_API_LLM_LOCK_DIR`：接口 LLM 锁目录，默认在系统临时目录。

接口层不把 `api_workers/llm_workers` 设计成单次请求参数。原因是并发额度必须在服务进程、批量请求内部任务、多个 HTTP 请求之间共享；如果每个请求各自传不同的并发数，文件锁 slot 数会不一致，限流语义不可靠。

`/health` 会返回当前接口并发配置：

```json
{
  "status": "ok",
  "api_workers": 2,
  "llm_workers": 1
}
```

### 4.5 主要环境变量

DeepSeek 相关：

```bash
export DEEPSEEK_API_KEY=...
export DEEPSEEK_BASE_URL=https://api.deepseek.com
export DEEPSEEK_MODEL=deepseek-v4-flash
export DEEPSEEK_TIMEOUT=90
export DEEPSEEK_MAX_RETRIES=4
export DEEPSEEK_RETRY_BASE_SECONDS=2
```

第一次表字段判断：

```bash
export EXTRACT_SCHEMA_WORKERS=2
export DEEPSEEK_BATCH_MAX_TOKENS=6000
```

第二次封装目录判断：

```bash
export EXTRACT_PACKAGE_WORKERS=4
export EXTRACT_PACKAGE_TIMEOUT=60
export EXTRACT_PACKAGE_BATCH_MAX_TOKENS=7000
```

跨进程 LLM 限流：

```bash
export EXTRACT_LLM_WORKERS=1
export EXTRACT_LLM_LOCK_DIR=ex_outputs/_llm_locks
export EXTRACT_LLM_LOCK_POLL_SECONDS=0.2
```

通常不需要手动设置 `EXTRACT_LLM_WORKERS`，`batch_extract.py --llm-workers` 会自动传入。

MinerU 相关：

```bash
export MINERU_MODEL_SOURCE=modelscope
```

## 5. 总体处理流程

### 5.1 高层流程

```text
PDF
  |
  v
parse_doc.py
  |
  +-- MinerU do_parse()
  |     +-- layout detection
  |     +-- table recognition
  |     +-- OCR / formula / image processing
  |     +-- md / model_json / middle_json / content_list 输出
  |
  +-- middle_json 后处理
  |     +-- 页眉页脚过滤
  |     +-- MIN/MAX 表格坐标修复
  |     +-- 单元格内换行恢复
  |     +-- 重建 Markdown
  |
  +-- Markdown 表格后处理
        +-- 拆分 MinerU 错误合并的逻辑子表
        +-- 展开/修复部分 rowspan/colspan
        +-- 表格 OCR 字符修正
        +-- 生成最终 Markdown JSON
  |
  v
extract.py
  |
  +-- 收集表格候选
  +-- 表头和 span-aware 结构解析
  +-- 特殊表直接过滤/保留
  +-- 第一次模型或规则判断目标字段
  +-- 表内多封装计划
  +-- 第二次模型或 fallback 建立文档级封装目录
  +-- 目标表/表内分支绑定封装槽位
  +-- 按行读取 pin_no / pin_name / type
  +-- description / notes 追加
  +-- 去重和 pkg 后缀
  |
  v
最终 JSON + info + debug
```

### 5.2 MinerU 表格处理机制

当前 AutoDL 实际导入的 MinerU 路径是：

```text
/root/autodl-tmp/MinerU/mineru
```

表格链路在：

- `MinerU/mineru/backend/pipeline/batch_analyze.py`
- `MinerU/mineru/model/table/rec/slanet_plus/main.py`
- `MinerU/mineru/model/table/rec/slanet_plus/matcher.py`
- `MinerU/mineru/model/table/rec/unet_table/main.py`
- `MinerU/mineru/model/table/rec/unet_table/utils_table_recover.py`

MinerU 不是读取 PDF 原生表格对象，而是：

```text
页面图片
  -> 表格区域 crop
  -> 表内 OCR 检测/识别
  -> 表格结构模型预测 cell / rowspan / colspan
  -> OCR text box 根据 bbox/IoU/距离匹配到 cell
  -> 输出 HTML
```

这解释了当前未解决的超长单元格问题：如果一个单元格里有非常长的 pin 列表，OCR 框很多、跨多行，表格结构模型或 OCR-cell 匹配一旦错位，`*_model.json` 中就已经缺列、串列或脏字符。后续抽取只能消费这个 HTML，无法天然知道原 PDF 中另一列被漏了。

## 6. 解析阶段后处理

### 6.1 页眉页脚过滤

入口：

- `parse_doc.py::apply_header_footer_filter`
- `mineru_api_zip.py::apply_header_footer_filter`

实现：

- `modify/Filter_ headers_and_footers.py`

职责：

- 在 `middle_json` 阶段识别页眉、页脚、页码、首页底部声明；
- 将这些 block 移入 `discarded_blocks`；
- 重建 Markdown。

当前重要策略：

- 禁用了 `enable_line_boundary_filter`。
- 原因：通过页面黑色横线推断正文上下边界时，可能把表格自身边框误判为页脚分界线，导致靠近页底的完整表格被删除。
- 当前仍保留明确类型、重复页边文本、页码等过滤规则。

### 6.2 MIN/MAX 极限值表格坐标修复

实现：

- `post_table/min_max_coordinate_correct.py`

目标：

- 修复 `MIN / TYP / NOM / MAX / UNIT` 类表格中因 MinerU 表格识别导致的列错位、重复、合并列问题。

思路：

- 使用 PDF 原始字符坐标辅助判断数值属于哪个列；
- 保留 MinerU/VLM 识别出的文本值；
- 只修正列归属和显式合并结构；
- 对短数值表达式保护空格，避免 `1 . 2` 或单位表达式被 Markdown/HTML 重建破坏。

### 6.3 单元格内换行恢复

实现：

- `post_table/restore_cell_line_breaks.py`

目标：

- 有些 MinerU HTML 会把单元格内多行文本合成一行，导致并行 pin/name 对应关系丢失。
- 该模块尝试根据 PDF 原始字符纵坐标，在 HTML 单元格内恢复真实视觉换行。

当前思路：

- 解析表格 HTML，建立逻辑单元格；
- 将 middle_json bbox 映射到 PDF 坐标；
- 在源 PDF 对应区域提取字符；
- 聚类成视觉文本行；
- 只有完整单元格文本能够匹配到连续多条视觉行时，才插入 `<br>`；
- 不直接根据文本长度猜测换行。

当前问题：

- 该逻辑可以修复缺少换行的问题，但也可能在紧密数字或短 pin 单元格上误插入换行，例如把 `12` 变成 `1<br>2`。
- 曾尝试调整视觉行聚类，但对已知样例没有有效解决，已回退。
- 目前该问题暂时搁置，后续需要更严格的“单元格级 bbox + 字符连续性 + 行内 token 形态”判断。

### 6.4 Markdown 表格后处理

入口：

- `parse_doc.py::apply_post_table_correction`

顺序：

1. `post_table/split_merged_tables.py`
2. `post_table/fix_ocr_table.py`
3. `parse_doc.py::write_final_markdown_json`

为什么必须先拆表：

- 有些 MinerU 会把多个逻辑子表合并成一个 `<table>`；
- 拆分依据是“全宽小节行 + 完整重复表头”；
- 如果先展开 rowspan/colspan，原始 colspan 证据会丢失，后面更难判断拆分边界。

`fix_ocr_table.py` 的主要职责：

- 修复表格 OCR 字符混淆；
- 处理 terminal functions 特殊表；
- 修复跨页合并的 pin 行；
- 针对部分固定形态进行 HTML 归一化。

## 7. 表格候选和表头结构

### 7.1 表格候选来源

实现：

- `extract/pin_package_extractor.py::iter_table_candidates`
- `extract/pin_package_extractor.py::iter_table_candidates_from_markdown`

优先读取最终 Markdown JSON：

- 如果 `<pdf_stem>.json` 中存在 `markdown` 字段并包含 `<table>`，优先从这个最终后处理结果读取；
- 如果没有最终 Markdown JSON，才回退到 `*_middle.json`。

这样保证抽取使用的是后处理后的表格，而不是 MinerU 原始 HTML。

表题来源：

- 使用表格前的局部文本窗口；
- 结合章节标题上下文；
- 对宽泛 `Pin Functions` 这类表，允许读取最近的 Figure/Package 图题作为额外局部上下文；
- 但不能让很早之前的旧章节标题无限传播到后续表。

### 7.2 页码恢复

最终 Markdown JSON 自身没有页码。为了支持“目录前后/末十页”这类封装目录候选规则，代码会：

- 读取同目录的 `*_middle.json`；
- 使用 `table_exporter.match_final_tables_to_pages` 将最终表格按内容相似度映射回 middle_json 表格；
- 为最终候选补回 `page_idx`。

如果页码恢复失败：

- 不影响基本抽取；
- 但封装目录候选会退化为只接受明确表题命中的候选，不再用旧的“前后百分比”猜测。

### 7.3 span-aware 表头解析

实现：

- `extract/table_header_structure.py`

核心能力：

- 解析 HTML 中的 rowspan/colspan；
- 展开为二维表格；
- 构建每列完整父子表头路径；
- 识别多层表头中 `pin_name` 分支；
- 区分：
  - 等价名称列；
  - package 分支名称列；
  - parallel operating mode 名称列。

这个模块是表内多封装的基础。不能只看扁平 headers，否则会把父表头信息丢掉。

## 8. 第一次模型调用：目标引脚表和字段判断

实现：

- `extract/pin_package_extractor.py::decide_all_tables`
- `extract/semantic_classifier.py::classify_table_schema_batch`

输入：

- 表题；
- 扁平 headers；
- span-aware header_paths；
- name_layout；
- 数据行采样；
- 每张表独立 request_id。

输出：

```json
{
  "should_extract": true,
  "columns": [
    {"column_index": 0, "field": "pin_no"},
    {"column_index": 1, "field": "pin_name"},
    {"column_index": 2, "field": "type"}
  ]
}
```

允许字段只有：

- `pin_no`
- `pin_name`
- `type`

不允许模型输出：

- package；
- group；
- description；
- confidence；
- reason；
- 最终 pin 记录。

### 8.1 数据采样策略

实现：

- `extract/pin_package_extractor.py::build_semantic_table_sample`

策略：

- 数据行 `<= 30`：完整发送；
- 数据行 `31-200`：保留前 8 行、中间连续 4 行、最后 8 行；
- 数据行 `> 200`：保留前 6 行、中间均匀取 8 行、最后 6 行；
- 所有表头行完整保留，不采样。

采样只用于模型判断，后续提取仍使用完整表格数据。

### 8.2 批量协议

- 每个请求最多四张表；
- 每张表必须有独立 `request_id`；
- 模型返回必须按 `request_id` 关联；
- 批次失败时降级为逐表重试；
- 单表重试失败只影响当前表，不能影响同批次其他表。

## 9. 特殊表处理

实现：

- `extract/special_table_handlers.py`

特殊表在第一次模型调用前处理。命中后直接返回确定性结果，不送模型。

当前特殊规则：

1. `unused_pins_connection_table_filter`
   - 过滤 `Connections for Unused Pins` / `Connection for Unused Pins and Modules`。
   - 这类表是板级连接建议，不是完整物理 pin list。

2. `mii_rmii_rgmii_pin_mux_table_filter`
   - 过滤固定六列表头的 MII/RMII/RGMII 复用矩阵。
   - 避免把模式矩阵误当成 pin list。

3. `supplemental_characteristics_table_filter`
   - 过滤 `Multiplexing Characteristics`、`Power Supplies Description` 等补充说明表。

4. `register_word_pin_affected_table_filter`
   - 过滤寄存器 Word 位字段表中的 `PIN AFFECTED` 辅助引用列。

5. `reserved_pin_table_handler`
   - 识别 Reserved/NC 引脚表；
   - 只保留明确要求悬空或禁止连接的真实 Reserved 行；
   - 明确说明某封装中不存在的位置不输出。

特殊规则必须严格。不能因为某表“看起来像”就扩大匹配范围，否则会误伤真实 pin table。

## 10. 表内多封装分析

实现：

- `extract/multi_package_extractor.py`

它位于字段判断之后、逐行抽取之前，只回答两个问题：

1. 当前表是否包含多个封装的物理引脚映射；
2. 如果是，每个封装应该读取哪一列 pin_no、哪一列 pin_name/type，以及哪些数据行。

它不做：

- 表是否要抽取的判断；
- 模型调用；
- 最终 JSON 生成；
- pin_no/pin_name 清洗；
- 文档级封装目录解析。

当前支持的表内多封装模式：

1. `package_columns`
   - 多个封装各有独立 pin_no 列；
   - 共享 pin_name/type。

2. `package_name_columns`
   - 共享一个 pin_no/type；
   - 多个封装分支各有独立 pin_name 列。

3. `parallel_name_columns`
   - 共享一个 pin_no/type；
   - 多个运行模式分支各有独立 pin_name 列；
   - 这不是多物理封装，不增加文档级 pkg 数量。

4. `package_rows`
   - 表内存在 package 控制列；
   - 不同行属于不同封装。

5. `package_sections`
   - 表内用 `XXX Package` 分段行切换当前封装。

横向重复的 `Pin# | Pin Name | Type` 字段块必须由本模块绑定为独立封装分支，不能落回普通单表路径后把多个 pin_name 合并。

## 11. 第二次模型调用：文档级封装目录

实现：

- `extract/package_catalog_resolver.py`
- `extract/semantic_classifier.py::classify_package_catalog_tables`

### 11.1 候选召回

封装目录候选来自全文表格，但已经由第一次模型确认的引脚表不能再作为封装目录候选送入第二次模型。

候选规则：

- 当前表题明确命中：
  - `Device Information`
  - `Package Information`
  - `Packaging Information`
  - `Ordering Information`
  - `器件信息`
  - `封装信息`
  - `包装信息`
  - `订购信息`
- 或位于：
  - 目录开始前；
  - 目录结束后三页内；
  - 无目录时前十页；
  - 文档最后十页。

额外过滤：

- `Family Members` 表直接排除，不送入第二次模型。

原因：

- `Family Members` 通常是器件族功能/资源对比表，可能带 package 列；
- 模型容易把每个 family member 行误判成独立封装槽；
- 曾导致 CC430 类文档被错误解析成 `default` 或封装数量膨胀。

### 11.2 模型协议

第二次模型只返回结构：

```json
{
  "is_package_summary": true,
  "table_role": "identity_summary",
  "header_row_index": 0,
  "columns": [
    {"column_index": 0, "role": "package_identity"},
    {"column_index": 1, "role": "package_type"},
    {"column_index": 2, "role": "package_drawing"},
    {"column_index": 3, "role": "pin_count"}
  ]
}
```

允许角色：

- `package_identity`
- `package_type`
- `package_drawing`
- `pin_count`
- `orderable_sku`
- `ignore`

模型不能返回实际封装名或单元格值。代码根据模型给出的列索引，从原始 `table.rows` 中读取值。

### 11.3 fallback

当第二次模型失败、超时、返回空结果或没有建立 entries 时，会进入确定性 fallback：

- 从高优先级标题表中识别标准封装目录；
- 从 `Packaging Information` 类表中读取 package type/drawing/pin count；
- 结合 target tables 的上下文做安全收敛；
- 如果没有真实多封装证据且所有候选无法绑定，允许按单封装误膨胀兜底收敛为一个槽位。

注意：

- fallback 不是“随便用文件名”；
- 也不是“目标表有几张就建几个 pkg”；
- 没有唯一证据时必须 unresolved，不能默认塞进第一个封装。

### 11.4 封装槽位冻结

一旦 entries 建立，会冻结为：

```text
slot:0
slot:1
slot:2
...
```

冻结后：

- 后续绑定只能选择已有 slot；
- 未匹配表不能创建新 pkg；
- 多封装表的全部本地分支必须一次性做一对一绑定；
- 两个槽位即使公开封装名相同，也要保留独立槽位，最终输出再追加数字后缀，例如 `FCCSP1`、`FCCSP2`。

## 12. 跨表多封装结构

实现：

- `extract/multi_PkgTab_extractor.py`

处理场景：

- 一个 PDF 中多个封装分别位于多张单分支引脚表；
- 表题、表头或章节标题中有 package/drawing/identity 证据；
- 需要判断这些表是否分别对应不同物理封装。

证据优先级：

1. 表题；
2. 表头；
3. 最近章节标题。

重要规则：

- Drawing，例如 `RGZ`、`RKP`，优先于器件身份；
- 同一器件不同 drawing 必须保持不同分支；
- 没有 `Package/封装` 字样时，已由第二次模型确认的 identity/drawing 仍可作为严格匹配证据；
- 没有唯一证据的表不创建分支，也不默认绑定第一个 pkg；
- `parallel_name_columns` 只是同一封装多个运行模式名称，不创建 pkg 分支。

## 13. 行抽取和 pin 拆分

实现：

- `extract/pin_package_extractor.py::extract_records_from_row`
- `extract/pin_package_extractor.py::extract_records_from_bound_package_row`
- `extract/pin_package_extractor.py::split_pin_numbers`
- `extract/parallel_cell_splitter.py`

### 13.1 pin_no 拆分

支持：

- 显式列表：`A1, A2, A3`
- 空格/中文顿号/分号/斜杠/竖线分隔；
- 数字范围：`1-5`
- 同前缀范围：`A1-A5`
- BGA 方括号范围：`L[7:12]`
- `N/A` 作为完整占位值保留，不按 `/` 拆开。

防护：

- 范围展开最多 1000，避免异常文本触发超大展开；
- 前后缀不一致的范围不展开，例如 `A1-C3` 保留原值；
- 端点带前导零时保留宽度。

### 13.2 pin_name 并行拆分

如果同一行 `pin_no` 拆出多个编号，`pin_name` 只有在结构上能和编号数量完全对应时才按位置拆分。

如果数量不一致：

- 保留原始 pin_name；
- 不强行拆分，避免把一个完整功能描述错误拆成多个名称。

### 13.3 空名称处理

最终清洗：

- `pin_no` 去 HTML/Markdown 标记和首尾空白；
- `pin_name` 去尾部脚注；
- 空 pin_name 统一为 `Reserved`。

## 14. 结果结构

最终 JSON 外层是 package 列表：

```json
[
  {
    "pkg": "VQFN1",
    "group_list": [
      {
        "group": "Table 5-1. Pin Attributes",
        "pin_list": [
          {
            "pin_no": "A1",
            "pin_name": "VSS",
            "type": "GND"
          }
        ]
      }
    ]
  }
]
```

说明：

- `pkg` 是公开封装名；
- `group` 通常来自表题或章节/图题上下文；
- `pin_list` 是实际记录；
- 文档级 `notes` 会作为单独对象追加到最终 JSON 数组末尾，不参与 package 数量统计；
- `_debug` 字段只在内部调试对象中存在，最终公开 JSON 会剥离。

## 15. 批量并发设计

当前已实现文档级并发：

- `batch_extract.py --workers N`

工作方式：

- 主进程读取输入目录 PDF；
- 每个 PDF 启动一个独立 `extract.py` 子进程；
- 每个子进程完整执行 parse + extract；
- 主进程等待 futures 完成；
- 每个 PDF 单独写日志；
- 主进程最后统一写 `extraction_summary.json`。

当前已实现跨进程 LLM 限流：

- `batch_extract.py --llm-workers N`
- `extract/semantic_classifier.py::llm_request_slot`

工作方式：

- `batch_extract.py` 为每个子进程注入：
  - `EXTRACT_LLM_WORKERS`
  - `EXTRACT_LLM_LOCK_DIR`
- `call_model_json()` 在真正 `urllib.request.urlopen()` 前抢文件锁；
- 文件锁按 slot 实现；
- 一个请求结束后释放 slot；
- 第一次模型调用和第二次模型调用共用同一个入口，所以都会受限。

推荐：

```text
AutoDL 单卡：--workers 2 --llm-workers 1
稳定后再试：--workers 3 --llm-workers 1
```

不建议直接 `--workers 4` 起步，因为 MinerU parse 会加载 OCR/table/formula/VLM 相关模型，显存和 CPU/IO 都可能成为瓶颈。

接口层也已接入同一思路：

- `extract_api.py` 使用 `EXTRACT_API_WORKERS` 限制批量请求内部和请求之间的完整 parse + extract 任务并发；
- `extract_api.py` 启动 `extract.py` 子进程时注入 `EXTRACT_LLM_WORKERS` 和 `EXTRACT_LLM_LOCK_DIR`；
- 所以接口并发请求之间的第一次模型调用和第二次封装目录模型调用也会按全局 LLM slot 排队；
- Flask 本地启动时显式启用 `threaded=True`，允许多个 HTTP 请求同时进入服务，由文件锁决定实际执行数量。

## 16. 调试方式

### 16.1 判断错误发生在哪一层

优先顺序：

1. 看最终业务 JSON：`ex_outputs/<pdf_stem>.json`
2. 看抽取 debug：`ex_outputs/<pdf_stem>_debug.json`
3. 看最终 Markdown JSON：`ex_outputs/_mineru_parse/<pdf_stem>/<mode>/<pdf_stem>.json`
4. 看 middle_json：`*_middle.json`
5. 看 MinerU 原始模型输出：`*_model.json`
6. 对照源 PDF 页面

判断依据：

- 如果 `*_model.json` 的表格 HTML 已经错，问题在 MinerU 表格识别阶段；
- 如果 `*_model.json` 正确但 `*_middle.json` 或 `.md` 错，问题在后处理；
- 如果表格 HTML 正确但字段选择错，问题在第一次模型/规则判断；
- 如果字段正确但 pkg 数量或归属错，问题在第二次模型/封装目录/绑定；
- 如果字段和 pkg 都正确但记录数量错，问题在行抽取、pin 拆分或去重。

### 16.2 重点 debug 字段

`<pdf_stem>_debug.json` 中每张表通常会记录：

- `table_id`
- `page`
- `title`
- `group_context`
- `row_count`
- `status`
- `skip_reason`
- `decision`
- `semantic_sampling`
- `multi_package_plan`
- `target_local_slots`
- `package_assignment`
- `diagnostics`

常见状态：

- `too_few_rows`
- `pinout_matrix_table`
- `special_table`
- `semantic_batch_missing_result`
- `semantic_classification_failed_after_single_retry`
- `package_catalog`
- `multi_pkg_tab_route`
- `unresolved`

### 16.3 模型超时排查

第一次模型：

- 批次失败会打印：
  - 批次错误；
  - 失败批次表格 id/title/采样情况；
  - 单表重试错误。

第二次模型：

- 如果封装目录批次超时，会打印显式警告：

```text
⚠️ 封装目录判断批次超时: 候选表 [...] 标记为失败，后续将按现有规则尝试本地 fallback。
```

如果模型全部失败但 fallback 成功，最终可能仍然是正确结果。这不是模型成功，而是本地 fallback 接管。

## 17. 测试覆盖

当前测试主要位于：

```text
extract/test/
post_table/test_*.py
test_table_exporter.py
```

覆盖方向：

- 字段模型批量协议；
- 模型超时和 fallback；
- 封装目录解析；
- package binding 回归；
- 跨表多封装；
- 表内多封装；
- 表头 span 解析；
- pin number 拆分；
- description 输出约束；
- required field contract；
- special table handlers；
- repeated horizontal table filter；
- supplemental group filter；
- target local slots debug；
- MIN/MAX 坐标修复；
- 单元格换行恢复；
- 逻辑子表拆分。

常用验证命令：

```bash
python -m py_compile \
  batch_extract.py \
  extract/semantic_classifier.py \
  extract/package_catalog_resolver.py

python -m unittest extract.test.test_semantic_batching
python -m unittest extract.test.test_package_catalog_resolver
python -m unittest extract.test.test_package_binding_regressions
python -m unittest extract.test.test_multi_PkgTab_extractor
python -m unittest extract.test.test_special_table_handlers
python -m unittest post_table.test_restore_cell_line_breaks
python -m unittest post_table.test_min_max_coordinate_correct
python -m unittest post_table.test_split_merged_tables
```

AutoDL 上使用：

```bash
cd /root/autodl-tmp
source .env
source MinerU/.venv/bin/activate
python -m unittest extract.test.test_semantic_batching
```

## 18. 目前已知重要样例和经验结论

### 18.1 CC430 Family Members 污染封装目录

现象：

- `CC430F614x、CC430F514x、CC430F512x_64_48` 曾在某次模型成功返回时变成单个 `default`。

原因：

- 第二次模型把 `Table 3-1. Family Members` 误判成 `identity_summary`；
- 将多个 family member 行当成封装 entries；
- 绑定阶段出现歧义；
- 正常路径无结果后触发 empty-output default fallback。

当前修复：

- `find_package_catalog_candidates()` 中直接过滤表题含 `Family Members` 的表；
- 不送入第二次模型；
- 不进入 all-candidate fallback。

### 18.2 第二次模型超时反而得到正确结果

现象：

- 某些文件第二次模型调用超时后，fallback 得到正确封装；
- 模型成功时反而可能误引入错误表。

解释：

- 第二次模型只是帮助判断封装目录结构；
- 如果模型失败，本地 fallback 可能只使用高置信表题，例如 `PACKAGING INFORMATION`；
- 这会绕过模型误判的低质量候选。

当前策略：

- 对第二次模型超时打印警告；
- 对明显污染候选如 `Family Members` 在模型前过滤。

### 18.3 am62a3 超长 VSS 单元格

现象：

- `am62a3-AMB_484_484` 中 ANF 封装少大量 pin。

定位：

- 源 PDF 第 44 页 VSS 行：
  - AMB BALL NUMBER 有 111 个 pin；
  - ANF BALL NUMBER 有 111 个 pin；
  - 两列内容相同。
- MinerU `*_model.json` 同一行：
  - 第 0 列有 111 个 pin；
  - 第 1 列变成 `VSS`；
  - 第 2 列也变成 `VSS`；
  - 后续列基本为空。

结论：

- 错误发生在 MinerU 表格 HTML 生成阶段；
- 不是封装绑定问题；
- 不是最终抽取行逻辑问题。

### 18.4 AM335x 超长 VDD_CORE 单元格

现象：

- `AM335x_324_298` 中 ZCE 封装少多个 pin，并出现错乱。

定位：

- 源 PDF 第 46 页 VDD_CORE 行 ZCE 列包含：
  - `K15`
  - `K16`
  - `L12`
  - `L13`
  - `M7`
  - `M8`
  - `M12`
  - `N11`
  - `P9`
  - `P11`
- MinerU `*_model.json` 同一行漏掉这些值；
- 同时把一段 ZCZ 列内容串入 ZCE 列。

结论：

- 这是 MinerU 表格结构识别/OCR-cell 匹配阶段的错列；
- 后续抽取只能消费错误 HTML。

## 19. 当前未解决问题

### 19.1 超长单元格导致缺列、错列、串列

涉及样例：

- `am62a3-AMB_484_484`
- `AM335x_324_298`

根因：

- MinerU 对表格区域做图像识别；
- 超长 pin 列表被拆成大量 OCR 框；
- 表格结构模型预测的 cell bbox 与 OCR 框匹配时出现错位；
- 错误在 `*_model.json` 中已经存在。

可行修复方向：

1. 后处理兜底：
   - 针对明确表头结构，如 `AMB BALL NUMBER / ANF BALL NUMBER`、`ZCE BALL NUMBER / ZCZ BALL NUMBER`；
   - 检测数据行中 pin 列数量明显异常、后续 name/signal 列左移、或某 package 列为空；
   - 利用源 PDF 文本或相邻重复模式恢复缺失列。

2. MinerU 表格模型级修复：
   - 修改 OCR-cell matching 或 table structure recovery；
   - 风险较高，容易影响大量正常表。

当前建议：

- 优先做项目级后处理兜底；
- 不建议直接改 MinerU 模型算法。

### 19.2 单元格内误换行

涉及样例：

- `DRV8353M_40_40`
- `CC430F514x、CC430F614x_64_48`
- `CC430F614x、CC430F514x、CC430F512x_64_48`

现象：

- 短 pin 编号可能被错误插入 `<br>`，例如 `12` 变成 `1<br>2`。

根因：

- `restore_cell_line_breaks.py` 为修复缺少换行问题，会根据 PDF 字符坐标恢复视觉行；
- 对紧密数字、短单元格、OCR bbox 不稳定的场景，聚类可能过度拆分。

状态：

- 曾尝试调整聚类策略，但对最新样例无效；
- 已回退该改动；
- 当前暂时不继续处理。

后续方向：

- 仅对多 token 文本或存在明确多行证据的单元格恢复换行；
- 对短纯数字、短 pin 编号、单 token pin 形态设置更强保护；
- 增加“恢复前后 pin token 数量不应变化”的校验。

### 19.3 MinerU OCR 字符污染

已观察到：

- `E5` 被识别为 `E5.`
- `N1` 被识别为 `N 1`
- `U6` 被识别为 `U6.`

部分可以通过 pin token 清洗解决，但如果污染影响列匹配或 HTML 结构，则单纯清洗最终 token 不够。

### 19.4 模型稳定性和超时

当前已有：

- batch 请求；
- 超时重试；
- 第一次模型失败后逐表重试；
- 第二次模型超时警告；
- 第二次模型 fallback；
- 跨进程 LLM 限流。

仍需注意：

- 模型成功不代表结果一定正确；
- 对明显污染候选应尽量在模型前过滤；
- raw 模型返回目前没有完整保存到 debug，只保存归一化 diagnostics。后续如果需要定位 prompt 问题，建议保存裁剪后的 raw response。

### 19.5 `--skip-parse` 仍存在但不是主路径

`extract.py` 和 `batch_extract.py` 当前仍保留 `--skip-parse` 参数，便于复用已有 MinerU 输出。但项目标准流程是完整 parse + extract，不依赖该模式。

如果后续维护者修改并发逻辑，不应以 `--skip-parse` 为第一优先路径。

## 20. 维护注意事项

### 20.1 三方同步

项目维护要求：

- 本地；
- AutoDL；
- GitHub；

三方代码版本要保持一致。每次代码改动后应：

1. 本地测试；
2. 同步到 AutoDL；
3. AutoDL 测试；
4. 本地 commit；
5. push GitHub；
6. AutoDL fast-forward 到同一 commit；
7. 确认三方 HEAD 一致。

确认命令：

```bash
git rev-parse HEAD
ssh autodl-westc-49251 'cd /root/autodl-tmp && git rev-parse HEAD'
git ls-remote git@github.com:Panbkforever/mineru-custom.git refs/heads/main
```

注意：

- AutoDL 上有大量未跟踪输出/数据目录，不要删除；
- 不要用 `git reset --hard` 清理用户数据；
- 如需临时 stash，只 stash tracked 代码文件，并在同步后清理临时 stash。

### 20.2 修改优先级

遇到问题时建议按这个优先级处理：

1. 如果是 `*_model.json` 已经错，优先考虑后处理兜底，不先改封装绑定；
2. 如果模型误判，优先在模型前收窄候选或增强协议，不让模型处理明显脏候选；
3. 如果封装数量错，先查第二次模型 diagnostics 和 entries，再查 target local slots；
4. 如果同一个 pkg 下数量多/少，查行抽取、pin 拆分、去重；
5. 如果 package 绑定错，查 `package_catalog_resolver.py` 的 diagnostics，不要从 filename 推断。

### 20.3 不要随意破坏的硬约束

- 模型不生成最终值；
- 文件名不参与判断；
- description 不参与封装绑定；
- 已冻结 slot 后不创建新 pkg；
- 多封装分支必须一次性一对一绑定；
- 不能默认把 unresolved 多封装表塞入第一个 pkg；
- 不能跨 pkg 去重；
- `Family Members` 不送第二次封装目录模型；
- 特殊表过滤必须严格命中，不做模糊扩张。

## 21. 推荐下一步工作

如果项目后续继续推进，建议按以下顺序：

1. 为超长 pin 单元格建立独立后处理模块；
   - 先只覆盖 `AMB/ANF`、`ZCE/ZCZ` 这类明确双封装列；
   - 加入源 PDF 对照；
   - 只在检测到明显列错位时触发。

2. 保存模型 raw response 到 debug；
   - 限制长度；
   - 避免泄露 API key；
   - 便于复盘模型为什么误判。

3. 做小规模并发压力测试；
   - `--workers 2 --llm-workers 1`
   - 观察显存、总耗时、失败率、日志完整性。

4. 补全端到端回归样例；
   - CC430 family members；
   - AM335x VDD_CORE；
   - am62a3 VSS；
   - DRV8353M 数字误换行；
   - AM2431 多 22 pin 场景。

5. 将文档中“已知样例”固化成自动化 fixtures；
   - 用局部 HTML/middle_json 片段测试；
   - 避免每次完整跑 PDF 才能复现。

## 22. 当前项目状态总结

当前项目已经具备：

- 完整 parse + extract CLI；
- 批量完整 parse + extract；
- 文档级并发；
- 跨进程 LLM 限流；
- 表级模型批处理；
- 第一次模型失败逐表重试；
- 第二次模型超时警告和 fallback；
- 特殊表过滤；
- 表内多封装；
- 跨表多封装；
- 封装目录和 target 表绑定；
- debug/info 输出；
- 多类回归测试。

当前主要短板：

- MinerU 对超长表格单元格的结构识别错误无法在现有抽取层天然恢复；
- 单元格换行恢复仍存在误拆短编号风险；
- 模型 raw 返回没有完整落盘；
- 端到端样例自动化还不够完整。

总体判断：

项目主体流程已经成型，后续不应再大幅重构主链路。更合理的维护方向是：围绕已知 MinerU 解析缺陷增加小范围、高置信、可测试的后处理兜底，同时继续保持“模型只判结构、代码读值”的核心设计。
