"""
=============================================================================
HTML 表格 Rowspan / Colspan 展开模块
=============================================================================

MinerU 解析表格时，对于合并单元格（rowspan / colspan），只在起始位置输出
内容，被合并的位置为空或省略。本模块将合并单元格展开，使每个逻辑位置都
获得完整的内容副本。

Rowspan 示例：
    输入：
        <tr><td rowspan=3>A</td><td>1</td></tr>
        <tr><td>2</td></tr>
        <tr><td>3</td></tr>

    输出：
        <tr><td>A</td><td>1</td></tr>
        <tr><td>A</td><td>2</td></tr>
        <tr><td>A</td><td>3</td></tr>

Colspan 示例：
    输入：
        <tr><td colspan=3>标题</td></tr>
        <tr><td>子1</td><td>子2</td><td>子3</td></tr>

    输出：
        <tr><td>标题</td><td>标题</td><td>标题</td></tr>
        <tr><td>子1</td><td>子2</td><td>子3</td></tr>

使用方式：
    from post_table.expand_rowspan import expand_rowspan, expand_colspan
    result = expand_rowspan(html_table_string)
    result = expand_colspan(result)
=============================================================================
"""

import re
import logging

logger = logging.getLogger(__name__)


def expand_rowspan(html: str) -> str:
    """
    展开 HTML 表格中的 rowspan 合并单元格。

    算法：
        1. 解析所有 <tr> 行
        2. 维护 rowspan_tracker 字典：{逻辑列索引: (剩余行数, 单元格HTML)}
        3. 对每一行：
           - 先处理 rowspan_tracker，在对应列位置插入之前行的 rowspan 单元格
           - 处理当前行的 <td> 时，若 rowspan>1，记录到 tracker
           - 移除已展开单元格的 rowspan 属性
        4. 重新拼接 HTML 输出

    参数：
        html: 包含 <table> 的 HTML 字符串（可以是整个 markdown 中的一段）

    返回：
        str: rowspan 展开后的 HTML
    """
    # 匹配 <table>...</table> 块
    table_pattern = re.compile(
        r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE
    )

    def process_table(table_match: re.Match) -> str:
        return _expand_rowspan_in_table(table_match.group(0))

    return table_pattern.sub(process_table, html)


def _expand_rowspan_in_table(table_html: str) -> str:
    """
    处理单个 <table> 块，展开其中的 rowspan。

    参数：
        table_html: 单个 <table>...</table> 的 HTML 字符串

    返回：
        str: rowspan 展开后的 <table> HTML
    """
    # 提取 <tr> 行
    tr_pattern = re.compile(
        r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE
    )
    # 提取 <td> 或 <th> 单元格
    cell_pattern = re.compile(
        r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL | re.IGNORECASE
    )

    # rowspan 追踪器：{逻辑列索引: (剩余延续行数, 单元格HTML不含rowspan属性)}
    rowspan_tracker: dict[int, tuple[int, str]] = {}

    result_rows: list[str] = []

    for tr_match in tr_pattern.finditer(table_html):
        tr_open = tr_match.group(1)    # <tr ...>
        tr_content = tr_match.group(2) # 行内容
        tr_close = tr_match.group(3)   # </tr>

        # 构建新行的单元格列表
        new_cells: list[str] = []
        logical_col = 0  # 当前逻辑列索引

        # 第一步：处理当前行的 <td>，同时考虑 rowspan_tracker 中需要插入的单元格
        cells_in_row = list(cell_pattern.finditer(tr_content))
        cell_idx = 0

        while cell_idx < len(cells_in_row) or logical_col in rowspan_tracker:
            # 检查是否有 rowspan 单元格需要在此列插入
            if logical_col in rowspan_tracker:
                remaining, cell_html, cell_colspan = rowspan_tracker[logical_col]
                # 插入之前行的 rowspan 单元格内容（如果 cell_html 不是 None）
                if cell_html is not None:
                    new_cells.append(cell_html)
                # 递减计数
                if remaining > 1:
                    rowspan_tracker[logical_col] = (remaining - 1, cell_html, cell_colspan)
                else:
                    del rowspan_tracker[logical_col]
                # 跳过被 colspan 覆盖的列
                logical_col += cell_colspan
                continue

            # 处理当前行的单元格
            if cell_idx < len(cells_in_row):
                cell_match = cells_in_row[cell_idx]
                tag_open = cell_match.group(1)
                inner = cell_match.group(2)
                tag_close = cell_match.group(3)

                # 解析 rowspan
                rowspan = 1
                rs_match = re.search(
                    r'rowspan\s*=\s*["\']?(\d+)["\']?', tag_open, re.IGNORECASE
                )
                if rs_match:
                    rowspan = int(rs_match.group(1))

                # 解析 colspan（用于计算逻辑列偏移）
                colspan = 1
                cs_match = re.search(
                    r'colspan\s*=\s*["\']?(\d+)["\']?', tag_open, re.IGNORECASE
                )
                if cs_match:
                    colspan = int(cs_match.group(1))

                # 移除 rowspan 属性（展开后不再需要）
                new_tag_open = _remove_rowspan_attr(tag_open)

                # 构建单元格 HTML
                cell_html = new_tag_open + inner + tag_close

                if rowspan > 1:
                    # 记录到 tracker，为后续行提供内容
                    # 存储: (剩余行数, 单元格HTML, colspan值)
                    tracker_html = new_tag_open + inner + tag_close
                    rowspan_tracker[logical_col] = (rowspan - 1, tracker_html, colspan)

                new_cells.append(cell_html)
                logical_col += colspan
                cell_idx += 1
            else:
                # 没有更多单元格了，跳出
                break

        # 拼接新行
        new_row_content = "".join(new_cells)
        result_rows.append(tr_open + new_row_content + tr_close)

    # 重新拼接表格
    # 保留 <table> 开标签和 </table> 闭标签
    table_open_match = re.match(r"(<table[^>]*>)", table_html, re.IGNORECASE)
    table_close_match = re.search(r"(</table>)\s*$", table_html, re.IGNORECASE)

    if table_open_match and table_close_match:
        table_open = table_open_match.group(1)
        table_close = table_close_match.group(1)
        return table_open + "".join(result_rows) + table_close
    else:
        # 异常情况，返回原始内容
        logger.warning("无法解析表格结构，保留原始 HTML")
        return table_html


def _remove_rowspan_attr(tag_open: str) -> str:
    """
    从 <td> 或 <th> 开标签中移除 rowspan 属性。

    参数：
        tag_open: 如 '<td rowspan=3 colspan=1>'

    返回：
        str: 如 '<td colspan=1>'
    """
    # 匹配 rowspan='N' 或 rowspan="N" 或 rowspan=N
    # 注意处理前后可能有空格
    result = re.sub(
        r'\s*rowspan\s*=\s*["\']?\d+["\']?',
        '',
        tag_open,
        flags=re.IGNORECASE
    )
    return result


# ============================================================================
# Colspan 展开
# ============================================================================


def expand_colspan(html: str) -> str:
    """
    展开 HTML 表格中的 colspan 合并单元格。

    算法：
        1. 解析所有 <tr> 行
        2. 对每一行中的 <td>/<th>：
           - 若 colspan=N (N>1)，将该单元格复制 N 份，移除 colspan 属性
           - 否则保留原样
        3. 重新拼接 HTML 输出

    参数：
        html: 包含 <table> 的 HTML 字符串（可以是整个 markdown 中的一段）

    返回：
        str: colspan 展开后的 HTML
    """
    table_pattern = re.compile(
        r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE
    )

    def process_table(table_match: re.Match) -> str:
        return _expand_colspan_in_table(table_match.group(0))

    return table_pattern.sub(process_table, html)


def _expand_colspan_in_table(table_html: str) -> str:
    """
    处理单个 <table> 块，展开其中的 colspan。

    参数：
        table_html: 单个 <table>...</table> 的 HTML 字符串

    返回：
        str: colspan 展开后的 <table> HTML
    """
    tr_pattern = re.compile(
        r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE
    )
    cell_pattern = re.compile(
        r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL | re.IGNORECASE
    )

    result_rows: list[str] = []

    for tr_match in tr_pattern.finditer(table_html):
        tr_open = tr_match.group(1)
        tr_content = tr_match.group(2)
        tr_close = tr_match.group(3)

        new_cells: list[str] = []
        expanded_cell_values: list[str] = []
        original_cells: list[tuple[str, str, str, int]] = []

        for cell_match in cell_pattern.finditer(tr_content):
            tag_open = cell_match.group(1)
            inner = cell_match.group(2)
            tag_close = cell_match.group(3)

            # 解析 colspan
            colspan = 1
            cs_match = re.search(
                r'colspan\s*=\s*["\']?(\d+)["\']?', tag_open, re.IGNORECASE
            )
            if cs_match:
                colspan = int(cs_match.group(1))

            original_cells.append((tag_open, inner, tag_close, colspan))

            if colspan > 1:
                # 移除 colspan 属性，生成干净的单元格标签
                clean_tag = _remove_colspan_attr(tag_open)
                cell_html = clean_tag + inner + tag_close
                # 复制 colspan 份
                for _ in range(colspan):
                    new_cells.append(cell_html)
                    expanded_cell_values.append(_plain_cell_text(inner))
            else:
                # 无 colspan，直接保留
                new_cells.append(tag_open + inner + tag_close)
                expanded_cell_values.append(_plain_cell_text(inner))

        if _is_same_content_group_row(expanded_cell_values):
            new_row_content = _build_group_row_cells(original_cells, len(expanded_cell_values))
        else:
            new_row_content = "".join(new_cells)
        result_rows.append(tr_open + new_row_content + tr_close)

    # 重新拼接表格
    table_open_match = re.match(r"(<table[^>]*>)", table_html, re.IGNORECASE)
    table_close_match = re.search(r"(</table>)\s*$", table_html, re.IGNORECASE)

    if table_open_match and table_close_match:
        table_open = table_open_match.group(1)
        table_close = table_close_match.group(1)
        return table_open + "".join(result_rows) + table_close
    else:
        logger.warning("无法解析表格结构，保留原始 HTML")
        return table_html


def _remove_colspan_attr(tag_open: str) -> str:
    """
    从 <td> 或 <th> 开标签中移除 colspan 属性。

    参数：
        tag_open: 如 '<td colspan=3 class="x">'

    返回：
        str: 如 '<td class="x">'
    """
    result = re.sub(
        r'\s*colspan\s*=\s*["\']?\d+["\']?',
        '',
        tag_open,
        flags=re.IGNORECASE
    )
    return result


def _plain_cell_text(inner: str) -> str:
    """Return normalized visible text for deciding whether a row is a group title."""
    text = re.sub(r"<br\s*/?>", " ", inner, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _is_same_content_group_row(values: list[str]) -> bool:
    """
    True when colspan expansion would create a row full of identical text.

    Those rows are usually sub-table headings such as "PCI INTERFACE". They
    should remain a single spanning cell instead of being duplicated across
    every column.
    """
    non_empty = [value for value in values if value]
    return len(values) > 1 and bool(non_empty) and len(set(non_empty)) == 1


def _build_group_row_cells(
    original_cells: list[tuple[str, str, str, int]],
    expanded_width: int,
) -> str:
    """
    Build one spanning cell for a same-content group row.

    If the original row already had a single colspan cell, keep it unchanged.
    If prior processing already duplicated the same text into several cells,
    collapse it back into one colspan cell.
    """
    if not original_cells:
        return ""
    if len(original_cells) == 1 and original_cells[0][3] > 1:
        tag_open, inner, tag_close, _colspan = original_cells[0]
        return tag_open + inner + tag_close

    tag_open, inner, tag_close, _colspan = original_cells[0]
    clean_tag = _remove_colspan_attr(tag_open)
    clean_tag = re.sub(r"<(t[dh])", rf'<\1 colspan="{expanded_width}"', clean_tag, count=1, flags=re.IGNORECASE)
    return clean_tag + inner + tag_close
