"""STMicroelectronics-specific table recognition helpers."""

from __future__ import annotations

import re

from extract.special_table_handlers import (
    SpecialColumnMapping,
    SpecialTableMatch,
    normalize_header_text,
    normalize_text,
)


def classify_st_header(normalized_header: str) -> tuple[str, int]:
    """Map ST pad-table headers onto the shared pin fields."""

    if normalized_header in {
        "pad ref",
        "pad ref.",
        "pad reference",
        "pads ref",
        "pads ref.",
        "pads reference",
    }:
        return "pin_no", 5
    if normalized_header in {"pad name", "pads name"}:
        return "pin_name", 5
    return "", 0


def match_st_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    return (
        match_st_pad_description_table(title, headers, data_rows)
        or match_st_pin_assignment_table(title, headers, data_rows)
    )


def repair_st_table_rows(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[list[str]]:
    """Repair MinerU row/cell artifacts in ST ball definition tables."""

    if not _is_st_ball_definition_table(title, headers):
        return data_rows

    roles = _st_pin_assignment_roles(headers)
    if not roles["pin_no"] or not roles["pin_name"] or not roles["type"]:
        return data_rows

    pin_indexes = roles["pin_no"]
    pin_name_index = roles["pin_name"][0]
    type_index = roles["type"][0]
    repaired_rows: list[list[str]] = []
    changed = False

    for row in data_rows:
        normalized_row = list(row)
        for index in pin_indexes:
            if index < len(normalized_row):
                fixed = _join_wrapped_bga_ball(normalized_row[index])
                if fixed != normalized_row[index]:
                    normalized_row[index] = fixed
                    changed = True

        split_rows = _split_merged_st_ball_row(
            normalized_row,
            pin_indexes,
            pin_name_index,
            type_index,
        )
        if split_rows is None:
            repaired_rows.append(normalized_row)
        else:
            repaired_rows.extend(split_rows)
            changed = True

    return repaired_rows if changed else data_rows


def _is_st_ball_definition_table(title: str, headers: list[str]) -> bool:
    title_text = normalize_text(title)
    roles = _st_pin_assignment_roles(headers)
    if not (roles["pin_no"] and roles["pin_name"] and roles["type"]):
        return False
    if "ball definitions" in title_text:
        return True

    normalized_headers = [normalize_header_text(header) for header in headers]
    has_bga_package = any(
        header.startswith("pin number") and "bga" in header
        for header in normalized_headers
    )
    has_pin_name = any(header.startswith("pin name") for header in normalized_headers)
    has_pin_type = any(header == "pin type" for header in normalized_headers)
    return has_bga_package and has_pin_name and has_pin_type


def _join_wrapped_bga_ball(value: str) -> str:
    text = str(value or "").strip()
    if not text or "\n" not in text:
        return text
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"[A-Z]{1,2}\d{1,2}", compact):
        return compact
    return text


def _split_merged_st_ball_row(
    row: list[str],
    pin_indexes: list[int],
    pin_name_index: int,
    type_index: int,
) -> list[list[str]] | None:
    if pin_name_index >= len(row) or type_index >= len(row):
        return None

    pin_splits: dict[int, tuple[str, str]] = {}
    merged_pin_cells = 0
    for index in pin_indexes:
        value = row[index] if index < len(row) else ""
        split = _split_merged_pin_cell(value)
        if split is None:
            return None
        pin_splits[index] = split
        if _cell_contains_two_values(value, split):
            merged_pin_cells += 1

    if merged_pin_cells < 2:
        return None

    type_split = _split_merged_type_cell(row[type_index])
    if type_split is None:
        return None
    name_split = _split_merged_pin_name_cell(row[pin_name_index], type_split)
    if name_split is None:
        return None

    first = list(row)
    second = list(row)
    for index, split in pin_splits.items():
        first[index], second[index] = split
    first[pin_name_index], second[pin_name_index] = name_split
    first[type_index], second[type_index] = type_split

    for index in range(len(row)):
        if index in pin_splits or index in {pin_name_index, type_index}:
            continue
        first[index], second[index] = _split_optional_merged_cell(row[index])

    return [first, second]


def _split_merged_pin_cell(value: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return ("", "")
    ball = r"[A-Z]{1,2}\d{1,2}"
    if text == "-":
        return ("-", "-")
    match = re.fullmatch(rf"-({ball})", text)
    if match:
        return ("-", match.group(1))
    match = re.fullmatch(rf"({ball})-", text)
    if match:
        return (match.group(1), "-")
    match = re.fullmatch(rf"({ball})({ball})", text)
    if match:
        return (match.group(1), match.group(2))
    if re.fullmatch(ball, text):
        return (text, text)
    return None


def _cell_contains_two_values(original: str, split: tuple[str, str]) -> bool:
    compact = re.sub(r"\s+", "", str(original or ""))
    return compact not in {split[0], "-"} or split[0] != split[1]


def _split_merged_type_cell(value: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or ""))
    for first in ("I/O", "S", "O", "I", "A"):
        if text.startswith(first):
            second = text[len(first):]
            if second in {"I/O", "S", "O", "I", "A"}:
                return (first, second)
    return None


def _split_merged_pin_name_cell(
    value: str,
    type_split: tuple[str, str],
) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return None
    power_names = ("VDDQ_DDR", "VDDCORE", "VDDCPU", "VSS", "VDD")
    if type_split[0] == "S":
        for name in power_names:
            if text.startswith(name) and len(text) > len(name):
                return (name, text[len(name):])
    if type_split[1] == "S":
        for name in power_names:
            if text.endswith(name) and len(text) > len(name):
                return (text[:-len(name)], name)
    return None


def _split_optional_merged_cell(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return ("", "")
    if set(compact) == {"-"} and len(compact) >= 2:
        return ("-", "-")
    if compact.startswith("-") and len(compact) > 1:
        return ("-", compact[1:])
    if compact.endswith("-") and len(compact) > 1:
        return (compact[:-1], "-")
    return (text, text)


def is_st_physical_table(title: str, headers: list[str]) -> bool:
    title_text = normalize_text(title)
    if "pin assignment" not in title_text or "description" not in title_text:
        return False
    roles = _st_pin_assignment_roles(headers)
    return bool(roles["pin_no"] and roles["pin_name"])


def match_st_pad_description_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """Recognize ST physical pad description tables.

    ST small RF/filter datasheets often publish the physical pin list as
    ``Pad ref | Pad name | Description`` under titles such as
    ``Pad description top view (pads down)``. These are equivalent to the
    shared ``pin_no | pin_name | description`` schema, but TI-oriented rules
    do not treat ``Pad`` as a pin axis.
    """

    title_text = normalize_text(title)
    if "pad" not in title_text or "description" not in title_text:
        return None

    normalized_headers = [normalize_header_text(header) for header in headers]
    ref_index = _find_header_index(normalized_headers, {
        "pad ref",
        "pad ref.",
        "pad reference",
        "pads ref",
        "pads ref.",
        "pads reference",
    })
    name_index = _find_header_index(normalized_headers, {"pad name", "pads name"})
    description_index = _find_header_index(normalized_headers, {"description"})
    if ref_index is None or name_index is None:
        return None

    if not any(_has_data_cell(row, ref_index) for row in data_rows):
        return None

    mappings = [
        SpecialColumnMapping(ref_index, headers[ref_index], "pin_no"),
        SpecialColumnMapping(name_index, headers[name_index], "pin_name"),
    ]
    if description_index is not None:
        mappings.append(
            SpecialColumnMapping(
                description_index,
                headers[description_index],
                "description",
            )
        )

    return SpecialTableMatch(
        handler_name="st_pad_description_table_handler",
        columns=tuple(mappings),
        included_row_indexes=frozenset(range(len(data_rows))),
    )


def _find_header_index(headers: list[str], accepted: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in accepted:
            return index
    return None


def _has_data_cell(row: list[str], index: int) -> bool:
    return index < len(row) and bool(str(row[index]).strip())


def match_st_pin_assignment_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """Recognize ST MCU pin assignment tables with package-specific pin columns."""

    if not is_st_physical_table(title, headers):
        return None
    roles = _st_pin_assignment_roles(headers)
    mappings: list[SpecialColumnMapping] = []
    for index in roles["pin_no"]:
        mappings.append(SpecialColumnMapping(index, headers[index], "pin_no"))
    mappings.append(SpecialColumnMapping(roles["pin_name"][0], headers[roles["pin_name"][0]], "pin_name"))
    if roles["type"]:
        mappings.append(SpecialColumnMapping(roles["type"][0], headers[roles["type"][0]], "type"))

    if not any(
        any(_has_data_cell(row, index) for index in roles["pin_no"])
        for row in data_rows
    ):
        return None

    return SpecialTableMatch(
        handler_name="st_pin_assignment_table_handler",
        columns=tuple(mappings),
        included_row_indexes=frozenset(range(len(data_rows))),
    )


def _st_pin_assignment_roles(headers: list[str]) -> dict[str, list[int]]:
    roles = {"pin_no": [], "pin_name": [], "type": []}
    for index, header in enumerate(headers):
        normalized = normalize_header_text(header)
        if _is_st_package_pin_header(normalized):
            roles["pin_no"].append(index)
        elif normalized.startswith("pin name"):
            roles["pin_name"].append(index)
        elif normalized == "pin type":
            roles["type"].append(index)
    return roles


def _is_st_package_pin_header(normalized: str) -> bool:
    if not normalized.startswith("pin "):
        return False
    if normalized.startswith("pin name") or normalized == "pin type":
        return False
    if normalized.startswith("pin number") and "bga" in normalized:
        return True
    package_terms = (
        "so",
        "sop",
        "tssop",
        "wlcsp",
        "ufqfpn",
        "ufqfn",
        "qfpn",
        "lqfp",
        "ufbga",
        "bga",
        "qfn",
    )
    return any(term in normalized for term in package_terms)
