"""Elan field aliases and explicit parallel ranges; never aggregate functions."""

from __future__ import annotations

import re
import warnings

from extract.special_table_handlers import SpecialColumnMapping, SpecialTableMatch


_RANGES = str.maketrans({"~": "-", "\uff5e": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-"})
_FIELDS = {
    "pin_no": {"pinnumber", "pinno", "引脚序号", "引脚编号", "引脚号", "引脚"},
    "pin_name": {"pinname", "pinnames", "symbol", "符号", "名称", "引脚名称"},
    "type": {"type", "pintype", "类型", "引脚类型"},
    "description": {"description", "function", "功能", "说明"},
}


def _key(value: str) -> str:
    return re.sub(r"[\s.#]+", "", value).lower()


def roles(headers: list[str]) -> dict[str, list[int]]:
    return {field: [i for i, value in enumerate(headers) if _key(value) in aliases]
            for field, aliases in _FIELDS.items()}


def classify_elan_header(value: str) -> tuple[str, int]:
    for field, aliases in _FIELDS.items():
        if field != "description" and _key(value) in aliases:
            return field, 5
    return "", 0


def numbers(value: str) -> list[str] | None:
    value = value.replace("\\~", "~").translate(_RANGES)
    result = []
    for part in re.split(r"[,;\n]+", value):
        if not part.strip():
            continue
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            return None
        start, end = int(match[1]), int(match[2] or match[1])
        if min(start, end) < 1 or abs(end - start) > 1000:
            return None
        step = 1 if end >= start else -1
        result.extend(str(n) for n in range(start, end + step, step))
        if len(result) > 1000:
            return None
    return result or None


def _names(value: str) -> list[str]:
    result = []
    for part in re.split(r"[,;\n]+", value):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"([A-Za-z]+)(\d+)\s*[~\uff5e\-]\s*([A-Za-z]+)(\d+)", part)
        if match and match[1] == match[3] and abs(int(match[2]) - int(match[4])) <= 1000:
            start, end = int(match[2]), int(match[4])
            step = 1 if end >= start else -1
            result.extend(f"{match[1]}{n}" for n in range(start, end + step, step))
        else:
            result.append(part)
    return result


def repair_elan_table(title: str, headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    fields = roles(headers)
    if any(len(fields[field]) != 1 for field in ("pin_no", "pin_name", "type")):
        return headers, rows
    canonical = {"pin_no": "Pin number", "pin_name": "Pin name", "type": "Pin type", "description": "Description"}
    fixed = list(headers)
    for field, indexes in fields.items():
        for index in indexes:
            fixed[index] = canonical[field]
    return (fixed, rows) if fixed != headers else (headers, rows)


def match_elan_table(title: str, headers: list[str], rows: list[list[str]]) -> SpecialTableMatch | None:
    fields = roles(headers)
    if any(len(fields[field]) != 1 for field in ("pin_no", "pin_name", "type")):
        return None
    index = fields["pin_no"][0]
    included = frozenset(i for i, row in enumerate(rows) if index < len(row) and numbers(row[index]))
    if not included:
        return None
    return SpecialTableMatch("elan_physical_pin_table", tuple(
        SpecialColumnMapping(i, headers[i], field) for field, indexes in fields.items() for i in indexes), included)


def _types(value: str, count: int, names_cell: str, number_cell: str) -> list[str] | None:
    value = value.strip()
    if value in {"I", "O", "I/O", "P", "-", "\u2013", "\u2014", ""}:
        return [value.translate(_RANGES)] * count
    parts = [part.strip() for part in re.split(r"[\n|]+", value) if part.strip()]
    if len(parts) == count:
        return parts
    name_lines = [part for part in names_cell.splitlines() if part.strip()]
    number_lines = [numbers(part) for part in number_cell.splitlines() if part.strip(", \t")]
    if len(parts) == len(name_lines) == len(number_lines) and all(number_lines):
        expanded = [part for part, ns in zip(parts, number_lines) for _ in ns]
        if len(expanded) == count:
            return expanded
    return None


def repair_elan_rows(title: str, headers: list[str], rows: list[list[str]]) -> list[list[str]]:
    fields = roles(headers)
    if any(len(fields[field]) != 1 for field in ("pin_no", "pin_name", "type")):
        return rows
    no, name, typ = (fields[field][0] for field in ("pin_no", "pin_name", "type"))
    result = []
    for row in rows:
        if len(row) <= max(no, name, typ):
            result.append(row)
            continue
        ns = numbers(row[no])
        labels = _names(row[name])
        if not ns:
            result.append(row)
            continue
        if len(labels) == 1:
            labels = labels * len(ns)
        types = _types(row[typ], len(ns), row[name], row[no])
        if len(labels) != len(ns) or types is None:
            warnings.warn(f"Elan source row has unequal pin/name/type cardinality: {row[name]!r}, {row[no]!r}; no guessed pairing.")
            result.append(row)
            continue
        for number, label, pin_type in zip(ns, labels, types):
            fixed = list(row)
            fixed[no], fixed[name], fixed[typ] = number, label, pin_type
            result.append(fixed)
    return result
