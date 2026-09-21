"""STMicroelectronics-specific table recognition helpers."""

from __future__ import annotations

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
