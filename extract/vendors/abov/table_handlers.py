"""Repair only proven physical pin-table structures in ABOV datasheets.

No document names, expected package counts or missing-pin lists are used.
Every output pin is backed by an explicit source cell or bounded range.
"""

from __future__ import annotations

import re

from extract.special_table_handlers import SpecialColumnMapping, SpecialTableMatch


_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-",
                           "\u2013": "-", "\u2014": "-", "\u2212": "-"})
_PACKAGE = re.compile(
    r"(?:LQFP|MQFP|PQFP|QFP|QFN|LQFN|TQFP|TSSOP|SSOP|SOP|DIP)"
    r"\s*-?\s*\d+(?:\s*\(\s*\d+\s*\))?", re.I,
)
_PIN_HEADER = re.compile(r"^pin\s*(?:numbers?|nos?\.?|#)(?:\s+|$)", re.I)
_NUMERIC_TERM = re.compile(r"(\d+)(?:\s*(?:-|to)\s*(\d+))?", re.I)


def _text(value: str) -> str:
    return str(value).translate(_DASHES).strip()


def _roles(headers: list[str]) -> dict[str, list[int]]:
    roles: dict[str, list[int]] = {key: [] for key in ("pin_no", "pin_name", "type", "description")}
    for index, header in enumerate(headers):
        h = re.sub(r"\s+", " ", _text(header)).lower()
        if _PIN_HEADER.match(h) or _PACKAGE.fullmatch(h):
            roles["pin_no"].append(index)
        elif h in {"pin name", "pin names"}:
            roles["pin_name"].append(index)
        elif h in {"type", "pin type", "i/o", "i o", "io"}:
            roles["type"].append(index)
        elif h in {"description", "pin description"}:
            roles["description"].append(index)
    return roles


def classify_abov_header(header: str) -> tuple[str, int]:
    if re.fullmatch(r"pin\s*#", header, re.I) or _PACKAGE.fullmatch(header):
        return "pin_no", 5
    return "", 0


def _numbers(value: str) -> list[str] | None:
    """Read an entire numeric expression, never a prefix of an unknown token."""
    value = _text(value)
    result: list[str] = []
    for part in re.split(r"[,;\n]+", value):
        part = part.strip()
        if not part:
            continue
        match = _NUMERIC_TERM.fullmatch(part)
        if not match:
            return None
        start = int(match[1])
        end = int(match[2] or match[1])
        if start < 1 or end < 1 or abs(end - start) > 1000:
            return None
        step = 1 if end >= start else -1
        result.extend(str(number) for number in range(start, end + step, step))
        if len(result) > 1000:
            return None
    return result or None


def _paired_numbers(value: str) -> tuple[list[str], list[str]] | None:
    match = re.fullmatch(r"([^()]+)\(([^()]+)\)", _text(value))
    if not match:
        return None
    primary, alternate = _numbers(match[1]), _numbers(match[2])
    if primary and alternate and len(primary) == len(alternate):
        return primary, alternate
    return None


def match_abov_table(title: str, headers: list[str], rows: list[list[str]]) -> SpecialTableMatch | None:
    roles = _roles(headers)
    if not roles["pin_no"] or len(roles["pin_name"]) != 1:
        return None
    if not roles["type"] and not roles["description"]:
        return None
    included = frozenset(
        i for i, row in enumerate(rows)
        if any(index < len(row) and (_numbers(row[index]) or _paired_numbers(row[index]))
               for index in roles["pin_no"])
    )
    if not included:
        return None
    return SpecialTableMatch(
        handler_name="abov_physical_pin_table",
        columns=tuple(SpecialColumnMapping(i, headers[i], field)
                      for field, indexes in roles.items() for i in indexes),
        included_row_indexes=included,
    )


def repair_abov_table(title: str, headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    roles = _roles(headers)
    if not roles["pin_no"] or len(roles["pin_name"]) != 1:
        return headers, rows
    fixed_headers = list(headers)
    # A repeated package base with distinct numeric suffixes is a pinout variant,
    # not a field footnote. Preserve that evidence before shared label cleanup.
    variants: dict[str, set[str]] = {}
    for i in roles["pin_no"]:
        match = re.fullmatch(r"(.+?)\s*\((\d+)\)", headers[i])
        if match:
            variants.setdefault(match[1].strip(), set()).add(match[2])
    for i in roles["pin_no"]:
        match = re.fullmatch(r"(.+?)\s*\((\d+)\)", headers[i])
        if match and len(variants[match[1].strip()]) > 1:
            fixed_headers[i] = f"{match[1].strip()} variant {match[2]}"
    if len(roles["pin_no"]) == 1:
        index = roles["pin_no"][0]
        pairs = [_paired_numbers(row[index]) if index < len(row) else None for row in rows]
        # Require a complete, nontrivial permutation with equal source coverage.
        # Ordinary pin-number footnotes do not provide this second mapping axis.
        if len(pairs) >= 3 and all(pairs):
            primary = [n for pair in pairs for n in pair[0]]
            alternate = [n for pair in pairs for n in pair[1]]
            if (primary != alternate and len(set(primary)) == len(primary)
                    and len(set(alternate)) == len(alternate) and set(primary) == set(alternate)):
                fixed_headers[index:index + 1] = ["Pin number mapping A", "Pin number mapping B"]
                fixed_rows = []
                for row, pair in zip(rows, pairs):
                    fixed = list(row)
                    fixed[index:index + 1] = [",".join(pair[0]), ",".join(pair[1])]
                    fixed_rows.append(fixed)
                return _merge_descriptions(fixed_headers, fixed_rows)
    fixed_headers, fixed_rows = _merge_descriptions(fixed_headers, rows)
    return (fixed_headers, fixed_rows) if fixed_headers != headers or fixed_rows is not rows else (headers, rows)


def _merge_descriptions(headers: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    indexes = _roles(headers)["description"]
    if len(indexes) < 2:
        return headers, rows
    removed = set(indexes[1:])
    fixed_rows = []
    for row in rows:
        values = list(dict.fromkeys(row[i] for i in indexes if i < len(row) and row[i].strip()))
        fixed = list(row)
        if indexes[0] < len(fixed):
            fixed[indexes[0]] = "\n".join(values)
        fixed_rows.append([value for i, value in enumerate(fixed) if i not in removed])
    return [header for i, header in enumerate(headers) if i not in removed], fixed_rows


def _join_wrapped_numbers(rows: list[list[str]], roles: dict[str, list[int]]) -> list[list[str]]:
    fixed = [list(row) for row in rows]
    name_index = roles["pin_name"][0]
    for column in roles["pin_no"]:
        candidates: dict[int, int] = {}
        for i, row in enumerate(rows):
            if column >= len(row) or name_index >= len(row):
                continue
            parts = row[column].strip().splitlines()
            names = [line for line in row[name_index].splitlines() if line.strip()]
            if len(parts) >= 2 and all(re.fullmatch(r"\d+", p.strip()) for p in parts) and len(names) != len(parts):
                candidates[i] = int("".join(p.strip() for p in parts))
        for i, value in candidates.items():
            left, right = i - 1, i + 1
            while left in candidates and candidates[left] + (i - left) == value:
                left -= 1
            while right in candidates and candidates[right] - (right - i) == value:
                right += 1
            if left < 0 or right >= len(rows):
                continue
            before = _numbers(rows[left][column]) if column < len(rows[left]) else None
            after = _numbers(rows[right][column]) if column < len(rows[right]) else None
            if before == [str(value - (i - left))] and after == [str(value + (right - i))]:
                fixed[i][column] = str(value)
    return fixed


def _name_range(value: str) -> list[str] | None:
    match = re.fullmatch(r"(.+?)\s*(?:\bto\b|\s*-\s*)\s*(.+)", _text(value), re.I)
    if not match:
        return None
    start_parts, end_parts = match[1].split("/"), match[2].split("/")
    if len(start_parts) != len(end_parts):
        return None
    axes = []
    for start, end in zip(start_parts, end_parts):
        a = re.fullmatch(r"([A-Za-z_]+)(\d+)", start.strip())
        b = re.fullmatch(r"([A-Za-z_]+)(\d+)", end.strip())
        if not a or not b or a[1] != b[1] or abs(int(b[2]) - int(a[2])) > 1000:
            return None
        step = 1 if int(b[2]) >= int(a[2]) else -1
        axes.append([f"{a[1]}{n}" for n in range(int(a[2]), int(b[2]) + step, step)])
    if len({len(axis) for axis in axes}) != 1:
        return None
    return ["/".join(parts) for parts in zip(*axes)]


def _mapped_names(name: str, number_cell: str, numbers: list[str]) -> list[str]:
    names = [line.strip() for line in name.splitlines() if line.strip()]
    number_lines = [line.strip() for line in number_cell.splitlines() if line.strip()]
    if len(names) == len(number_lines) and len(names) > 1:
        groups = [_numbers(line) for line in number_lines]
        if all(groups) and [n for group in groups for n in group] == numbers:
            mapped = []
            for label, group in zip(names, groups):
                expanded = _name_range(label)
                mapped.extend(expanded if expanded and len(expanded) == len(group) else [label] * len(group))
            return mapped
    expanded = _name_range(re.sub(r"\s+", " ", name))
    if expanded and len(expanded) == len(numbers):
        return expanded
    return [name] * len(numbers)


def repair_abov_rows(title: str, headers: list[str], rows: list[list[str]]) -> list[list[str]]:
    roles = _roles(headers)
    if not roles["pin_no"] or len(roles["pin_name"]) != 1:
        return rows
    name_index = roles["pin_name"][0]
    rows = _join_wrapped_numbers(rows, roles)
    expanded_rows = []
    for row in rows:
        cells = [_numbers(row[i]) if i < len(row) else None for i in roles["pin_no"]]
        if not any(cells):
            expanded_rows.append(row)
            continue
        lengths = {len(cell) for cell in cells if cell}
        if len(lengths) != 1:
            expanded_rows.append(row)
            continue
        numbers = next(cell for cell in cells if cell)
        first_column = next(i for i, cell in zip(roles["pin_no"], cells) if cell)
        names = _mapped_names(row[name_index], row[first_column], numbers)
        for offset in range(len(numbers)):
            fixed = list(row)
            for i, cell in zip(roles["pin_no"], cells):
                if cell:
                    fixed[i] = cell[offset]
            fixed[name_index] = names[offset]
            expanded_rows.append(fixed)
    # Rowspan expansion repeats a physical pin for each alternate function or
    # nested description row. Keep every alias/description in one physical row.
    by_pins: dict[tuple[str, ...], list[str]] = {}
    seen_cells: dict[tuple[str, ...], list[set[str]]] = {}
    result = []
    for row in expanded_rows:
        key = tuple(row[i] if i < len(row) else "" for i in roles["pin_no"])
        if not any(value.isdigit() for value in key):
            result.append(row)
            continue
        if key not in by_pins:
            by_pins[key] = list(row)
            seen_cells[key] = [{value} for value in row]
            result.append(by_pins[key])
            continue
        previous = by_pins[key]
        for i, value in enumerate(row):
            if i in roles["pin_no"] or not value.strip() or i >= len(previous):
                continue
            separator = "/" if i == name_index or i in roles["type"] else "\n"
            if value not in seen_cells[key][i]:
                seen_cells[key][i].add(value)
                previous[i] = separator.join(part for part in (previous[i], value) if part)
    return result


def should_keep_abov_record(record: dict[str, object]) -> bool:
    value = _text(str(record.get("pin_no", ""))).upper()
    return bool(value) and not (set(value) == {"-"} or value in {"NA", "N/A", "N.A."})
