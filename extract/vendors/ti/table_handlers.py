"""TI 附属配置表的表级判断。"""

import re

from extract.special_table_handlers import SpecialTableMatch


def ti_binding_group(group: str, label: str, header: str) -> str:
    """以显式 SLOT/SOCKET 表头保留卡槽来源，卡槽不再成为封装。"""
    match = re.search(r"\b(?:slot|socket)\s*([AB])\b", str(header), re.I)
    if match and re.fullmatch(r"\s*[AB]\s+[A-Za-z0-9]+\s*", str(label), re.I):
        return f"{group} (SLOT {match.group(1).upper()})"
    return group


def repair_ti_rows(title: str, headers: list[str], rows: list[list[str]]) -> list[list[str]]:
    """拆分编号列中紧邻且边界明确的 BGA 球号，不改动行数和其他字段。"""
    indexes = []
    for i, header in enumerate(headers):
        normalized = re.sub(r"\s+", " ", str(header)).strip().lower()
        if re.search(r"\bball\b", normalized) or re.search(r"\bpin\s*(?:no|number)\b", normalized):
            if not any(term in normalized for term in ("name", "type", "count", "description")):
                indexes.append(i)
    if not indexes:
        return rows
    changed = False
    result = []
    for row in rows:
        fixed = list(row)
        for index in indexes:
            if index >= len(fixed):
                continue
            value = str(fixed[index]).strip()
            # 只拆完全由大写行字母和一至两位列号构成的连续球号。
            if not re.fullmatch(r"(?:[A-Z]{1,2}\d{1,2}){2,}", value):
                continue
            tokens = re.findall(r"[A-Z]{1,2}\d{1,2}", value)
            if len(tokens) >= 2 and ''.join(tokens) == value:
                fixed[index] = ', '.join(tokens)
                changed = True
        result.append(fixed)
    return result if changed else rows


def match_ti_table(title: str, headers: list[str], rows: list[list[str]]):
    """仅排除结构明确的配置矩阵，不触碰已选物理引脚表的数据行。"""
    normalized = [re.sub(r"\s+", " ", str(h)).strip().lower() for h in headers]
    clean_title = re.sub(r"\s+", " ", str(title)).strip().lower()
    ioset_balls = sum(bool(re.search(r"\bioset\s*\d+\s+ball\b", h)) for h in normalized)
    ioset_mux = sum(bool(re.search(r"\bioset\s*\d+\s+mux\b", h)) for h in normalized)
    is_ioset_matrix = bool(re.search(r"\biosets?\b", clean_title)) and ioset_balls >= 2 and ioset_mux >= 2
    has_input = any("input pin number" in h for h in normalized)
    has_output = any("output pin number" in h for h in normalized)
    timer_matrix = ("signal connections" in clean_title or any("signal connections" in h for h in normalized)) and has_input and has_output
    # 输入选择/控制矩阵以控制位为主轴，不是器件封装引脚定义表。
    input_mux_matrix = (
        "input multiplexing and control" in clean_title
        and any("dedicated inputs" in h for h in normalized)
        and any("multiplexed inputs" in h for h in normalized)
        and any("input multiplexor control" in h for h in normalized)
    )
    if not (is_ioset_matrix or timer_matrix or input_mux_matrix):
        return None
    return SpecialTableMatch(
        handler_name="ti_non_package_configuration_matrix",
        columns=(), included_row_indexes=frozenset(), should_extract=False,
    )
