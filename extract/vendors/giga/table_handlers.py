"""识别物理引脚字段，区分坐标列与名称列，不聚合不同功能行。"""

from __future__ import annotations

import re

from extract.special_table_handlers import SpecialColumnMapping, SpecialTableMatch


_NUMBER_HEADER = re.compile(r"^(?:pin|ball|pad)\s+(?:no\.?|number|ref\.?)(?:\s+|$)", re.I)


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _coordinate_column(rows: list[list[str]], index: int) -> bool:
    values = [row[index].strip() for row in rows if index < len(row) and row[index].strip()]
    hits = sum(bool(re.fullmatch(r"[A-Z]{1,3}\d{1,3}", value)) for value in values)
    return hits >= 2 and hits > len(values) / 2


def field_indexes(headers: list[str], rows: list[list[str]] | None = None) -> dict[str, list[int]]:
    fields: dict[str, list[int]] = {key: [] for key in ("pin_no", "pin_name", "type", "description")}
    for index, header in enumerate(headers):
        h = normalized(header)
        if _NUMBER_HEADER.match(h):
            fields["pin_no"].append(index)
        elif h in {"pin name", "ball name", "pad name"}:
            fields["pin_name"].append(index)
        elif h in {"i/o", "i o", "io", "type", "pin type"}:
            fields["type"].append(index)
        elif h in {"description", "function", "pin description"}:
            fields["description"].append(index)
    # 同时存在 Pin Name 时，坐标型 Ball Name 才是编号列；不能全局改写 Ball Name。
    if not fields["pin_no"] and rows is not None:
        signals = [i for i in fields["pin_name"] if normalized(headers[i]) == "pin name"]
        coordinates = [i for i in fields["pin_name"] if normalized(headers[i]) == "ball name"
                       and _coordinate_column(rows, i)]
        if len(signals) == len(coordinates) == 1:
            fields["pin_no"] = coordinates
            fields["pin_name"] = signals
    return fields


def repair_giga_table(title: str, headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    fields = field_indexes(headers, rows)
    if len(fields["pin_no"]) == 1:
        index = fields["pin_no"][0]
        if normalized(headers[index]) == "ball name":
            fixed = list(headers)
            fixed[index] = "Ball No."
            return fixed, rows
    return headers, rows


def match_giga_table(title: str, headers: list[str], rows: list[list[str]]) -> SpecialTableMatch | None:
    fields = field_indexes(headers, rows)
    if not fields["pin_no"] or len(fields["pin_name"]) != 1:
        return None
    if not fields["type"] and not fields["description"]:
        return None
    indexes = frozenset(i for i, row in enumerate(rows)
                        if any(index < len(row) and row[index].strip() for index in fields["pin_no"]))
    if not indexes:
        return None
    return SpecialTableMatch(
        handler_name="giga_physical_pin_table",
        columns=tuple(SpecialColumnMapping(i, headers[i], field)
                      for field, columns in fields.items() for i in columns),
        included_row_indexes=indexes,
    )


def should_keep_giga_record(record: dict[str, object]) -> bool:
    number = str(record.get("pin_no", "")).strip()
    if not number or re.fullmatch(r"[-\u2010-\u2014\u2212\s]+", number):
        return False
    # Multiple/NC 是无具体球位的汇总说明；明确写出 A1/NC、B1/DNU 等记录仍保留。
    name = re.sub(r"[.\s]+", "", str(record.get("pin_name", ""))).upper()
    return not (number.casefold() == "multiple" and name == "NC")
