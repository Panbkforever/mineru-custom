"""识别引脚编号、名称和功能字段，不聚合复用功能。"""

from __future__ import annotations

from dataclasses import replace
from html import escape
import re

from extract.special_table_handlers import SpecialColumnMapping, SpecialTableMatch


_ALIASES = {
    "pin_no": {"pin", "pinno", "pinnumber", "pinorballno", "ballno", "ballnumber", "terminalno"},
    "pin_name": {"name", "pinname", "ballname", "designation", "symbol"},
    "type": {"pintype", "type", "io"},
    "description": {"function", "description", "pindescription"},
}
_CANONICAL = {"pin_no": "Pin number", "pin_name": "Pin name", "type": "Pin type", "description": "Description"}


def key(value: str) -> str:
    return re.sub(r"[\s.#/]+", "", value).lower()


def roles(headers: list[str]) -> dict[str, list[int]]:
    return {field: [i for i, value in enumerate(headers) if key(value) in aliases]
            for field, aliases in _ALIASES.items()}


def classify_infineon_header(value: str) -> tuple[str, int]:
    for field, aliases in _ALIASES.items():
        if field != "description" and key(value) in aliases:
            return field, 5
    return "", 0


def physical_fields(headers: list[str]) -> dict[str, list[int]] | None:
    fields = roles(headers)
    if len(fields["pin_no"]) == len(fields["pin_name"]) == 1:
        return fields
    return None


def match_infineon_table(title: str, headers: list[str], rows: list[list[str]]) -> SpecialTableMatch | None:
    fields = physical_fields(headers)
    if fields is None:
        return None
    no, name = fields["pin_no"][0], fields["pin_name"][0]
    # 两字段定义表可以没有类型、描述，但编号与名称必须共同存在。
    included = frozenset(i for i, row in enumerate(rows)
                          if len(row) > max(no, name) and row[name].strip()
                          and re.fullmatch(r"\s*\d+(?:[\s,;]+\d+)*\s*", row[no]))
    if not included:
        return None
    return SpecialTableMatch("infineon_physical_pin_table", tuple(
        SpecialColumnMapping(i, headers[i], field) for field, indexes in fields.items() for i in indexes), included)


def is_mode_comparison(title: str, headers: list[str]) -> bool:
    fields = roles(headers)
    # 只有明确标为接口比较、含编号且无名称字段的模式矩阵才排除。
    return (bool(re.search(r"\binterfaces?\b.*\bcomparison\b", title, re.I))
            and len(fields["pin_no"]) == 1 and not fields["pin_name"]
            and len(headers) >= 3)


def prepare_infineon_tables(tables: list, source_pdf_path=None) -> list:
    """在共享初筛前规范字段；仅触碰有编号和名称双重锚点的表。"""
    from extract.table_header_structure import parse_spanned_table

    result = []
    for table in tables:
        rows = parse_spanned_table(table.html)
        if not rows:
            result.append(table)
            continue
        headers = rows[0]
        if is_mode_comparison(table.title, headers):
            # 保留候选和表号供 debug 追踪，清空数据使其不再生成引脚记录。
            html = "<table><tr><td>接口模式比较表，非引脚定义表</td></tr></table>"
            result.append(replace(table, html=html))
            continue
        fields = physical_fields(headers)
        if fields is None:
            result.append(table)
            continue
        fixed = list(headers)
        for field, indexes in fields.items():
            for index in indexes:
                fixed[index] = _CANONICAL[field]
        if fixed == headers:
            result.append(table)
            continue
        # 先按 rowspan/colspan 展开，再重建表，保留每个复用功能行。
        html = "<table>" + "".join("<tr>" + "".join(
            "<td>" + escape(cell).replace("\n", "<br>") + "</td>" for cell in row) + "</tr>"
            for row in [fixed] + rows[1:]) + "</table>"
        result.append(replace(table, html=html))
    return result
