"""
Post-process very long tables.

This module is reserved for table structures where one visual row may span a
large vertical area, such as TI Pin Attributes tables. Those tables need logic
that understands vertical positions inside a cell; guessing from adjacent rows
can incorrectly attach values to ball numbers that are actually blank.
"""

from __future__ import annotations

import html as html_lib
import re


TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(
    r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
    re.DOTALL | re.IGNORECASE,
)


def repair_ultra_long_table_html(table_html: str) -> tuple[str, int]:
    """Apply conservative repairs for ultra-long table HTML.

    For now, do not copy VSS continuation fields from adjacent rows. A Pin
    Attributes row can contain hundreds of ball numbers, while VSS (continued)
    only applies to a lower vertical slice of that same visual cell. Without
    PDF-coordinate splitting, filling the whole row creates false values.
    """
    if not _is_pin_attributes_table(table_html):
        return table_html, 0
    return table_html, 0


def _is_pin_attributes_table(table_html: str) -> bool:
    rows = _parse_expanded_rows(table_html)
    header_index, _columns = _find_pin_attributes_header(rows)
    return header_index >= 0


def _find_pin_attributes_header(
    rows: list[list[dict[str, str]]],
) -> tuple[int, dict[str, int]]:
    for row_index, cells in enumerate(rows[:3]):
        labels = [_normalize_header_label(_plain_text(cell["inner"])) for cell in cells]
        columns = {
            "ball_num": _first_label_index(labels, "ballnum"),
            "ball_name": _first_label_index(labels, "ballname"),
            "signal_name": _first_label_index(labels, "signalname"),
            "signal_type": _first_label_index(labels, "signaltype"),
        }
        if all(index >= 0 for index in columns.values()):
            return row_index, columns
    return -1, {}


def _normalize_header_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _first_label_index(labels: list[str], needle: str) -> int:
    for index, label in enumerate(labels):
        if needle in label:
            return index
    return -1


def _parse_expanded_rows(table_html: str) -> list[list[dict[str, str]]]:
    rows = []
    for tr_match in TR_RE.finditer(table_html):
        cells = []
        for cell_match in CELL_RE.finditer(tr_match.group(2)):
            cells.append(
                {
                    "open": cell_match.group(1),
                    "inner": cell_match.group(2),
                    "close": cell_match.group(3),
                }
            )
        if cells:
            rows.append(cells)
    return rows


def _plain_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()
