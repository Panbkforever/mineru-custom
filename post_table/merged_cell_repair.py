"""
Repair merged rows caused by continued tables.

Some VLM table outputs merge the last physical row before a page break with the
first physical row after the page break. This module handles that class of
errors conservatively:

* Only pin/function-like tables are considered.
* Only cells with no visible separators are split. Values containing comma,
  slash, whitespace, or hyphen are treated as intentional multi-value cells.
* A row is split only when both pin number and pin/signal name columns can be
  split into the same number of tokens that match the surrounding table shape.

This intentionally lives outside fix_ocr_table.py so every dedicated table
repair remains isolated and easier to replace.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(
    r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
    re.DOTALL | re.IGNORECASE,
)

PIN_TOKEN_RE = re.compile(r"[A-Z]{1,3}\d{1,3}")
SIGNAL_WITH_PAREN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*\([^()]+\)"
)

VISIBLE_SEPARATOR_RE = re.compile(r"[\s,，;/／、-]")
TYPE_TOKEN_RE = re.compile(r"I/O/Z|I/O|O/Z|I|O|S|A|GND|Supply|Control I", re.IGNORECASE)


@dataclass
class ColumnMap:
    pin_no: int
    pin_name: int
    pin_type: int | None
    description: int | None
    header_rows: int


def repair_merged_cells_in_markdown(md_content: str) -> str:
    """Repair merged continued-table rows in every HTML table in markdown."""

    fixed_count = 0

    def replace_table(match: re.Match[str]) -> str:
        nonlocal fixed_count
        fixed_html, count = repair_merged_cells_in_table(match.group(0))
        fixed_count += count
        return fixed_html

    result = re.sub(
        r"<table[^>]*>.*?</table>",
        replace_table,
        md_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fixed_count:
        logger.info("跨页续表合并单元格修复: 拆分 %d 行", fixed_count)
    return result


def repair_merged_cells_in_table(table_html: str) -> tuple[str, int]:
    """Repair a single HTML table and return ``(html, split_row_count)``."""
    rows = _parse_rows(table_html)
    if len(rows) < 4:
        return table_html, 0

    columns = _detect_pin_table_columns(rows)
    if columns is None:
        return table_html, 0

    changed = 0
    repaired_rows: list[list[dict[str, str]]] = []
    for row_index, cells in enumerate(rows):
        if row_index < columns.header_rows:
            repaired_rows.append(cells)
            continue

        split_rows = _split_suspicious_row(rows, row_index, columns)
        if split_rows is None:
            repaired_rows.append(cells)
            continue

        repaired_rows.extend(split_rows)
        changed += 1

    if not changed:
        return table_html, 0
    return _rebuild_table(repaired_rows), changed


def _detect_pin_table_columns(rows: list[list[dict[str, str]]]) -> ColumnMap | None:
    """Infer key columns for pin/function tables from the first two rows."""
    best: ColumnMap | None = None
    for header_rows in (2, 1):
        if len(rows) < header_rows:
            continue
        max_cols = max(len(row) for row in rows[:header_rows])
        labels = []
        for col in range(max_cols):
            parts = []
            for row in rows[:header_rows]:
                if col < len(row):
                    parts.append(_plain_text(row[col]["inner"]))
            labels.append(_normalize_header(" ".join(parts)))

        pin_no = _find_pin_no_column(labels)
        pin_name = _find_first(labels, ("pinname", "signalname", "ballname", "name"))
        pin_type = _find_first(labels, ("signaltype", "pintype", "type", "io"))
        description = _find_first(labels, ("description", "function"))

        if pin_no < 0 or pin_name < 0 or pin_no == pin_name:
            continue
        if description < 0 and pin_type < 0:
            continue
        best = ColumnMap(pin_no, pin_name, pin_type if pin_type >= 0 else None, description if description >= 0 else None, header_rows)
        break
    return best


def _split_suspicious_row(
    rows: list[list[dict[str, str]]],
    row_index: int,
    columns: ColumnMap,
) -> list[list[dict[str, str]]] | None:
    cells = rows[row_index]
    if columns.pin_no >= len(cells) or columns.pin_name >= len(cells):
        return None

    pin_no_text = _plain_text(cells[columns.pin_no]["inner"])
    pin_name_text = _plain_text(cells[columns.pin_name]["inner"])

    pin_tokens = _split_compact_pin_numbers(pin_no_text)
    if len(pin_tokens) < 2:
        return None

    name_tokens = _split_compact_signal_names(pin_name_text)
    if len(name_tokens) != len(pin_tokens):
        return None

    if not _has_compatible_context(rows, row_index, columns):
        return None

    type_tokens = _type_tokens_for_row(cells, columns, len(pin_tokens))
    desc_tokens = _description_tokens_for_row(cells, columns, len(pin_tokens))

    split_rows: list[list[dict[str, str]]] = []
    for token_index, pin_token in enumerate(pin_tokens):
        new_row = [dict(cell) for cell in cells]
        new_row[columns.pin_no]["inner"] = html_lib.escape(pin_token)
        new_row[columns.pin_name]["inner"] = html_lib.escape(name_tokens[token_index])
        if columns.pin_type is not None and columns.pin_type < len(new_row):
            new_row[columns.pin_type]["inner"] = html_lib.escape(type_tokens[token_index])
        if (
            columns.description is not None
            and columns.description < len(new_row)
            and desc_tokens is not None
        ):
            new_row[columns.description]["inner"] = html_lib.escape(desc_tokens[token_index])
        split_rows.append(new_row)
    return split_rows


def _split_compact_pin_numbers(value: str) -> list[str]:
    """Split compact pin numbers like H2H1 or D11B10, never delimited values."""
    value = value.strip()
    if not value or VISIBLE_SEPARATOR_RE.search(value):
        return []
    tokens = PIN_TOKEN_RE.findall(value)
    if len(tokens) < 2 or "".join(tokens) != value:
        return []
    return tokens


def _split_compact_signal_names(value: str) -> list[str]:
    """Split compact signal names only when there is no visible separator."""
    value = value.strip()
    if not value or VISIBLE_SEPARATOR_RE.search(value):
        return []
    tokens = SIGNAL_WITH_PAREN_RE.findall(value)
    if len(tokens) >= 2 and "".join(tokens) == value:
        return tokens
    return []


def _type_tokens_for_row(
    cells: list[dict[str, str]],
    columns: ColumnMap,
    expected_count: int,
) -> list[str]:
    if columns.pin_type is None or columns.pin_type >= len(cells):
        return [""] * expected_count
    value = _plain_text(cells[columns.pin_type]["inner"])
    if not value:
        return [""] * expected_count

    tokens = TYPE_TOKEN_RE.findall(value)
    if (
        len(tokens) == expected_count
        and "".join(tokens).lower() == re.sub(r"\s+", "", value).lower()
    ):
        return tokens

    # Most cross-page merges keep a single type value such as I/O. Reuse it for
    # each split row instead of requiring an artificial "I/OI/O" pattern.
    return [value] * expected_count


def _description_tokens_for_row(
    cells: list[dict[str, str]],
    columns: ColumnMap,
    expected_count: int,
) -> list[str] | None:
    if columns.description is None or columns.description >= len(cells):
        return None
    value = _plain_text(cells[columns.description]["inner"])
    if not value:
        return [""] * expected_count

    # Description is not needed for pin extraction. Split only at very clear
    # glued sentence boundaries; otherwise copy the original text to avoid data
    # loss or invented punctuation.
    if expected_count == 2:
        parts = re.split(r"(?<=[a-z)])(?=[A-Z][a-z]+\\b)", value, maxsplit=1)
        if len(parts) == 2 and all(part.strip() for part in parts):
            return [parts[0].strip(), parts[1].strip()]
    return [value] * expected_count


def _has_compatible_context(
    rows: list[list[dict[str, str]]],
    row_index: int,
    columns: ColumnMap,
) -> bool:
    """Require nearby rows to look like ordinary rows of the same table."""
    normal_neighbors = 0
    for neighbor_index in range(max(columns.header_rows, row_index - 3), min(len(rows), row_index + 4)):
        if neighbor_index == row_index:
            continue
        neighbor = rows[neighbor_index]
        if _is_normal_pin_row(neighbor, columns):
            normal_neighbors += 1
    return normal_neighbors >= 2


def _is_normal_pin_row(cells: list[dict[str, str]], columns: ColumnMap) -> bool:
    if columns.pin_no >= len(cells) or columns.pin_name >= len(cells):
        return False
    pin_no = _plain_text(cells[columns.pin_no]["inner"])
    pin_name = _plain_text(cells[columns.pin_name]["inner"])
    if not pin_no or not pin_name:
        return False
    if _split_compact_pin_numbers(pin_no):
        return False
    if PIN_TOKEN_RE.fullmatch(pin_no):
        return True
    # Delimited multi-pin rows such as "A2, J4 | VDD" are normal table data.
    if re.search(r"[,，、]", pin_no) and not _split_compact_signal_names(pin_name):
        return True
    return False


def _parse_rows(table_html: str) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    for tr_match in TR_RE.finditer(table_html):
        row = []
        for cell_match in CELL_RE.finditer(tr_match.group(2)):
            row.append(
                {
                    "open": cell_match.group(1),
                    "inner": cell_match.group(2),
                    "close": cell_match.group(3),
                }
            )
        if row:
            rows.append(row)
    return rows


def _rebuild_table(rows: list[list[dict[str, str]]]) -> str:
    return "<table>" + "".join(
        "<tr>" + "".join(cell["open"] + cell["inner"] + cell["close"] for cell in row) + "</tr>"
        for row in rows
    ) + "</table>"


def _plain_text(value: str) -> str:
    value = re.sub(r"<br\\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _normalize_header(value: str) -> str:
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", "", value)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _find_pin_no_column(labels: list[str]) -> int:
    for index, label in enumerate(labels):
        if label in {"pin", "no", "number"}:
            return index
        if any(needle in label for needle in ("pinno", "pinnumber", "ballnumber", "ballnum")):
            return index
    return -1


def _find_first(labels: list[str], needles: tuple[str, ...]) -> int:
    for needle in needles:
        for index, label in enumerate(labels):
            if needle in label:
                return index
    return -1
