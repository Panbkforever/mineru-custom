"""拆分 MinerU 错误合并到同一个 ``<table>`` 中的多个逻辑子表。

处理对象不是普通的跨页续表，而是下面这种结构错误：

1. 一个逻辑子表已经出现表头和数据行；
2. 后面出现一个横跨整表的小节标题行；
3. 小节标题后完整重复前面的单级或多级表头；
4. 重复表头后还有新的数据行。

只有四项证据同时成立才会拆表。仅出现横跨整表的小节标题、仅出现重复
文本、或者表头只重复了一部分，都不会触发拆分。对于没有小节标题的页面
拼接结果，还支持“完整表头再次出现”的保守回退规则。

本模块必须在 ``rowspan/colspan`` 展开之前运行。这样既能使用原始
``colspan`` 判断全宽小节行，也能保留每个单元格的原始 HTML 属性和 ``<br>``。
内部使用 HTML DOM 解析行和单元格，正则表达式只负责在 Markdown 中定位
完整的 ``<table>...</table>`` 外层块，不参与表格结构判断。
"""

from __future__ import annotations

import copy
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)

# 这里只用于确认“重复行像一个字段表头”，不用于判断具体业务表类型。
# 表头仍然必须在同一张表中前后完整重复，关键词本身不能触发拆表。
HEADER_FIELD_RE = re.compile(
    r"\b(?:pin|ball|pad|terminal|signal|name|number|no|type|description|"
    r"function|package|parameter|unit|min|max|typ|nom|i\s*/?\s*o)\b|"
    r"引脚|管脚|端子|信号|名称|编号|类型|描述|功能|封装|参数|单位",
    re.IGNORECASE,
)

MAX_HEADER_ROWS = 4


@dataclass(frozen=True)
class RowInfo:
    """保存一行用于边界判断的结构信息。"""

    tag: Tag
    signature: tuple[str, ...]
    logical_width: int
    direct_cell_count: int
    is_full_width_section: bool


def split_merged_tables_in_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> int:
    """拆分 Markdown 文件中的错误合并表格，返回新增的表格数量。"""

    source_path = Path(input_path)
    target_path = Path(output_path) if output_path is not None else source_path
    content = source_path.read_text(encoding="utf-8")
    fixed_content, split_count = split_merged_tables_in_markdown(content)

    # 没有命中时不重写文件，避免无意义地改变文件时间和 HTML 格式。
    if split_count or target_path != source_path:
        target_path.write_text(fixed_content, encoding="utf-8")

    if split_count:
        logger.info("合并逻辑子表拆分完成: 新增 %d 个 table", split_count)
    return split_count


def split_merged_tables_in_markdown(markdown: str) -> tuple[str, int]:
    """处理 Markdown 中的所有 HTML 表格，返回 ``(内容, 新增表数)``。"""

    split_count = 0

    def replace_table(match: re.Match[str]) -> str:
        nonlocal split_count
        fixed_html, added_tables = split_merged_table_html(match.group(0))
        split_count += added_tables
        return fixed_html

    return TABLE_RE.sub(replace_table, markdown), split_count


def split_merged_table_html(table_html: str) -> tuple[str, int]:
    """拆分单个 HTML 表格，返回 ``(HTML, 新增表数)``。"""

    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    if table is None:
        return table_html, 0

    row_tags = [
        row
        for row in table.find_all("tr")
        if row.find_parent("table") is table
    ]
    if len(row_tags) < 5:
        return table_html, 0

    table_width = max((_logical_row_width(row) for row in row_tags), default=0)
    if table_width < 2:
        return table_html, 0

    rows = [_build_row_info(row, table_width) for row in row_tags]
    boundaries = _find_split_boundaries(rows)
    if not boundaries:
        return table_html, 0

    # 任何 rowspan 跨越拆分点时都不拆。否则原单元格会在其中一个子表中丢失。
    boundaries = [
        boundary
        for boundary in boundaries
        if not _rowspan_crosses_boundary(row_tags, boundary)
    ]
    if not boundaries:
        return table_html, 0

    slices: list[tuple[int, int]] = []
    start = 0
    for boundary in boundaries:
        if boundary > start:
            slices.append((start, boundary))
            start = boundary
    if start < len(row_tags):
        slices.append((start, len(row_tags)))

    if len(slices) < 2 or any(end <= start for start, end in slices):
        return table_html, 0

    rebuilt_tables = [
        _build_table_fragment(table, row_tags[start:end])
        for start, end in slices
    ]
    return "\n\n".join(rebuilt_tables), len(rebuilt_tables) - 1


def _find_split_boundaries(rows: list[RowInfo]) -> list[int]:
    """按“完整重复表头”寻找一个表格内部的所有逻辑分界。"""

    boundaries: list[int] = []
    row_count = len(rows)

    for boundary in range(1, row_count - 1):
        # 主规则从全宽小节标题的下一行开始比较表头；回退规则直接从
        # 当前行比较，用于续页没有再次输出小节标题的情况。
        # 如果当前行紧跟全宽标题，则只能由前一行的主规则处理，避免在
        # “标题”和“重复表头”之间再生成一个只有标题的空表。
        if (
            not rows[boundary].is_full_width_section
            and rows[boundary - 1].is_full_width_section
        ):
            continue
        repeated_header_start = (
            boundary + 1 if rows[boundary].is_full_width_section else boundary
        )
        if repeated_header_start >= row_count - 1:
            continue

        matched_header_length = _match_previous_header_block(
            rows,
            repeated_header_start,
            boundary,
        )
        if matched_header_length == 0:
            continue

        repeated_header_end = repeated_header_start + matched_header_length
        previous_header_start = _find_previous_block_start(
            rows,
            repeated_header_start,
            matched_header_length,
        )
        if previous_header_start is None:
            continue

        previous_header_end = previous_header_start + matched_header_length

        # 前后两段都必须存在真正的数据行。这样不会把连续重复的表头、
        # 只有说明文字的空表或文件末尾残缺表头拆成独立表格。
        if not _contains_data_row(rows, previous_header_end, boundary):
            continue
        if not _contains_data_row(rows, repeated_header_end, row_count):
            continue

        boundaries.append(boundary)

    # 多个候选点可能互相重叠；按行号去重并保持原顺序。
    return sorted(set(boundaries))


def _match_previous_header_block(
    rows: list[RowInfo],
    current_start: int,
    boundary: int,
) -> int:
    """返回当前重复表头与前方原表头匹配的最大行数。"""

    max_length = min(MAX_HEADER_ROWS, len(rows) - current_start - 1)
    for block_length in range(max_length, 0, -1):
        current_block = rows[current_start:current_start + block_length]
        if not _looks_like_complete_header_block(current_block):
            continue

        previous_start = _find_previous_block_start(
            rows,
            current_start,
            block_length,
        )
        if previous_start is None:
            continue
        if previous_start + block_length >= boundary:
            continue

        # 单行表头只有在它前后都没有紧邻的另一行表头时才接受；多级表头
        # 必须整体匹配，不能只因为第一层相同就把表格拆开。
        if block_length == 1 and _has_adjacent_header_layer(
            rows,
            previous_start,
            current_start,
        ):
            continue
        if block_length == 1 and _has_preceding_header_layer(
            rows,
            previous_start,
            current_start,
        ):
            continue
        return block_length
    return 0


def _find_previous_block_start(
    rows: list[RowInfo],
    current_start: int,
    block_length: int,
) -> int | None:
    """在前方查找结构完全相同、且位于表头位置的原始表头块。"""

    current_signatures = [
        row.signature for row in rows[current_start:current_start + block_length]
    ]
    for previous_start in range(current_start - block_length, -1, -1):
        previous_signatures = [
            row.signature
            for row in rows[previous_start:previous_start + block_length]
        ]
        if previous_signatures != current_signatures:
            continue

        # 原始表头通常位于表格开头，或紧跟一个全宽小节标题。这个位置约束
        # 可以排除数据区偶然出现的重复值。
        # 只有索引 0 才算真正的表格开头。过去把索引 1 也视为开头，会在
        # 多级表头第一层存在轻微 LaTeX 差异时，错误地只匹配第二层表头，
        # 最终把两层表头拆到两个 table 中。
        at_table_start = previous_start == 0
        after_section = (
            previous_start > 0
            and rows[previous_start - 1].is_full_width_section
        )
        if at_table_start or after_section:
            return previous_start
    return None


def _looks_like_complete_header_block(block: list[RowInfo]) -> bool:
    """判断一组重复行是否具有完整字段表头的结构。"""

    if not block:
        return False
    if any(row.logical_width < 2 or row.direct_cell_count < 2 for row in block):
        return False

    combined_text = " ".join(" ".join(row.signature) for row in block)
    # 至少出现两个字段型词，避免把普通数据行中的一个偶然单词当表头。
    matches = HEADER_FIELD_RE.findall(combined_text)
    return len(matches) >= 2


def _has_adjacent_header_layer(
    rows: list[RowInfo],
    previous_start: int,
    current_start: int,
) -> bool:
    """检查单行匹配是否其实只是一个多级表头的第一层。"""

    previous_next = previous_start + 1
    current_next = current_start + 1
    if previous_next >= len(rows) or current_next >= len(rows):
        return False
    return (
        _looks_like_complete_header_block([rows[previous_next]])
        and _looks_like_complete_header_block([rows[current_next]])
    )


def _has_preceding_header_layer(
    rows: list[RowInfo],
    previous_start: int,
    current_start: int,
) -> bool:
    """检查单行匹配是否其实是一个多级表头的后续层。"""

    if previous_start == 0 or current_start == 0:
        return False
    return (
        _looks_like_complete_header_block([rows[previous_start - 1]])
        and _looks_like_complete_header_block([rows[current_start - 1]])
    )


def _contains_data_row(rows: list[RowInfo], start: int, end: int) -> bool:
    """判断区间中是否至少存在一行非标题、非表头的数据。"""

    for row in rows[max(start, 0):max(start, end)]:
        if row.is_full_width_section:
            continue
        nonempty = [value for value in row.signature if value]
        if len(nonempty) < 2:
            continue
        if _looks_like_complete_header_block([row]):
            continue
        return True
    return False


def _build_row_info(row: Tag, table_width: int) -> RowInfo:
    cells = row.find_all(["td", "th"], recursive=False)
    signature: list[str] = []
    for cell in cells:
        colspan = _positive_int(cell.get("colspan"), default=1)
        normalized = _normalize_cell_text(cell)
        signature.extend([normalized] * colspan)

    logical_width = len(signature)
    unique_values = {value for value in signature if value}
    is_explicit_full_width = (
        len(cells) == 1
        and _positive_int(cells[0].get("colspan"), default=1) >= table_width
    )
    is_expanded_full_width = (
        logical_width == table_width
        and len(cells) == table_width
        and len(unique_values) == 1
    )
    return RowInfo(
        tag=row,
        signature=tuple(signature),
        logical_width=logical_width,
        direct_cell_count=len(cells),
        is_full_width_section=is_explicit_full_width or is_expanded_full_width,
    )


def _logical_row_width(row: Tag) -> int:
    cells = row.find_all(["td", "th"], recursive=False)
    return sum(_positive_int(cell.get("colspan"), default=1) for cell in cells)


def _normalize_cell_text(cell: Tag) -> str:
    """生成只用于表头比较的文本；不修改最终输出中的原始单元格。"""

    text = unicodedata.normalize("NFKC", cell.get_text(" ", strip=True))
    text = re.sub(r"\[\s*\d+\s*\]|\(\s*\d+\s*\)", "", text)
    text = re.sub(r"[†‡*]+", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _rowspan_crosses_boundary(rows: list[Tag], boundary: int) -> bool:
    """检查是否有从拆分点上方延伸到下方的 rowspan。"""

    for row_index, row in enumerate(rows[:boundary]):
        for cell in row.find_all(["td", "th"], recursive=False):
            rowspan = _positive_int(cell.get("rowspan"), default=1)
            if row_index + rowspan > boundary:
                return True
    return False


def _build_table_fragment(source_table: Tag, rows: list[Tag]) -> str:
    """复制原表属性以及指定行，构造一个独立的 HTML 表格。"""

    fragment_soup = BeautifulSoup("", "html.parser")
    new_table = fragment_soup.new_tag("table")
    new_table.attrs = copy.deepcopy(source_table.attrs)
    fragment_soup.append(new_table)

    for row in rows:
        row_copy = BeautifulSoup(str(row), "html.parser").find("tr")
        if row_copy is not None:
            new_table.append(row_copy)
    return str(new_table)


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
