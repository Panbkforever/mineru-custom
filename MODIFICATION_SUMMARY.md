# MinerU Custom 修改汇总

本文档记录当前仓库中基于原始 MinerU 项目追加或改动的主要文件、函数和处理逻辑，方便后续继续维护。

## 入口文件

### `parse_doc.py`

用途：命令行解析入口。负责调用 MinerU Python API，并在解析完成后执行页眉页脚过滤、表格后处理和 Markdown 重建。

主要函数：

- `_load_header_footer_filter()`
  - 动态加载 `modify/Filter_ headers_and_footers.py`。
  - 支持 AutoDL 路径 `/root/autodl-tmp/modify/...` 和本地相对路径两种情况。

- `_guess_union_make(output_subdir)`
  - 根据输出子目录名选择对应 Markdown 重建函数。
  - `auto/ocr/txt` 使用 pipeline 的 `union_make`。
  - `vlm*`、`hybrid*` 使用 VLM 的 `union_make`。
  - `office` 使用 office 的 `union_make`。

- `apply_header_footer_filter(doc_output_dir, source_pdf_path=None)`
  - 读取 `*_middle.json`。
  - 调用页眉页脚过滤逻辑。
  - 调用 `correct_min_max_tables_in_middle_json()` 修复极限值表格。
  - 调用 `restore_cell_line_breaks_in_middle_json()` 恢复单元格内真实换行。
  - 写回 middle_json，并重建最终 `.md`。

- `apply_post_table_correction(doc_output_dir)`
  - 遍历输出目录中的 Markdown。
  - 先调用 `split_merged_tables_in_file()` 拆分错误合并的逻辑子表。
  - 调用 `post_table/fix_ocr_table.py` 中的 `fix_markdown_file()` 做 Markdown 层面的表格 OCR 修正。
  - 最后输出与最终 Markdown 完全对应的同名 JSON。

- `main()`
  - 命令行入口。
  - 设置模型源。
  - 调用 MinerU `do_parse()`。
  - 解析完成后串联后处理流程。

重要行为：

- `parse_doc.py` 与 `mineru_api_zip.py` 的后处理逻辑应保持同步。
- 两个入口当前都显式关闭 `enable_line_boundary_filter`。仍执行显式页眉页脚类型和重复边缘文本过滤，但不再根据页面黑色横线推断正文上下边界，避免将表格自身边框误判为页脚分界线。
- 表格坐标修复和单元格换行恢复发生在 middle_json 阶段；Markdown OCR 后处理发生在 `.md` 阶段。
- 提取结果的 group 名保留原始 `Table xxx`/`表格 xxx` 编号，仅删除 `(continued)`；原表和续表仍按相同标题归组，同时可以按 PDF 表号检索。

### `mineru_api_zip.py`

用途：Flask API 入口。接收 PDF/图片上传，调用 MinerU 解析，打包输出 zip。

主要函数：

- `load_header_footer_filter()`
  - 动态加载页眉页脚过滤模块。

- `allowed_file(filename)`
  - 判断上传文件后缀是否允许。
  - 当前支持 `pdf/png/jpg/jpeg/tiff/bmp`。

- `sanitize_name(name)`
  - 将文件名中的非法字符替换为 `_`。

- `ensure_dirs()`
  - 确保输出根目录存在。

- `clear_previous_output(pdf_stem)`
  - 删除同名历史输出目录，避免旧结果残留影响新解析。

- `wait_for_output(outdir, timeout=MAX_WAIT_SECONDS)`
  - 轮询等待 MinerU 输出子目录生成。

- `guess_union_make(output_subdir)`
  - 与 `parse_doc.py` 中逻辑一致，根据输出模式选择 Markdown 重建函数。

- `apply_header_footer_filter(output_dir, source_pdf_path=None)`
  - API 版本的 middle_json 后处理。
  - 调用页眉页脚过滤、极限值表格坐标修复、单元格换行恢复。

- `run_mineru_pdf(pdf_path)`
  - 解析 PDF。
  - 当前默认 backend 为 `hybrid-auto-engine`。
  - 解析前会清理同名旧输出。

- `apply_post_table_correction(output_dir)`
  - 对 Markdown 执行表格 OCR 后处理。

- `make_zip(output_dir, zip_path)`
  - 打包解析结果。

- `process_pdf_zip()`
  - PDF API 主接口。

- `run_mineru_image(image_path)`
  - 图片解析流程。

- `process_image_zip()`
  - 图片 API 主接口。

重要配置：

- `MINERU_BACKEND`
  - 默认：`hybrid-auto-engine`。
  - 可用环境变量覆盖。

- `IMAGE_ANALYSIS`
  - 默认关闭。
  - 避免 VLM 额外分析图片导致输出出现 PDF 原文之外的信息。

## 页眉页脚过滤

### `modify/Filter_ headers_and_footers.py`

用途：在 middle_json 阶段识别并丢弃页眉、页脚、页码和部分首页底部声明。

主要类：

- `FilterConfig`
  - 配置页眉页脚过滤阈值。
  - 包括上下边距比例、重复文本阈值、页码识别、横线检测参数等。

- `LineBoundary`
  - 表示通过 PDF 横线检测得到的页面正文边界。
  - 用于判断横线以上/以下是否应过滤。

主要函数：

- `filter_headers_and_footers(pdf_info_list, pdf_path=None, config=None)`
  - 主入口。
  - 识别候选 block。
  - 将页眉页脚相关 block 移入 `discarded_blocks`。
  - 返回过滤统计。

- `detect_line_boundaries(pdf_path, pdf_info_list, config=None)`
  - 渲染 PDF 页面。
  - 检测页面上方和下方粗横线。
  - 将横线作为正文区域边界。

- `_detect_horizontal_rule_y(...)`
  - 在单页图像中检测横向粗线。

- `_longest_true_run(values)`
  - 查找连续满足条件的像素段。

- `_merge_consecutive_rows(rows)`
  - 合并连续像素行。

- `_pixel_y_to_page_y(...)`
  - 将渲染图像坐标转换为 PDF 页面坐标。

- `_find_repeated_margin_texts(pdf_info_list, config)`
  - 查找多页重复出现在页边的文本。

- `_find_first_page_bottom_notice_pages(pdf_info_list, config)`
  - 识别首页底部声明类区域。

- `_iter_candidate_blocks(page_info)`
  - 遍历页面中可判断的 block。

- `_should_discard_block(...)`
  - 综合规则判断 block 是否应丢弃。

- `_line_boundary_discard_reason(...)`
  - 根据横线边界判断丢弃原因。

- `_page_size(page_info)`
  - 获取页面尺寸。

- `_block_bbox(block)`
  - 获取 block 坐标。

- `_margin_band(...)`
  - 判断 block 是否处在页边区域。

- `_is_small_margin_block(...)`
  - 判断是否是页边小块文本。

- `_in_first_page_notice_band(...)`
  - 判断是否处于首页底部声明区域。

- `_bbox_to_page_units(...)`
  - 坐标单位转换。

- `_block_text(block)`
  - 提取 block 文本。

- `_normalize_text(text)`
  - 文本归一化，用于重复检测。

- `_looks_like_page_number(text)`
  - 判断文本是否像页码。

- `_is_protected_type(block)`
  - 保护表格、图片等不应过滤的 block。

- `load_pdf_info_list(data)`
  - 从 middle_json 中取出 `pdf_info` 列表。

- `main()`
  - 独立脚本入口。

当前逻辑重点：

- 优先使用上下粗横线确定页眉页脚范围。
- 检测不到横线时，回退到重复页边文本、页码、首页底部声明等规则。

## 表格 OCR 与 HTML 后处理

### `post_table/fix_ocr_table.py`

用途：Markdown/HTML 表格层面的 OCR 后处理，主要修复 pipeline 表格 OCR 常见混淆，以及部分特定 TI 表格结构。

主要函数：

- `_apply_cell_correction(value, header=None)`
  - 对单元格文本执行基础字符修正。

- `_has_header_keyword(header)`
  - 判断表头是否暗示当前列适合做字符混淆修正。

- `_is_confusion_column(values, header=None)`
  - 判断某列是否存在 OCR 混淆模式。

- `correct_table_cell(value)`
  - 对单个表格值做字符修正。

- `_correct_column_values(values)`
  - 对整列值做上下文修正。

- `_parse_table_structure(table_lines)`
  - 解析 Markdown 表格结构。

- `_replace_cell_in_line(...)`
  - 替换 Markdown 表格行中的指定单元格。

- `fix_markdown_tables(md_content)`
  - 修复 Markdown 表格。

- `fix_markdown_file(input_path, output_path=None)`
  - 文件级入口。
  - 默认覆盖原 Markdown。

- `fix_html_table(html)`
  - 修复 HTML 表格。

- `_parse_html_table(html)`
  - 将 HTML 表格解析成二维文本。

- `_parse_html_table_cells(html)`
  - 将 HTML 表格解析成带标签信息的二维结构。

- `_html_cell_plain_text(inner_html)`
  - 提取 HTML 单元格纯文本。

- `_is_terminal_functions_section_row(cells)`
  - 判断 Terminal Functions 表中的分组行。

- `_has_signal_name_no_header(rows)`
  - 判断表格是否具有 `SIGNAL / NAME / NO.` 表头结构。

- `_normalize_signal_name_no_header(html)`
  - 规范化 `SIGNAL / NAME / NO.` 表头。
  - 将识别成 `SIGNAL NAME NO.` 的表头处理成两列展开结构。

## 引脚与封装信息抽取

### `extract.py`

用途：单文件入口。先调用 `parse_doc.py` 解析 PDF，再从解析输出的 `*_middle.json` 中抽取引脚/封装信息，输出 JSON。

### `batch_extract.py`

用途：批量入口。默认遍历主目录下 `Multi_package_TIpdf/*.pdf`，将每个 PDF 的抽取结果写入 `ex_outputs/<pdf名>.json`。

可选参数：

- `--semantic-classify`
  - 启用 DeepSeek 表格语义分类。
  - 需要通过环境变量提供 `DEEPSEEK_API_KEY`。
  - 只用于判断表格是否可以创建封装引脚记录，不直接生成最终引脚数据。

### `extract/pin_package_extractor.py`

用途：从 MinerU `middle_json` 的 HTML 表格中识别引脚/封装字段，并输出结构化 JSON。

主要函数：

- `extract_pin_package_info_from_middle_json(...)`
  - 主入口。
  - 遍历 `middle_json` 中的表格。
  - 跳过订购信息表。
  - 识别引脚表后按封装分组、按表格分组、按 `pin_no` 归并记录。

- `score_package_column(header, values, title_context="")`
  - 判断某一列是否是“封装对应的引脚编号列”。
  - 使用综合打分，不只靠列名：
    - 封装名模式，如 `64 LQFP`、`48 PT/RGZ`、`ZCE Ball Number`。
    - 列值形态，大部分值应像引脚编号，如 `1`、`40`、`A11`、`R2`。
    - 表格上下文，如 `Pin Attributes`、`Terminal Functions`、`引脚属性`。
  - 订购表中的 `Package Pins`、`Package Type`、`Package Drawing` 不作为引脚列。

- `build_package_identity(label)`
  - 对封装名做标准化，生成内部 `pkg_key`。
  - 会提取：
    - `pin_count`：例如 `64`、`100`。
    - `family`：例如 `LQFP`、`VQFN`、`QFP`、`NFBGA`。
    - `code`：例如 `PM`、`PT`、`RGZ`、`RHB`、`ZCE`、`NZN`。
  - 示例：
    - `LQFP (100)` -> `100 LQFP`，`pkg_key=pins=100|family=LQFP`
    - `LQFP-64` -> `64 LQFP`，`pkg_key=pins=64|family=LQFP`
    - `引脚编号(1) 64 PM` -> `64 PM`，`pkg_key=pins=64|code=PM`
    - `ZCE Ball Number` -> `ZCE`，`pkg_key=code=ZCE`

- `get_package_bucket(...)`
  - 合并同一封装时不直接使用原始 `pkg` 字符串，而是使用标准化后的 `pkg_key`。
  - 这样同一封装在多个不同表格中出现时，会进入同一个封装对象。
  - 如果同一封装出现多个名称，例如 `ZCE`、`ZCE-64`，会在同一个封装对象的 `pkg` 后追加别名，而不是拆成两个封装。

- `package_identities_compatible(...)`
  - 判断两个封装标准化身份是否可合并。
  - 稳定 package code 相同且 pin count 不冲突时合并，例如 `ZCE` 和 `ZCE-64`。
  - family 相同且 pin count 相同或一方缺失时合并，例如 `LQFP-64` 和 `64 LQFP`。

- `add_pin_record_to_group(...)`
  - 同一封装、同一 group 内按 `pin_no` 做引脚级归并。
  - 如果一个表给出 `pin_name`，另一个表给出 `type`，最终合到同一个引脚记录中。

- `semantic_allows_pin_creation(...)`
  - 可选语义过滤入口。
  - 当 `--semantic-classify` 开启时，会调用 `extract/semantic_classifier.py`。
  - 语义分类结果只有在 `should_create_pins=true` 且置信度不低于 0.6 时才允许抽取。

### `extract/semantic_classifier.py`

用途：调用 DeepSeek API 对表格做语义分类。

主要函数：

- `classify_table_semantics(...)`
  - 输入表名、表头、样例行、规则初判列角色。
  - 输出结构化 JSON，包括：
    - `table_role`
    - `should_create_pins`
    - `package_columns`
    - `name_columns`
    - `type_columns`
    - `confidence`
    - `reason`

- `call_deepseek_json(...)`
  - 使用 OpenAI-compatible Chat Completions 调用 DeepSeek。
  - 默认 base url：`https://api.deepseek.com`
  - 默认模型：`deepseek-v4-flash`
  - API key 从环境变量 `DEEPSEEK_API_KEY` 读取，不写入代码。

相关环境变量：

- `DEEPSEEK_API_KEY`
  - DeepSeek API Key，必填。
- `DEEPSEEK_MODEL`
  - 默认 `deepseek-v4-flash`。
- `DEEPSEEK_BASE_URL`
  - 默认 `https://api.deepseek.com`。
- `DEEPSEEK_TIMEOUT`
  - 默认 `30` 秒。

当前逻辑重点：

- 判断封装列：使用“封装名模式 + 列值形态 + 表格上下文”综合打分。
- 判断两个封装是否相同：先做封装名标准化，再比较 `pin_count/family/code` 组成的 `pkg_key`。
- 多表合并同一封装：用标准化身份合并封装，封装别名追加到 `pkg`，再按表名进入不同 group。
- group 表示表名或表格分组；同一个封装可能出现在多个不同表格中，按 group 划分来源。
- LLM 语义分类只负责判断“这个表是否表达封装-物理引脚-信号映射关系”；最终数据仍由代码从表格中抽取，避免模型直接编造引脚。

- `_fix_terminal_functions_table_html(html)`
  - 修复 Terminal Functions 表格前两列合并错误。

- `tms320c6211b_TerminalFunctions_table(md_content)`
  - 以 `文件名_表名_table` 风格命名的特定表格后处理函数。
  - 当前用于修复 `tms320c6211b` 的 Terminal Functions 类表。

- `get_column_values(rows, col_index)`
  - 获取表格某列值。

- `print_confusion_analysis(values, header=None)`
  - 输出调试分析信息。

当前逻辑重点：

- OCR 字符混淆修正。
- Terminal Functions 类表格的 `SIGNAL NAME / NO.` 前两列拆分。
- 重复的分组行保留，因为这是用户需要的 row/col 展开结果。

### `post_table/min_max_coordinate_correct.py`

用途：使用 PDF 原生文字坐标修正极限值表格中 `MIN/TYP/NOM/MAX` 列的错位、重复、合并列问题。

主要类：

- `MinMaxCorrectionStats`
  - 统计处理表格数、命中数、拆列数、修正行数、nowrap 单元格数等。

主要函数：

- `correct_min_max_tables_in_middle_json(middle_json, pdf_path)`
  - 主入口。
  - 遍历 middle_json 中所有 HTML 表格。
  - 展开 rowspan/colspan。
  - 调用 `_correct_one_table()` 修复极限值表。
  - 调用 `_protect_numeric_value_spacing()` 保持短数值表达式不换行。
  - 调用 `post_table.ultra_long_table_processing.repair_ultra_long_table_html()` 作为超长表格处理入口。

- `_correct_one_table(...)`
  - 单个表格修复入口。
  - 判断是否存在 `MIN/TYP/NOM/MAX` 表头。
  - 通过 PDF 横线检测定位数据行。
  - 通过 PDF 原生文本定位值列中心。
  - 修复重复值、错位值、合并列。

- `_resolve_row_bands(...)`
  - 根据表格横线和 header 行位置确定每一行的纵向范围。

- `_correct_single_pdf_value_duplicated_in_html(...)`
  - 修复同一个 PDF 值被 VLM 重复放入多个值列的问题。
  - 例如同一值同时出现在 MIN 和 MAX，实际 PDF 只在其中一列。

- `_correct_values_by_pdf_centers(...)`
  - 修复非重复但错位的值。
  - 例如 `2` 被放到 TYP，但 PDF 证明它在 MIN。

- `_find_unique_pdf_value_column(...)`
  - 当字符中心点匹配失败时，退回到 PDF 单元格文本判断唯一列。

- `_pdf_cell_text_contains_value(pdf_text, value)`
  - 判断 PDF 单元格文本是否包含目标值。
  - 包含边界判断，避免 `2` 误匹配 `257`。

- `_preserve_explicit_value_span(...)`
  - 判断一个值是否来自原始 colspan，是否应该保留跨多列展开。

- `_find_pdf_text_run_centers(...)`
  - 从 PDF 行内文本块推断文本中心。

- `_split_merged_min_max_columns(...)`
  - 将识别成 `MIN MAX` 的合并列拆成独立 `MIN`、`MAX`。

- `_parse_expanded_rows(table_html)`
  - 解析已展开后的 HTML 表格。

- `_protect_numeric_value_spacing(table_html)`
  - 对短数值表达式使用 `&nbsp;` 保护，避免 Markdown/HTML 渲染时换行。

- `_is_short_numeric_expression(value)`
  - 判断是否是应该保持单行的短数值表达式。

- `_replace_text_spaces_with_nbsp(value)`
  - 将文本空格替换为 `&nbsp;`。

- `_find_value_header(rows)`
  - 查找 `MIN/TYP/NOM/MAX` 表头。

- `_value_header_groups(labels)`
  - 将 `MIN/TYP/MAX`、`MIN/NOM/MAX`、`MIN/MAX` 识别成独立值组。

- `_find_explicit_colspan_ranges(table_html)`
  - 在展开 colspan 前记录原始 colspan 证据。

- `_find_merged_min_max_header(rows)`
  - 查找合并的 `MIN MAX` 表头。

- `_merged_header_prefix(value)`
  - 提取合并表头前缀。

- `_clone_cell(cell, inner)`
  - 克隆单元格结构。

- `_place_merged_value(...)`
  - 将合并列中的值放回 MIN 或 MAX，或判断为跨列共享。

- `_find_pdf_value_center(...)`
  - 查找单个值在 PDF 中的中心点。

- `_find_pdf_value_centers(...)`
  - 查找一个值在 PDF 行范围内所有匹配中心点。

- `_normalized_match_has_value_boundaries(...)`
  - 判断归一化文本匹配是否有合理边界。
  - 防止短数字误匹配长数字。

- `_normalized_text_with_indexes(value)`
  - 建立归一化字符与原始字符索引映射。

- `_alternating_min_max(labels)`
  - 判断标签是否为 `MIN/MAX` 交替结构。

- `_detect_horizontal_lines(...)`
  - 渲染表格区域并检测横线。

- `_detect_lines_in_horizontal_segment(...)`
  - 在局部区域中检测横线。

- `_find_pdf_value_headers(...)`
  - 在 PDF 原生文本中定位 `MIN/TYP/NOM/MAX` 表头。

- `_column_ranges_from_centers(centers, table_left, table_right)`
  - 根据列中心推导列范围。

- `_extract_pdf_cell_text(...)`
  - 从 PDF 指定坐标范围抽取文本。

- `_rebuild_table(rows)`
  - 将二维单元格结构重建为 HTML 表格。

- `_plain_text(value)`
  - HTML 转纯文本。

- `_normalize_value(value)`
  - 值归一化，用于匹配。

- `_iter_html_spans(value)`
  - 遍历 middle_json 中所有带 HTML 表格的 span。

- `_merge_consecutive(values)`
  - 合并连续索引。

- `_page_size(page_info)`
  - 获取页面尺寸。

- `_valid_bbox(bbox)`
  - 判断 bbox 是否有效。

当前逻辑重点：

- PDF 坐标只用于定位，不替换 VLM 识别出的原始 HTML 值。
- 只有在 PDF 坐标或 PDF 单元格文本能够较明确证明时才修正。
- 已支持 `MIN/MAX`、`MIN/TYP/MAX`、`MIN/NOM/MAX`。
- `Pin Attributes` 超长表格不再在这里写具体修复逻辑，只调用单独模块入口。

### `post_table/restore_cell_line_breaks.py`

用途：根据 PDF 原始文字的视觉行恢复 HTML 单元格内部换行。

主要类：

- `TextRun`
  - 表示 PDF 中一段视觉文本行。
  - 包含文本、bbox、中心点等信息。

主要函数：

- `restore_cell_line_breaks_in_middle_json(middle_json, pdf_path)`
  - 主入口。
  - 遍历 middle_json 中 HTML 表格。
  - 根据 PDF 中单元格文本的视觉行插入 `<br>`。

- `_restore_table_cells(...)`
  - 单表格处理。

- `_match_visual_lines(...)`
  - 将 HTML 单元格文本与 PDF 视觉行匹配。

- `_line_run_variants(runs)`
  - 生成视觉行组合候选。

- `_same_cell_column(previous, current)`
  - 判断两个文本行是否属于同一列。

- `_extract_visual_runs(...)`
  - 从 PDF 表格区域抽取视觉文本行。

- `_characters_to_text(characters)`
  - 将 PDF 字符列表转成文本。

- `_insert_breaks_by_normalized_offsets(...)`
  - 按归一化字符偏移向 HTML 单元格插入 `<br>`。

- `_visible_character_map(value)`
  - 建立 HTML 可见字符与原始位置映射。

- `_plain_text(value)`
  - HTML 转纯文本。

- `_normalize(value)`
  - 文本归一化。

- `_iter_html_spans(value)`
  - 遍历 middle_json 中 HTML 表格 span。

- `_page_size(page_info)`
  - 获取页面尺寸。

- `_valid_bbox(bbox)`
  - 判断 bbox 是否有效。

当前逻辑重点：

- 不按文本长度猜测换行。
- 只有当 PDF 原始字符能匹配出多条视觉行时才插入 `<br>`。
- 用于解决原始 PDF 单元格有明显换行，但 VLM HTML 输出成一行的问题。

### `post_table/ultra_long_table_processing.py`

用途：超长表格处理模块。当前作为专门入口，后续所有超长表格逻辑都应写在这里。

主要函数：

- `repair_ultra_long_table_html(table_html)`
  - 超长表格处理入口。
  - 当前采取保守策略：识别 Pin Attributes 类表格，但不再通过相邻行猜测补齐 `VSS` 字段。
  - 这样避免把本来为空的 ball 行错误补成 `VSS / VSS / GND`。

- `_is_pin_attributes_table(table_html)`
  - 判断是否是 Pin Attributes 类表格。

- `_find_pin_attributes_header(rows)`
  - 查找 `Ball Num / Ball Name / Signal Name / Signal Type` 这类表头。

- `_normalize_header_label(value)`
  - 归一化表头文本。

- `_first_label_index(labels, needle)`
  - 查找表头列位置。

- `_parse_expanded_rows(table_html)`
  - 解析 HTML 表格。

- `_plain_text(value)`
  - HTML 转纯文本。

当前状态：

- 该模块目前不做实质补齐，只作为超长表格逻辑的集中入口。
- 原因：AG15/AG25 等问题说明，仅靠相邻行推断会把字段错误加到整段超长 ball 列表上。
- 后续正确方向应是结合 PDF 坐标，将一个超长单元格按纵向位置拆成多段，再分别填充字段。

## 表格结构辅助

### `post_table/split_merged_tables.py`

用途：在 Markdown 后处理开始时，拆分被 MinerU 错误合并在同一个
`<table>` 中的多个逻辑子表。

命中条件：

- 前一段已经存在正常数据行。
- 分界处是横跨整表的小节标题，后面完整重复前面的单级或多级表头；或者
  在没有小节标题时，完整表头本身再次出现。
- 重复表头之后仍然存在正常数据行。
- 没有 `rowspan` 从分界上方跨越到分界下方。

保护规则：

- 只出现小节标题但没有完整重复表头时不拆。
- 多级表头只重复一部分时不拆。
- 多级表头第二层不能单独作为分界。
- 表头文本比较会统一大小写、空白、`<br>` 和脚注标记，但不会修改输出内容。
- 拆分发生在 `rowspan/colspan` 展开之前，保留原始单元格属性和换行。

主要函数：

- `split_merged_tables_in_file()`
  - 文件级入口，原地更新 Markdown 并返回新增 table 数量。
- `split_merged_tables_in_markdown()`
  - Markdown 字符串级入口。
- `split_merged_table_html()`
  - 使用 BeautifulSoup DOM 分析并重建单个 HTML 表格。
- `_find_split_boundaries()`
  - 根据完整重复表头和前后数据行确定所有分界。

### `post_table/expand_rowspan.py`

用途：展开 HTML 表格中的 rowspan/colspan，方便后处理按二维表格操作。

主要函数：

- `expand_rowspan(html)`
  - 展开所有表格的 rowspan。

- `_expand_rowspan_in_table(table_html)`
  - 单表格 rowspan 展开。

- `_remove_rowspan_attr(tag_open)`
  - 删除已处理的 rowspan 属性。

- `expand_colspan(html)`
  - 展开所有表格的 colspan。

- `_expand_colspan_in_table(table_html)`
  - 单表格 colspan 展开。

- `_remove_colspan_attr(tag_open)`
  - 删除已处理的 colspan 属性。

## 测试文件

### `post_table/test_split_merged_tables.py`

用途：验证拆表规则的通用结构约束，不绑定具体 PDF 名或具体小节名称。

覆盖情况：

- 全宽小节标题加完整两级重复表头。
- 没有小节标题但完整表头再次出现。
- 只有小节标题、表头重复不完整、多级表头只匹配第二层时保持原表。
- 一个 HTML 表格中存在多个逻辑分界。
- `rowspan` 跨越候选分界时禁止拆表。

### `post_table/test_min_max_coordinate_correct.py`

用途：针对极限值表格坐标修复模块的回归测试。

当前测试函数：

- `test_value_header_groups_include_typ_and_nom()`
  - 验证 `MIN/TYP/MAX`、`MIN/NOM/MAX` 分组识别。

- `test_duplicate_without_colspan_keeps_nearest_column()`
  - 验证重复值会保留在 PDF 坐标最近的列。

- `test_explicit_shared_colspan_is_fully_expanded_and_preserved()`
  - 验证真实 colspan 跨列共享值不会被错误删掉。

- `test_false_numeric_colspan_is_not_preserved()`
  - 验证误识别的数字 colspan 不会被当成真实跨列共享。

- `test_non_duplicate_values_are_relocated_by_pdf_centers()`
  - 验证非重复值可根据 PDF 坐标重新放回正确列。

- `test_short_value_does_not_match_inside_longer_number()`
  - 验证 `2` 不会误匹配到 `257` 内部。

注意：

- 单元测试主要用于防止已知逻辑退化。
- 它不能证明所有 PDF 或所有表格都正确。
- 对超长表格这类问题，不能只依赖单个构造案例判断通用正确性。

## 当前处理顺序

PDF 解析后的主要后处理顺序如下：

1. MinerU `do_parse()` 生成 middle_json 和初始 Markdown。
2. 读取 middle_json。
3. `filter_headers_and_footers()` 过滤页眉页脚。
4. `correct_min_max_tables_in_middle_json()` 修正极限值表格坐标。
5. `restore_cell_line_breaks_in_middle_json()` 恢复单元格内换行。
6. 使用对应 backend 的 `union_make()` 重建 Markdown。
7. `split_merged_tables_in_file()` 在原始 `colspan/rowspan` 尚未展开时拆分逻辑子表。
8. `fix_markdown_file()` 展开合并单元格并执行 Markdown 层面的表格 OCR 修正。
9. 输出与最终 Markdown 一一对应的同名 JSON。

## 当前风险与后续方向

- 超长 Pin Attributes 表格仍未完成真正修复。
  - 当前只保留入口，不再进行相邻行猜测补齐。
  - 后续应基于 PDF 坐标拆分超长单元格纵向区间。

- 针对 TI 数据手册的部分表格修复带有格式假设。
  - 如 Terminal Functions、MIN/TYP/MAX 极限值表。
  - 对新表格类型应先分析结构，再决定是否加入通用规则或特定规则。

- 单案例验证只能作为回归样本。
  - 不能用单个 PDF 或单个表格证明通用正确。
  - 更可靠的验证方式应包括多文件批量对比、抽样人工检查、统计修正前后差异。

## 2026-07-23：物理引脚字段最低契约

涉及文件：

- `extract/pin_package_extractor.py`
  - `decision_from_schema(...)`：模型返回 `should_extract=true` 后，由代码再次
    检查是否存在 `pin_no` 映射；缺失时以
    `semantic_missing_pin_no_column` 明确拒绝。
  - `has_pin_number_column(...)`：统一定义物理引脚表的最低字段要求，只强制
    `pin_no`，不再强制 `pin_name`。
  - `is_pin_package_table(...)`：无模型规则分支同步采用同一最低字段契约。
  - `extract_records_from_row(...)`、`extract_records_from_bound_package_row(...)`：
    已确定 `pin_no` 列后，空编号单元格保留为 `pin_no: ""`；`pin_name`
    整列缺失或单元格为空时，由最终清洗统一填充 `Reserved`。
  - 单封装行循环跳过完全空白的 HTML 排版行，避免生成空编号占位记录。

- `extract/multi_package_extractor.py`
  - `iter_bound_package_rows(...)`：多封装绑定已经确定 `pin_no` 列后，不再
    因该行编号为空而提前丢行。
  - `_is_structural_group_row(...)`：完全空白行仍作为排版占位跳过。

- `extract/semantic_classifier.py`
  - 模型提示明确要求：没有物理 Pin/Ball/Terminal 编号列时必须返回
    `should_extract=false`；`pin_name` 可选，缺失名称由确定性代码补
    `Reserved`。

- `extract/test_required_field_contract.py`
  - 使用结构化通用案例覆盖：模型缺编号列、只有编号列、空编号单元格、
    完全空白行、多封装空编号以及完整字段映射。

## 2026-07-23：全文封装实体识别与 pkg 归属

涉及文件：

- `extract/package_resolver.py`
  - 把最终 Markdown 按阅读顺序转换为标题、正文和完整表格证据块，每个块
    使用稳定 `block_id`。
  - 封装语义模型分块读取全文，只返回具体 `package_identity`、原文名称和
    证据块 ID。代码拒绝不存在的 block ID、原文中找不到的名称以及只有
    QFN/BGA/LQFP 等机械家族的结果。
  - 候选实体按名称、drawing code、pin count 和 device scope 校验合并；
    相同家族、不同 drawing/device scope 不合并。
  - 单封装表按“多封装绑定 > 当前表题 > 当前表头 > 语义上下文 > 续表 >
    同章节唯一归属 > 引脚集合”确定 pkg。
  - 当前表题和表头中的具体封装优先于上下文候选；二者指向不同具体封装时
    保持空 pkg 并记录冲突，不进行猜测。
  - 表级语义归属只能返回候选库已有 `package_id` 和有效证据块 ID，不能
    创建自由文本封装名。
  - 全文实体发现默认按约 12000 字符的完整块边界分批、并发 4；封装模型
    默认超时 60 秒。可分别通过 `EXTRACT_PACKAGE_CHUNK_CHARS`、
    `EXTRACT_PACKAGE_WORKERS` 和 `EXTRACT_PACKAGE_TIMEOUT` 调整。

- `extract/pin_package_extractor.py`
  - 最终 JSON 入口把完整 Markdown 证据块传给封装解析阶段。
  - 封装候选、证据、每表归属模式和拒绝原因写入调试信息。
  - 表格字段判断和逐行引脚提取职责不变。

- `extract/semantic_classifier.py`
  - 提取通用 JSON 模型调用底座，字段判断和封装判断使用各自独立的 system
    prompt，共用网络重试与 JSON 容错逻辑。

- `extract/test_package_semantic_resolution.py`
  - 使用不依赖具体 PDF 的伪模型结果验证：虚构名称拒绝、表头具体封装优先
    于表题家族、表题/表头具体身份冲突不猜、表归属只能选择已有 package ID。

## 2026-07-24：pkg 单一规范名称

涉及文件：

- `extract/package_resolver.py`
  - `PackageCandidate.aliases` 继续保存同一封装的原文别名，但
    `PackageCandidate.display` 只返回一个规范名称，不再通过 `|` 拼接。
  - `TablePackageAssignment.selected_label` 保存当前表题或表头实际命中的名称；
    单封装表按“当前表题 > 当前表头 > 其他上下文 > 候选规范名”输出 pkg。
  - package drawing/code 是没有表级名称时的强兜底；名称不超过 15 个字符
    只作为排序信号，不截断证据充分的原文名称。
  - `display_for_label(...)` 保留多封装表头/控制列中的单个原始名称，不扩展
    为候选实体的全部别名。
  - 续表和同章节继承会同时继承唯一且一致的 `selected_label`；名称存在冲突
    时只继承内部 package key，再由候选规范名兜底。

- `extract/pin_package_extractor.py`
  - 最终封装桶使用解析阶段的内部 package key 分组，避免不同 drawing/code
    因显示名称相似而错误归并。
  - 同一 key 遇到多个别名时只保留证据等级最高的一个名称；等级相同时保留
    文档中先出现的名称，别名仅保存在内部调试结构。
  - 最终公开 JSON 增加强制校验：`pkg` 包含 `|` 时直接报错，不能静默输出
    非法的别名拼接结果。
  - 真正的多封装表继续生成多个外层 package 对象；调试信息使用数组记录
    多个 package，不再生成看似单一 pkg 的拼接字符串。

- `extract/test_package_semantic_resolution.py`
  - 新增通用测试覆盖：别名只内部保存、表题别名优先、最终桶只输出一个
    高优先级名称，以及不同 package key 必须生成不同外层对象。

## 2026-07-24：隔离 DESCRIPTION 与封装判断

涉及文件：

- `extract/pin_package_extractor.py`
  - DESCRIPTION 是只读附加字段，不参与 pin、type 或 package 字段判断。
  - 组合表头即使误带首行 description 文字，也不能因为其中出现 package、
    pin、ball 等词提高核心字段分数，避免数据行被错选为表头。
  - DESCRIPTION 表头允许识别后方误附加的首行文字，作为异常输入防御，但
    附加文字不会改变该列的 description 语义。

- `extract/multi_package_extractor.py`
  - package 控制列评分明确排除 DESCRIPTION/描述语义列，说明文字中出现
    Package/PKG/封装不再触发多封装分支。

- `extract/test_description_output.py`
  - 使用通用三列表格验证：首条描述含 package 时仍选择正确表头、description
    不能成为 package 控制列、最终不能按每条描述拆出多个虚假封装。

## 2026-07-24：删除真实 pkg 识别，改为匿名结构分组

当前规则取代前述所有真实封装名识别和封装实体归属规则。

- 删除 `extract/package_resolver.py`
  - 不再扫描全文、标题、表头、DESCRIPTION 或引脚集合识别真实封装名称。
  - 不再调用模型发现封装实体，也不再维护 drawing、family、pin count、
    alias、package key 或跨表封装归属。
- 删除 `extract/table_association_rules.py`
  - 不再依据标题名称、引脚重合度或上一张表推断补充表属于哪个真实封装。
- `extract/pin_package_extractor.py`
  - 单封装表统一进入内部结构槽位 0。
  - 多封装表仍由 `multi_package_extractor.py` 判断列、行和分段结构，但表头
    中的封装文字只用于建立当前表内绑定，不作为输出名称。
  - 多封装绑定按原表先后顺序进入槽位 0、1、2……；不同表只按槽位序号汇总。
  - 最终序列化时才把非空槽位依次写为 `pkg: "a"`、`"b"`、`"c"`；超过
    26 个槽位后继续使用 `aa`、`ab`。
- `extract/semantic_classifier.py`
  - 模型只判断表格是否需要提取以及 `pin_no`、`pin_name`、`type` 列，
    不再存在供封装识别复用的通用模型调用入口。
- 删除 `extract/test_package_resolver.py` 和
  `extract/test_package_semantic_resolution.py`，新增
  `extract/test_package_slot_grouping.py` 验证匿名槽位分组。

# 文档级封装目录识别

新增 `extract/package_catalog_resolver.py`，将真实 `pkg` 判断与表格字段判断、
多封装表内结构分析和逐行提取彻底分离。系统现在先从全文前后区域及封装语义
表格中召回总述候选，再由独立模型协议返回真实封装目录，最后使用表题、当前
章节、表头和已确认的多封装绑定将引脚表归入对应封装。

旧的 `a/b/c` 结构槽位输出已经从主流程删除。无法确认真实名称时保留空
`pkg`，不会用假名称代替；一个 `pkg` 只能输出一个名称，禁止使用 `|`
拼接候选，且真实名称最长 15 个字符。`multi_package_extractor.py`继续只处理单张表内部的列、行和分段
绑定，不参与文档级名称判断。
