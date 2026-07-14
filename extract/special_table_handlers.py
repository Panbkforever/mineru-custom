"""识别可以绕过模型、直接保留的特殊表格。

本模块只处理格式和语义都高度确定的特殊情况。每一种特殊表格必须使用一个
独立函数，未完全满足该函数全部条件时必须返回 ``None``，继续交给模型判断。

当前特殊情况：

* ``reserved_pin_table_handler``：处理只有物理引脚/球号和连接要求的
  Reserved/NC 表。只保留明确要求悬空或禁止连接的行；明确说明封装中不存在
  的位置不作为物理引脚输出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SpecialColumnMapping:
    """特殊表命中后提供给统一提取流程的列映射。"""

    index: int
    header: str
    field_name: str


@dataclass(frozen=True)
class SpecialTableMatch:
    """特殊表的确定性判断结果，不包含最终 pin 记录。"""

    handler_name: str
    columns: tuple[SpecialColumnMapping, ...]
    included_row_indexes: frozenset[int]


def find_special_table_match(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """依次调用每一种特殊处理函数，返回第一个完整命中的结果。"""

    handlers = (reserved_pin_table_handler,)
    for handler in handlers:
        match = handler(title, headers, data_rows)
        if match is not None:
            return match
    return None


def reserved_pin_table_handler(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """识别 Reserved/NC 引脚表，并选出其中真实存在的 Reserved 行。

    必须同时满足以下条件才直接保留：

    1. 标题明确同时表达 Reserved 和 Ball/Pin/Terminal。
    2. 有明确的物理编号列和 Connection Requirements 列。
    3. 没有独立的 pin/ball/signal/terminal name 列。
    4. 每个非空数据行都能明确归类为 Reserved 行或“封装中不存在”行。
    5. 至少存在一行 Reserved，且该行编号列符合物理引脚列表形态。

    返回的行号相对于 ``data_rows``。调用方仍使用统一行提取逻辑，因此
    pin_name 缺失时会由既有规则填充为 ``Reserved``。
    """

    normalized_title = normalize_text(title)
    has_reserved_subject = (
        "reserved" in normalized_title
        and any(word in normalized_title for word in ("ball", "pin", "terminal"))
    )
    if not has_reserved_subject:
        return None

    normalized_headers = [normalize_header_text(header) for header in headers]
    if any(is_name_header(header) for header in normalized_headers):
        return None

    pin_index = find_pin_number_column(normalized_headers)
    requirement_index = find_connection_requirement_column(normalized_headers)
    if pin_index is None or requirement_index is None:
        return None

    included_rows: set[int] = set()
    classified_row_count = 0
    for row_index, row in enumerate(data_rows):
        pin_value = cell_value(row, pin_index)
        requirement = normalize_text(cell_value(row, requirement_index))
        if not pin_value and not requirement:
            continue

        # “封装中不存在”优先于 Reserved 关键词，防止混合描述被误保留。
        if is_nonexistent_package_position(requirement):
            classified_row_count += 1
            continue
        if is_reserved_connection_requirement(requirement) and looks_like_pin_list(pin_value):
            included_rows.add(row_index)
            classified_row_count += 1
            continue

        # 出现任何无法确定语义的行，整张表都不走特殊通道，交回模型。
        return None

    if not included_rows or classified_row_count == 0:
        return None

    return SpecialTableMatch(
        handler_name="reserved_pin_table_handler",
        columns=(SpecialColumnMapping(pin_index, headers[pin_index], "pin_no"),),
        included_row_indexes=frozenset(included_rows),
    )


def find_pin_number_column(headers: list[str]) -> int | None:
    """寻找明确表示物理 Pin/Ball/Terminal 编号的列。"""

    exact_headers = {
        "balls",
        "ball numbers",
        "pins",
        "pin numbers",
        "terminals",
        "terminal numbers",
        "球号",
        "引脚编号",
        "端子编号",
    }
    for index, header in enumerate(headers):
        if header in exact_headers:
            return index
    return None


def find_connection_requirement_column(headers: list[str]) -> int | None:
    """寻找用于判定 Reserved/不存在语义的连接要求列。"""

    for index, header in enumerate(headers):
        if "connection requirement" in header or "连接要求" in header:
            return index
    return None


def is_name_header(header: str) -> bool:
    """判断表中是否已经存在独立的引脚或信号名称列。"""

    return any(
        phrase in header
        for phrase in (
            "pin name",
            "ball name",
            "signal name",
            "terminal name",
            "引脚名称",
            "信号名称",
            "端子名称",
        )
    )


def is_reserved_connection_requirement(value: str) -> bool:
    """判断连接要求是否明确表示 Reserved、NC 或必须悬空。"""

    return bool(
        re.search(r"\breserved\b", value)
        or re.search(r"\b(?:no[ -]?connect|n/?c)\b", value)
        or "left unconnected" in value
        or "leave unconnected" in value
        or "must not be connected" in value
        or "shall not be connected" in value
        or "do not connect" in value
    )


def is_nonexistent_package_position(value: str) -> bool:
    """判断编号是否明确表示封装中不存在，而不是真实 Reserved 引脚。"""

    return any(
        phrase in value
        for phrase in (
            "do not exist on the package",
            "does not exist on the package",
            "not exist on the package",
            "not present on the package",
            "not available on the package",
            "no ball exists",
            "no pin exists",
        )
    )


def looks_like_pin_list(value: str) -> bool:
    """保守确认单元格由一个或多个物理引脚/球号组成。"""

    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return False
    tokens = [token for token in re.split(r"[\s,，;/／、|]+", value) if token]
    return bool(tokens) and all(
        re.fullmatch(r"(?:[A-Za-z]{1,3}\d{1,4}[A-Za-z]?|\d{1,4})", token)
        for token in tokens
    )


def cell_value(row: list[str], index: int) -> str:
    """安全读取指定列。"""

    return str(row[index]).strip() if index < len(row) else ""


def normalize_text(value: str) -> str:
    """生成供特殊规则比较的规范化小写文本。"""

    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def normalize_header_text(value: str) -> str:
    """规范化表头并移除脚注编号，避免 ``BALLS [1]`` 漏判。"""

    value = re.sub(r"\[[^]]+\]", " ", str(value or ""))
    return normalize_text(value)
