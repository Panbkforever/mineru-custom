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
