"""
=============================================================================
表格 OCR 识别结果后处理 — 核心逻辑
=============================================================================

本模块解决 MinerU pipeline 后端表格 OCR 中的字符混淆问题。
核心思路：通过表格列级上下文来判断某一列是否发生了 OCR 误识别，
然后仅对确认有误识别的列执行修正。

修正策略：
  1. 解析 markdown 表格，按列提取所有单元格的值
  2. 对每一列，根据列标题 + 值分布判断是否属于"混淆列"
  3. 对确认的混淆列，按照映射表执行值修正
  4. 将修正后的值写回 markdown

混淆列的判断依据（同时满足）：
  - 列标题包含关键词：类型/type/种类/类别/功能/function/信号/signal
  - 或标题无关键词但列中 ≥60% 的值是单个字符
  - 且列中所有单字符值都属于混淆集 {0, 1, 二, I, O, —}
  - 且列中互不相同的值种类 ≤ 4（I/O/— 最多 3 种原始值 + 误识别）

字符映射规则：
  - 1 → I  （数字1 → 大写字母I，常见于 Input 缩写）
  - 0 → O  （数字0 → 大写字母O，常见于 Output 缩写）
  - 二 → — （汉字二 → 长破折号，常见于电源/接地标识）

使用方式：
    from post_table.fix_ocr_table import fix_markdown_file
    fix_markdown_file("input.md", "output.md")
=============================================================================
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# =============================================================================
# 常量定义
# =============================================================================

# OCR 误识别映射表：key=误识别结果, value=正确值
# 说明：中文 PP-OCRv4 模型在表格单元格中，对于视觉相似字符，
#       倾向于输出字典索引靠前的字符，导致以下典型误识别：
OCR_CONFUSION_MAP = {
    "1": "I",   # 数字 1（字典#93）→ 大写字母 I（字典#3587）
    "0": "O",   # 数字 0（字典#26） → 大写字母 O（字典#4741）
    "二": "—",  # 汉字 二（字典#320） → 长破折号 —（字典#5550）
}

# 所有可能出现在混淆列中的值（包括误识别值和正确值）
# 用于判断某一列是否属于"混淆列"
CONFUSION_SET = set(OCR_CONFUSION_MAP.keys()) | set(OCR_CONFUSION_MAP.values())

# 列标题关键词：如果列标题包含以下关键词，则该列被优先视为混淆列
# 这些词常见于电子/工程文档中表示引脚类型的列名
HEADER_KEYWORDS = [
    "类型", "type", "种类", "类别",
    "功能", "function",
    "信号", "signal",
    "方向", "direction",
    "I/O", "io",
]

# 单字符比例阈值：当列中 ≥ 该比例的值是单字符时，触发混淆检测
SINGLE_CHAR_RATIO_THRESHOLD = 0.6

# 最大互异值数量：混淆列中不同值的种类数不会超过此值
# I/O/— 加上可能的误识别，最多 6 种（I/O/— + 1/0/二）
MAX_UNIQUE_VALUES = 6

# 引脚名称 → 类型推断规则
# 当"类型"列（混淆列）的值存在歧义时（如 1 既可能是 I 也可能是 —），
# 根据该行第一列（引脚名称）的上下文来推断正确的值。
# 规则按优先级排列，匹配即返回。
PIN_TYPE_RULES: list[tuple[str, int, str]] = [
    # ── 输入引脚 → I ──
    (r"^(IN|输入|同相|反相|正向|反向)", re.IGNORECASE, "I"),
    (r"IN[+\-]?\d*$", re.IGNORECASE, "I"),
    # ── 输出引脚 → O ──
    (r"^(OUT|输出)", re.IGNORECASE, "O"),
    (r"OUT\d*$", re.IGNORECASE, "O"),
    # ── 电源/地/NC → —（长破折号） ──
    (r"^(GND|VCC|VEE|VDD|VSS|V[+\-]?|电源|地|接地)$", re.IGNORECASE, "—"),
    (r"(电源|接地|地|散热)$", re.IGNORECASE, "—"),
    (r"^(NC|悬空|散热焊盘)$", re.IGNORECASE, "—"),
]


def _apply_cell_correction(
    pin_name: str, current_value: str, allow_fallback: bool = True
) -> str:
    """
    根据引脚名称（第一列）对混淆列单元格执行消歧修正。

    流程：
        1. 如果当前值不含疑似 OCR 误识别源字符（1/0/二），直接返回（已是正确值）
        2. 根据引脚名称匹配 PIN_TYPE_RULES 推断正确值
        3. 若无法推断且 allow_fallback=True，回退到字符级替换（1→I, 0→O, 二→—）

    参数：
        pin_name:       该行第一列的值（引脚名称）
        current_value:  当前单元格的 OCR 识别文本
        allow_fallback: 是否允许回退到字符级替换。
                       非混淆列（如引脚编号列）设为 False，避免误伤合法数字。

    返回：
        str: 修正后的值

    示例：
        >>> _apply_cell_correction("GND", "1")
        '—'
        >>> _apply_cell_correction("IN+", "1")
        'I'
        >>> _apply_cell_correction("IN+", "I")   # 已正确，无混淆源字符
        'I'
        >>> _apply_cell_correction("", "1")       # 无引脚名，回退到字符替换
        'I'
        >>> _apply_cell_correction("OUT2", "1", allow_fallback=False)  # 无规则匹配，不回退
        '1'
    """
    # 检查是否包含疑似 OCR 误识别的源字符（1、0、二）
    # 注意：只检查 key（误识别来源），不检查 value（已正确修正的值）
    confusion_sources = set(OCR_CONFUSION_MAP.keys())  # {"1", "0", "二"}
    has_confusion = any(c in current_value for c in confusion_sources)

    if not has_confusion:
        # 当前值已是正确值（I/O/—），不做任何修改
        return current_value

    # 包含混淆字符，尝试用引脚名称推断正确类型
    # 非混淆列（allow_fallback=False）跳过规则推断，避免将引脚编号误换为 I/O
    if pin_name and allow_fallback:
        for pattern, flags, inferred in PIN_TYPE_RULES:
            if re.search(pattern, pin_name, flags):
                return inferred

    # 无法通过规则推断
    if not allow_fallback:
        # 非混淆列（如引脚编号列）：不做完整规则匹配和 OCR_CONFUSION_MAP 字符替换
        # 但 "二" → "—" 是安全的（"二" 从不出现在合法引脚编号中）
        result = current_value
        if "二" in result:
            result = result.replace("二", "—")
        return result

    # 无法推断，回退到字符级替换（旧行为）
    result = current_value
    for old, new in OCR_CONFUSION_MAP.items():
        result = result.replace(old, new)
    return result


# =============================================================================
# 列级检测函数
# =============================================================================

def _has_header_keyword(header: Optional[str]) -> bool:
    """
    检查列标题是否包含混淆列关键词。

    参数：
        header: 列标题文本（可能为 None，常见于无表头的表格）

    返回：
        bool: 是否匹配关键词

    说明：
        - 大小写不敏感，会去除标题中的空格后再匹配
        - 先对 header 执行 correct_table_cell 修正，再匹配关键词
          这样 OCR 误识别的标题（如 1/0 → I/O）也能正确匹配
    """
    if not header:
        return False

    # 先对 header 做字符级 OCR 修正，再匹配关键词
    # 注意：不能用 correct_table_cell（它是精确匹配），需要子串级替换
    # 例如 "1/0" → "I/O"（字符替换后匹配 "I/O" 关键词）
    header_fixed = header
    for old_char, new_char in OCR_CONFUSION_MAP.items():
        header_fixed = header_fixed.replace(old_char, new_char)
    # 统一转小写并去空格，方便关键词匹配
    header_clean = re.sub(r"\s+", "", header_fixed).lower()
    for kw in HEADER_KEYWORDS:
        if kw.lower() in header_clean:
            return True
    return False


def _is_confusion_column(values: list[str], header: Optional[str] = None) -> bool:
    """
    判断一列值是否属于 OCR 误识别（混淆列）。

    判断流程：
        1. 如果列标题包含关键词 → 直接进入值验证
        2. 如果列标题无关键词 → 检查单字符比例是否 ≥ 阈值
        3. 通过上述筛选后，验证所有值是否都属于混淆集
        4. 检查互异值数量是否在合理范围内

    参数：
        values: 该列所有单元格的文本值列表
        header: 该列的标题文本（可选）

    返回：
        bool: 如果该列疑似混淆列返回 True

    示例：
        # 列标题为"类型"，值为 ["1", "1", "0", "二", "1"]
        >>> _is_confusion_column(["1", "1", "0", "二", "1"], "类型")
        True

        # 列标题为"编号"，值为 ["1", "2", "3", "4", "5"]
        # → 虽然是单字符，但有 5 种互异值，超过阈值 → False
        >>> _is_confusion_column(["1", "2", "3", "4", "5"], "编号")
        False
    """
    # ======================================================================
    # 第一步：过滤空列
    # ======================================================================
    # 过滤掉空字符串单元格，避免影响统计
    non_empty = [v for v in values if v]
    if not non_empty:
        return False

    # ======================================================================
    # 第二步：筛选条件判断
    # ======================================================================
    # 条件 A：列标题包含关键词
    has_keyword = _has_header_keyword(header)

    # 条件 B：列中单字符值的比例
    single_char_count = sum(1 for v in non_empty if len(v) == 1)
    single_char_ratio = single_char_count / len(non_empty) if non_empty else 0

    # 需要至少满足条件 A 或 B 之一
    if not has_keyword and single_char_ratio < SINGLE_CHAR_RATIO_THRESHOLD:
        return False

    # ======================================================================
    # 第三步：值域验证
    # ======================================================================
    # 提取所有单字符值，检查它们是否都属于混淆集
    single_chars = [v for v in non_empty if len(v) == 1]
    if not single_chars:
        return False

    # 所有单字符值都必须属于混淆集 {0, 1, 二, I, O, —}
    all_in_confusion_set = all(v in CONFUSION_SET for v in single_chars)
    if not all_in_confusion_set:
        return False

    # ======================================================================
    # 第四步：互异值数量验证
    # ======================================================================
    # 混淆列的值种类数应该很小（I/O/— 最多 3 种，加误识别最多 6 种）
    # 如果是数字编号列（如 1,2,3,4,5...）会有很多互异值，应排除
    unique_count = len(set(non_empty))
    if unique_count > MAX_UNIQUE_VALUES:
        return False

    return True


# =============================================================================
# 值修正函数
# =============================================================================

def correct_table_cell(value: str) -> str:
    """
    修正单个表格单元格的 OCR 误识别。

    说明：
        仅当该值完全匹配混淆映射表中的 key 时才修正。
        非匹配值原样返回，不会做任何修改。

    参数：
        value: 原始的 OCR 识别文本

    返回：
        str: 修正后的文本

    示例：
        >>> correct_table_cell("1")
        'I'
        >>> correct_table_cell("OUT")
        'OUT'
        >>> correct_table_cell("正输入")
        '正输入'
    """
    # 仅在精确匹配时进行替换
    return OCR_CONFUSION_MAP.get(value, value)


def _correct_column_values(values: list[str]) -> list[str]:
    """
    对一列中的所有值执行 OCR 修正。

    参数：
        values: 该列的原始值列表

    返回：
        list[str]: 修正后的值列表
    """
    return [correct_table_cell(v) for v in values]


# =============================================================================
# Markdown 表格处理
# =============================================================================

def _parse_table_structure(table_lines: list[str]) -> dict:
    """
    解析 markdown 表格的结构信息（标题行和各列的值）。

    说明：
        此函数仅提取结构信息用于列级混淆检测，不关心原始格式。
        表格的修正会在 fix_markdown_tables 中直接对原文做字符串替换。

    参数：
        table_lines: markdown 表格的每一行文本

    返回：
        dict: {
            "headers": [str, ...],         # 表头（可能为空）
            "column_values": [[str,...],...],  # 每列的所有数据行值
            "data_start_idx": int,          # 第一条数据行在 table_lines 中的索引
            "data_end_idx": int,            # 最后一条数据行在 table_lines 中的索引
        }
    """
    headers: list[str] = []
    column_values: list[list[str]] = []
    data_start_idx = -1
    data_end_idx = -1
    row_idx = 0  # 当前行在 table_lines 中的索引

    for line in table_lines:
        stripped = line.strip()
        if not stripped:
            row_idx += 1
            continue

        # 跳过分隔行（|---|---|）
        sep_pattern = re.sub(r"[|\-:\s]", "", stripped)
        if not sep_pattern and "|" in stripped:
            row_idx += 1
            continue

        # 解析单元格
        cells_str = stripped.strip("|")
        cells = [c.strip() for c in cells_str.split("|")]

        if not headers:
            # 第一个非空、非分隔行是表头
            headers = cells
            column_values = [[] for _ in cells]
        else:
            # 数据行：将值加入对应列
            for ci, cell in enumerate(cells):
                if ci < len(column_values):
                    column_values[ci].append(cell)
            if data_start_idx == -1:
                data_start_idx = row_idx
            data_end_idx = row_idx

        row_idx += 1

    return {
        "headers": headers,
        "column_values": column_values,
        "data_start_idx": data_start_idx,
        "data_end_idx": data_end_idx,
    }


def _replace_cell_in_line(
    line: str,
    col_index: int,
    old_value: str,
    new_value: str,
) -> str:
    """
    在原始表格行文本中，替换指定列的单元格内容。

    原理：
        按 | 分割行文本，定位指定列的单元格文本段，
        将其中匹配 old_value 的部分替换为 new_value。
        这样能完整保留原始行中的空格对齐、前后缀等格式。

    参数：
        line:      原始行文本（如 "| IN+ | 3 | 4 | 1 | 正输入 |"）
        col_index: 列索引（从 0 开始）
        old_value: 原值（混淆的 OCR 结果）
        new_value: 修正后的值

    返回：
        str: 替换后的行文本
    """
    parts = line.split("|")
    # 列 col_index 对应 parts 中的索引 col_index + 1
    # 因为 parts[0] 是行首（可能是空格或空字符串）
    target_idx = col_index + 1

    if target_idx < len(parts):
        cell_text = parts[target_idx]
        # 在单元格文本中执行精确替换（仅替换第一个匹配）
        new_cell_text = cell_text.replace(old_value, new_value, 1)
        parts[target_idx] = new_cell_text

    return "|".join(parts)


def fix_markdown_tables(md_content: str) -> str:
    """
    对 markdown 内容中的所有表格执行 OCR 后处理修正。

    处理流程：
        1. 按行扫描，识别出所有连续的表格块
        2. 对每个表格块：
           a. 解析结构，提取表头和各列的数据值
           b. 逐列判断是否为混淆列
           c. 对确认的混淆列，在原始行文本中直接替换单元格内容
        3. 非表格内容完全不变

    参数：
        md_content: 原始 markdown 文本

    返回：
        str: 修正后的 markdown 文本

    说明：
        - 仅修正表格内容，段落/标题/列表等完全不变
        - 替换在原始行文本中完成，保留空格对齐等原始格式
    """
    lines = md_content.split("\n")
    result_lines: list[str] = []
    table_buffer: list[str] = []
    in_table = False

    # ======================================================================
    # 内部函数：flush_table — 处理累积的表格行
    # ======================================================================
    def flush_table():
        """
        处理缓冲区中累积的表格行。

        逻辑：
            遍历表格的每一行，跳过表头行和分隔行，对每个数据行，
            如果该行某一列是混淆列，则在原文中替换该单元格的内容。
        """
        nonlocal table_buffer
        if not table_buffer:
            return

        try:
            # ================================================================
            # 第一步：解析表格结构，获取各列的值集合用于混淆检测
            # ================================================================
            struct = _parse_table_structure(table_buffer)
            if not struct["column_values"]:
                # 解析失败，保留原始内容
                result_lines.extend(table_buffer)
                table_buffer = []
                return

            num_cols = len(struct["column_values"])

            # ================================================================
            # 第二步：逐列判断是否为混淆列
            # ================================================================
            is_confusion: list[bool] = []
            for ci in range(num_cols):
                header = struct["headers"][ci] if ci < len(struct["headers"]) else None
                is_confusion.append(
                    _is_confusion_column(struct["column_values"][ci], header)
                )

            # 现在非混淆列也可能通过 PIN_TYPE_RULES 匹配获得修正
            # （如 NC→SOIC="二" → 规则匹配 → —），不再提前返回。

            # ================================================================
            # 第三步：在原始行文本中逐行替换
            # ================================================================
            # 策略：复制缓冲区，对于每行，找到其在 column_values 中对应的
            #       数据行索引，然后对其中的混淆列单元格执行替换。
            #       column_values[ci][data_row_idx] 是第 ci 列第 data_row_idx
            #       个数据行的值，也是我们替换的旧值。
            corrected_lines = list(table_buffer)
            data_row_idx = 0  # 当前处理到第几条数据行

            for li, line in enumerate(corrected_lines):
                stripped = line.strip()
                if not stripped:
                    continue

                # 跳过分隔行（|---|---|）
                sep_pattern = re.sub(r"[|\-:\s]", "", stripped)
                if not sep_pattern and "|" in stripped:
                    continue

                # 跳过表头行
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if cells == struct["headers"]:
                    continue

                # 至此，line 是一条数据行
                # 获取第一列（引脚名称）的值，用于消歧
                pin_name = ""
                if len(struct["column_values"]) > 0:
                    pin_name = struct["column_values"][0][data_row_idx]

                # 对所有列都尝试修正，非混淆列不走 fallback（避免误伤合法数字）
                for ci in range(num_cols):
                    # 第一列（引脚名称）不做任何修正，保持原值
                    if ci == 0:
                        continue
                    # 获取该行该列的原始值
                    if data_row_idx < len(struct["column_values"][ci]):
                        old_val = struct["column_values"][ci][data_row_idx]
                        new_val = _apply_cell_correction(
                            pin_name, old_val, allow_fallback=is_confusion[ci]
                        )
                        if old_val != new_val:
                            # 在原始行文本中进行精确替换
                            corrected_lines[li] = _replace_cell_in_line(
                                corrected_lines[li], ci, old_val, new_val
                            )

                data_row_idx += 1

            result_lines.extend(corrected_lines)

        except Exception as e:
            logger.warning("表格解析失败，保留原始内容: %s", e)
            result_lines.extend(table_buffer)

        table_buffer = []

    # ======================================================================
    # 主扫描循环
    # ======================================================================
    for line in lines:
        # 判断是否为表格行：以 | 开头且包含 |
        is_table_line = line.strip().startswith("|") and "|" in line

        if is_table_line:
            table_buffer.append(line)
            in_table = True
        else:
            if in_table:
                # 表格结束，处理缓冲区的表格
                flush_table()
                in_table = False
            result_lines.append(line)

    # 处理文件末尾可能残留的表格
    if table_buffer:
        flush_table()

    return "\n".join(result_lines)


def fix_markdown_file(input_path: str, output_path: Optional[str] = None) -> str:
    """
    读取 markdown 文件，对其中的表格执行 OCR 修正，并保存结果。

    支持两种表格格式：
      1. Markdown 管道表格（| ... |）— 由 fix_markdown_tables 处理
      2. HTML 表格（<table>...</table>）— 由 fix_html_table 处理
         MinerU pipeline 后端输出的表格为 HTML 格式。

    参数：
        input_path:  输入 markdown 文件路径
        output_path: 输出文件路径。如果为 None，则覆盖原文件

    返回：
        str: 修正后的 markdown 内容

    示例：
        >>> fix_markdown_file("output.md")              # 覆盖原文件
        >>> fix_markdown_file("output.md", "fixed.md")  # 保存为新文件
    """
    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 第一步：处理 markdown 管道表格（| ... |）
    fixed_content = fix_markdown_tables(content)

    # 第二步：处理 HTML 表格（<table>...</table>）
    # MinerU pipeline 后端输出的表格是 HTML 格式，嵌入在 markdown 中。
    # 使用正则匹配每个 <table> 块，逐个调用 fix_html_table 修复。
    def _fix_html_block(match: re.Match) -> str:
        return fix_html_table(match.group(0))

    fixed_content = re.sub(
        r"<table[^>]*>.*?</table>",
        _fix_html_block,
        fixed_content,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 第三步：展开 rowspan 合并单元格
    # 将纵向合并的单元格展开，使每一行都获得合并单元格的内容副本
    from post_table.expand_rowspan import expand_rowspan
    fixed_content = expand_rowspan(fixed_content)

    # 第四步：展开 colspan 合并单元格
    # 将横向合并的单元格展开，使每一列都获得合并单元格的内容副本
    from post_table.expand_rowspan import expand_colspan
    fixed_content = expand_colspan(fixed_content)

    # 第五步：修复 tms320c6211b Terminal Functions 表格的前两列合并错误
    # 该问题发生在 colspan 展开之后：部分续表的前两列本应是
    # SIGNAL NAME / NO.，但模型输出为重复的 "SIGNAL PIN"。
    fixed_content = tms320c6211b_TerminalFunctions_table(fixed_content)

    out_path = output_path or input_path
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(fixed_content)

    logger.info("表格 OCR 修正完成: %s → %s", input_path, out_path)
    return fixed_content


# =============================================================================
# HTML 表格处理
# =============================================================================

def _parse_html_table(html: str) -> list[list[str]]:
    """
    解析 HTML 表格，提取行/列数据（支持 colspan 展开）。

    说明：
        使用正则表达式提取 <table> 中的 <tr> 和 <td>/<th> 内容。
        这是一个轻量级解析器，不依赖第三方 HTML 解析库。
        正确处理 colspan 属性，将跨列单元格展开为多列。
        rowspan 暂不处理（后续再用空字符串补齐会破坏对齐，暂不处理）。

    参数：
        html: HTML 表格字符串

    返回：
        list[list[str]]: 二维表格数据，每行展开 colspan 后的逻辑列数
    """
    rows: list[list[str]] = []

    # 提取所有 <tr> 块
    tr_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    # 提取 <tr> 中的 <td> 或 <th> 块
    cell_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)

    for tr_match in tr_pattern.finditer(html):
        tr_content = tr_match.group(1)
        row: list[str] = []
        for cell_match in cell_pattern.finditer(tr_content):
            cell_html = cell_match.group(0)   # 完整标签，用于解析属性
            cell_text = cell_match.group(1)   # 标签内的内容
            # 去除内部 HTML 标签，仅保留纯文本
            cell_text = re.sub(r"<[^>]+>", "", cell_text)
            cell_text = cell_text.strip()

            # 解析 colspan 属性（将跨列单元格展开为多列）
            colspan = 1
            cs_match = re.search(
                r'colspan\s*=\s*["\']?(\d+)["\']?', cell_html, re.IGNORECASE
            )
            if cs_match:
                colspan = int(cs_match.group(1))

            # 按 colspan 展开：<td colspan=3>XXX</td> → [XXX, XXX, XXX]
            for _ in range(colspan):
                row.append(cell_text)

        if row:
            rows.append(row)

    return rows


def fix_html_table(html: str) -> str:
    """
    对 HTML 表格中的内容执行 OCR 后处理修正。

    两步策略：
      1. 用 _parse_html_table 解析并展开 colspan，做列级混淆检测
      2. 在原始 HTML 中按 <td>/<th> 标签边界替换混淆列单元格的文本
         使用 col_offset + colspan 跟踪逻辑列位置，避免误伤 HTML 属性

    参数：
        html: 原始的 HTML 表格字符串

    返回：
        str: 修正后的 HTML 表格字符串
    """
    # ======================================================================
    # 第一步：解析并检测混淆列
    # ======================================================================
    rows = _parse_html_table(html)
    if not rows:
        return html

    num_cols = max(len(row) for row in rows)
    if num_cols == 0:
        return html

    cols_values: list[list[str]] = [[] for _ in range(num_cols)]
    for row in rows:
        for ci in range(num_cols):
            if ci < len(row):
                cols_values[ci].append(row[ci])

    is_confusion: list[bool] = []
    for ci in range(num_cols):
        header = rows[0][ci] if rows and ci < len(rows[0]) else None
        is_confusion.append(_is_confusion_column(cols_values[ci], header))

    # 不提前返回：非混淆列也可能通过 PIN_TYPE_RULES 匹配获得修正
    # （如 NC→SOIC="二" → 规则匹配 → —），所以即使 is_confusion 全为 False
    # 也需要走 _fix_row 让 _apply_cell_correction 有机会匹配规则。

    # ======================================================================
    # 第二步：在原始 HTML 中按标签边界做替换
    # ======================================================================
    tr_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(
        r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL | re.IGNORECASE
    )

    def _fix_row(tr_match: re.Match) -> str:
        """处理单个 <tr>，修正其所有混淆列单元格"""
        tr_html = tr_match.group(0)
        tr_content = tr_match.group(1)

        # 逐段拼接 row 内容（非单元格文本 + 单元格HTML）
        parts: list[str] = []
        last_end = 0
        col_offset = 0
        pin_name = ""  # 第一列（引脚名称）的值，用于消歧

        for cell_match in cell_pattern.finditer(tr_content):
            # 1) 保留单元格之间的原始文本（换行、空白等）
            parts.append(tr_content[last_end : cell_match.start()])

            tag_open = cell_match.group(1)
            inner = cell_match.group(2)
            tag_close = cell_match.group(3)

            # 解析 colspan
            colspan = 1
            cs = re.search(
                r'colspan\s*=\s*["\']?(\d+)["\']?', tag_open, re.IGNORECASE
            )
            if cs:
                colspan = int(cs.group(1))

            # 第一列的值作为 pin_name（用于后续列的消歧）
            if col_offset == 0:
                pin_name = inner.strip()

            # 2) 修正或保留当前单元格
            # 第一列（引脚名称）不做任何修正，保持原值
            if col_offset == 0:
                parts.append(cell_match.group(0))
            # 对所有列都调用修正，但区分是否允许 fallback：
            #   - 混淆列(is_confusion=True) → 规则匹配 + fallback 全开
            #   - 非混淆列(is_confusion=False) → 只走规则匹配，不走 fallback
            #     避免把引脚编号列的合法数字（如 1）误换成 I
            elif col_offset < len(is_confusion):
                new_inner = _apply_cell_correction(
                    pin_name, inner, allow_fallback=is_confusion[col_offset]
                )
                if new_inner != inner:
                    parts.append(tag_open + new_inner + tag_close)
                else:
                    parts.append(cell_match.group(0))
            else:
                parts.append(cell_match.group(0))

            col_offset += colspan
            last_end = cell_match.end()

        # 3) 最后一个单元格之后的文本
        parts.append(tr_content[last_end:])

        new_content = "".join(parts)
        return tr_html.replace(tr_content, new_content, 1)

    return tr_pattern.sub(_fix_row, html)


# =============================================================================
# 指定数据手册表格结构修复
# =============================================================================

TERMINAL_FUNCTIONS_PIN_RE = re.compile(r"^(.+?)\s+([A-Z]{1,2}\d{1,2})$")
TERMINAL_FUNCTIONS_TYPE_RE = re.compile(
    r"^(I|O|S|GND|A|I/O/Z|O/Z|I/O|A¶|A§|—|-)$",
    re.IGNORECASE,
)


def _html_cell_plain_text(inner_html: str) -> str:
    """提取单元格纯文本，仅用于规则判断，不用于最终输出。"""
    text = re.sub(r"<[^>]+>", "", inner_html)
    return re.sub(r"\s+", " ", text).strip()


def _parse_html_table_cells(html: str) -> list[list[dict]]:
    """
    将 HTML 表格解析成可回写的 cell 结构。

    每个单元格保留 open/inner/close 三段。后续只替换 inner，
    不改动 <td> / <th> 标签属性，避免破坏表格结构。
    """
    rows: list[list[dict]] = []
    tr_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(
        r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
        re.DOTALL | re.IGNORECASE,
    )

    for tr_match in tr_pattern.finditer(html):
        tr_content = tr_match.group(1)
        row = []
        for cell_match in cell_pattern.finditer(tr_content):
            row.append({
                "open": cell_match.group(1),
                "inner": cell_match.group(2),
                "close": cell_match.group(3),
            })
        if row:
            rows.append(row)
    return rows


def _is_terminal_functions_section_row(cells: list[dict]) -> bool:
    """
    判断是否为 Terminal Functions 表格的分组标题行。

    这类行是正常的 colspan 展开结果，例如：
        EMIF - ADDRESS | EMIF - ADDRESS | ... | EMIF - ADDRESS

    用户需要保留这种展开重复，所以不能把它拆成两列。
    """
    texts = [_html_cell_plain_text(cell["inner"]) for cell in cells]
    non_empty = [text for text in texts if text]
    if len(non_empty) < 3:
        return False
    return len(set(non_empty)) == 1


def _has_signal_name_no_header(rows: list[list[dict]]) -> bool:
    """
    判断表格是否具有标准化后的两行 SIGNAL NAME / NO. 表头：

        SIGNAL | SIGNAL | ...
        NAME   | NO.    | ...

    只要命中该表头，就允许后续逐行检查并拆分重复的 SIGNAL/PIN 单元格。
    不再依赖固定 5 列、TYPE 列或 IPD/IPU 列，因此同样支持 4 列表格。
    """
    if len(rows) < 2 or len(rows[0]) < 2 or len(rows[1]) < 2:
        return False

    first_row = [
        _html_cell_plain_text(cell["inner"]).upper().rstrip(".")
        for cell in rows[0][:2]
    ]
    second_row = [
        _html_cell_plain_text(cell["inner"]).upper().rstrip(".")
        for cell in rows[1][:2]
    ]
    return first_row == ["SIGNAL", "SIGNAL"] and second_row == ["NAME", "NO"]


def _normalize_signal_name_no_header(html: str) -> str:
    """
    将模型合并输出的 SIGNAL NAME NO. 表头改成两行逻辑表头。

    输入：
        SIGNAL NAME NO. | SIGNAL NAME NO. | TYPE | DESCRIPTION

    输出：
        SIGNAL | SIGNAL | TYPE | DESCRIPTION
        NAME   | NO.    | TYPE | DESCRIPTION

    后面的列按 rowspan 展开语义复制到两行，保持整张表列数一致。
    """
    rows = _parse_html_table_cells(html)
    if not rows or len(rows[0]) < 2:
        return html

    if _has_signal_name_no_header(rows):
        return html

    first_two = [
        _html_cell_plain_text(cell["inner"]).upper().replace(".", "")
        for cell in rows[0][:2]
    ]
    if not all(
        all(keyword in text for keyword in ("SIGNAL", "NAME", "NO"))
        for text in first_two
    ):
        return html

    tr_match = re.search(
        r"(<tr[^>]*>)(.*?)(</tr>)",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if tr_match is None:
        return html

    header_cells = rows[0]
    first_row_cells = []
    second_row_cells = []
    for index, cell in enumerate(header_cells):
        if index == 0:
            first_inner, second_inner = "SIGNAL", "NAME"
        elif index == 1:
            first_inner, second_inner = "SIGNAL", "NO."
        else:
            first_inner = second_inner = cell["inner"]
        first_row_cells.append(cell["open"] + first_inner + cell["close"])
        second_row_cells.append(cell["open"] + second_inner + cell["close"])

    first_row = tr_match.group(1) + "".join(first_row_cells) + tr_match.group(3)
    second_row = tr_match.group(1) + "".join(second_row_cells) + tr_match.group(3)
    return html[:tr_match.start()] + first_row + second_row + html[tr_match.end():]


def _fix_terminal_functions_table_html(html: str) -> str:
    """
    修复单个 Terminal Functions HTML 表格中前两列重复的问题。

    错误形态：
        HOLDA J18 | HOLDA J18 | O | IPU | ...

    修复为：
        HOLDA | J18 | O | IPU | ...

    分组标题行保持不动：
        EMIF - ADDRESS | EMIF - ADDRESS | ...
    """
    html = _normalize_signal_name_no_header(html)
    rows = _parse_html_table_cells(html)
    if not _has_signal_name_no_header(rows):
        return html

    tr_pattern = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(
        r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
        re.DOTALL | re.IGNORECASE,
    )

    rebuilt_parts: list[str] = []
    last_end = 0
    row_index = 0
    fixed_count = 0

    for tr_match in tr_pattern.finditer(html):
        rebuilt_parts.append(html[last_end:tr_match.start()])

        tr_open = tr_match.group(1)
        tr_content = tr_match.group(2)
        tr_close = tr_match.group(3)
        cells = rows[row_index] if row_index < len(rows) else []
        row_index += 1

        # 跳过两行标准表头、短行、分组标题行。
        if row_index <= 2 or len(cells) < 2 or _is_terminal_functions_section_row(cells):
            rebuilt_parts.append(tr_match.group(0))
            last_end = tr_match.end()
            continue

        col0 = _html_cell_plain_text(cells[0]["inner"])
        col1 = _html_cell_plain_text(cells[1]["inner"])
        split_match = TERMINAL_FUNCTIONS_PIN_RE.match(col0)

        if not (split_match and col0 == col1):
            rebuilt_parts.append(tr_match.group(0))
            last_end = tr_match.end()
            continue

        signal_name = split_match.group(1).strip()
        pin_no = split_match.group(2).strip()
        cell_idx = 0

        def _replace_first_two_cells(cell_match: re.Match) -> str:
            nonlocal cell_idx
            cell_idx += 1
            if cell_idx == 1:
                return cell_match.group(1) + signal_name + cell_match.group(3)
            if cell_idx == 2:
                return cell_match.group(1) + pin_no + cell_match.group(3)
            return cell_match.group(0)

        new_content = cell_pattern.sub(_replace_first_two_cells, tr_content)
        rebuilt_parts.append(tr_open + new_content + tr_close)
        fixed_count += 1
        last_end = tr_match.end()

    rebuilt_parts.append(html[last_end:])
    if fixed_count:
        logger.info("tms320c6211b Terminal Functions 表格修复: 拆分前两列 %d 行", fixed_count)
    return "".join(rebuilt_parts)


def tms320c6211b_TerminalFunctions_table(md_content: str) -> str:
    """
    修复 SIGNAL NAME / NO. 表格的表头和前两列合并错误。

    命名按“文件名_表名_table”组织，便于以后继续追加同类专项修复。

    命中规则：
        先将合并的 SIGNAL NAME NO. 表头标准化为：
            SIGNAL | SIGNAL
            NAME   | NO.
        只要表格具有这个表头就命中，不限制 4 列或 5 列，也不要求
        存在 IPD/IPU 列。

    数据行处理：
        前两列重复的 "SIGNAL PIN"，例如：
            HOLDA J18 | HOLDA J18 | O | IPU | ...
        拆分为 HOLDA | J18。分组标题行的展开重复保持不动。
    """
    def _fix_html_block(match: re.Match) -> str:
        return _fix_terminal_functions_table_html(match.group(0))

    return re.sub(
        r"<table[^>]*>.*?</table>",
        _fix_html_block,
        md_content,
        flags=re.DOTALL | re.IGNORECASE,
    )


# =============================================================================
# 便捷工具函数
# =============================================================================

def get_column_values(rows: list[list[str]], col_index: int) -> list[str]:
    """
    从二维表格数据中提取指定列的所有值。

    参数：
        rows:      二维表格数据（行列表）
        col_index: 列索引（从 0 开始）

    返回：
        list[str]: 该列所有行的值
    """
    return [row[col_index] for row in rows if col_index < len(row)]


def print_confusion_analysis(values: list[str], header: Optional[str] = None) -> None:
    """
    打印一列值的 OCR 混淆分析结果（用于调试）。

    参数：
        values: 列的原始值
        header: 列标题（可选）
    """
    is_conf = _is_confusion_column(values, header)
    status = "✅ 是混淆列" if is_conf else "❌ 不是混淆列"
    print(f"  [{status}] header={header!r}, values={values}")

    if is_conf:
        corrected = _correct_column_values(values)
        print(f"    修正后: {corrected}")


# =============================================================================
# 命令行入口（调试用）
# =============================================================================
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("用法: python fix_ocr_table.py <input.md> [output.md]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    fix_markdown_file(input_path, output_path)
