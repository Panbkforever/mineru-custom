"""识别明确的 Pinning 表及表内范围，不从图形、型号或文件名推导引脚。"""

from __future__ import annotations

from dataclasses import replace
from html import escape
import re

from extract.special_table_handlers import SpecialColumnMapping, SpecialTableMatch


_FIELDS = {"pin_no": {"pin", "pinno", "pinnumber"},
           "pin_name": {"symbol", "pinname", "name"},
           "type": {"type", "pintype", "io"},
           "description": {"description", "pindescription"}}
_CANONICAL = {"pin_no": "Pin number", "pin_name": "Pin name", "type": "Pin type", "description": "Description"}


def key(value: str) -> str:
    return re.sub(r"[\s.#/]+", "", value).lower()


def is_pinning(title: str) -> bool:
    return bool(re.search(r"\bpinning\b", title, re.I))


def is_terminal(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[1-9]\d*|[A-Z]{1,3}\d{1,3}|mb|tab|ep|epad|pad)", value.strip(), re.I))


def roles(headers: list[str]) -> dict[str, list[int]] | None:
    fields = {field: [i for i, h in enumerate(headers) if key(h) in aliases]
              for field, aliases in _FIELDS.items()}
    if (len(fields["pin_no"]) != 1 or len(fields["description"]) != 1
            or len(fields["pin_name"]) > 1 or len(fields["type"]) > 1):
        return None
    return fields


def _html(headers: list[str], rows: list[list[str]]) -> str:
    return "<table>" + "".join("<tr>" + "".join(
        "<td>" + escape(cell).replace("\n", "<br>") + "</td>" for cell in row) + "</tr>"
        for row in [headers] + rows) + "</table>"


def prepare_nexperia_tables(tables: list, source_pdf_path=None) -> list:
    """将有明确字段和表题的 Pinning 表规范化，并按全宽范围标题拆分。"""
    from extract.table_header_structure import parse_spanned_table

    output = []
    for table in tables:
        if not is_pinning(table.title):
            output.append(table)
            continue
        rows = parse_spanned_table(table.html)
        fields = roles(rows[0]) if rows else None
        if fields is None:
            output.append(table)
            continue
        selected = [(field, index) for field in ("pin_no", "pin_name", "type", "description")
                    for index in fields[field]]
        headers = [_CANONICAL[field] for field, _ in selected]
        number, description = fields["pin_no"][0], fields["description"][0]
        scope = ""
        blocks = []
        data = []
        for row in rows[1:]:
            # colspan 展开后的全宽标题必须在所有列中重复，不凭编号重置猜分组。
            if (row and row[0].strip() and all(value == row[0] for value in row)
                    and not row[0].strip().isdigit()):
                if data:
                    blocks.append((scope, data))
                scope, data = re.sub(r"\s+", " ", row[0]).strip(), []
            elif (len(row) > max(number, description) and is_terminal(row[number])
                  and row[description].strip()):
                data.append([row[index] if index < len(row) else "" for _, index in selected])
        if data:
            blocks.append((scope, data))
        if not blocks:
            output.append(table)
            continue
        for scope, data in blocks:
            title = table.title + (" | " + scope if scope else "")
            output.append(replace(table, html=_html(headers, data), title=title,
                                  group_context=(table.group_context + "\n" + title).strip()))
    return output


def match_nexperia_table(title: str, headers: list[str], rows: list[list[str]]) -> SpecialTableMatch | None:
    fields = roles(headers)
    if not is_pinning(title) or fields is None:
        return None
    number, description = fields["pin_no"][0], fields["description"][0]
    included = frozenset(i for i, row in enumerate(rows)
                          if len(row) > max(number, description) and is_terminal(row[number]) and row[description].strip())
    if not included:
        return None
    return SpecialTableMatch("nexperia_explicit_pinning_table", tuple(
        SpecialColumnMapping(i, headers[i], field) for field, indexes in fields.items() for i in indexes), included)


def _clauses(value: str) -> list[str]:
    # 只在括号外分割逗号，不破坏括号内的器件说明。
    depth = 0
    start = 0
    parts = []
    for i, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and not depth:
            parts.append(value[start:i].strip())
            start = i + 1
    parts.append(value[start:].strip())
    return parts


def _agrees(symbol: str, description: str) -> bool:
    # A/K 编号与原描述中的阳极/阴极及二极管编号同时一致，才按分句配对。
    match = re.fullmatch(r"([AK])(\d*)", symbol, re.I)
    if not match:
        return False
    term = "anode" if match[1].upper() == "A" else "cathode"
    return bool(re.search(r"\b" + term + r"\b", description, re.I)) and (
        not match[2] or bool(re.search(r"\bdiode\s+" + re.escape(match[2]) + r"\b", description, re.I)))


def repair_nexperia_rows(title: str, headers: list[str], rows: list[list[str]]) -> list[list[str]]:
    fields = roles(headers)
    if not is_pinning(title) or fields is None or not fields["pin_name"]:
        return rows
    name, description = fields["pin_name"][0], fields["description"][0]
    output = []
    for row in rows:
        names = [value.strip() for value in row[name].split(";")] if name < len(row) else []
        if len(names) < 2 or not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_#.+-]*", value) for value in names):
            output.append(row)
            continue
        parts = _clauses(row[description]) if description < len(row) else []
        paired = len(parts) == len(names) and all(_agrees(n, d) for n, d in zip(names, parts))
        for index, symbol in enumerate(names):
            fixed = list(row)
            fixed[name] = symbol
            if paired:
                fixed[description] = parts[index]
            # 无法证明分句对应时，每个名称保留完整原描述，不猜测一一关系。
            output.append(fixed)
    return output


def finalize_nexperia_record(normalized: dict, original: dict) -> dict:
    """没有名称来源时不沿用共享层的 Reserved 默认值，也不把描述冒充名称。"""
    if not str(original.get("pin_name", "")).strip():
        normalized.pop("pin_name", None)
    return normalized
