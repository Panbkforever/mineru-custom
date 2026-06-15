"""
Use PDF-native text coordinates to correct shifted values in MIN/MAX tables.

The VLM may recognize all values correctly but place one value in the wrong
HTML cell, especially in sparse multi-level MIN/MAX tables. This module only
changes a row when all of the following are true:

1. The table header contains separate or VLM-merged MIN/MAX columns.
2. The rendered table has enough horizontal rules to identify its data rows.
3. PDF-native text finds matching MIN/MAX headers and recognized values.
4. Existing columns are only relocated; merged columns are split conservatively.

The original HTML value strings are preserved; PDF text is used only as a
coordinate reference for relocating them.
"""

from __future__ import annotations

import html as html_lib
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)
TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(
    r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class MinMaxCorrectionStats:
    tables_checked: int = 0
    tables_matched: int = 0
    tables_split: int = 0
    columns_added: int = 0
    rows_corrected: int = 0


def correct_min_max_tables_in_middle_json(
    middle_json: dict[str, Any],
    pdf_path: str | Path | None,
) -> dict[str, int]:
    """Correct MIN/MAX table cell alignment in a MinerU middle-json object."""
    stats = MinMaxCorrectionStats()
    if not pdf_path:
        return stats.__dict__

    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        return stats.__dict__

    try:
        import cv2
        import numpy as np
        import pypdfium2 as pdfium
        from post_table.expand_rowspan import expand_colspan, expand_rowspan
    except Exception:
        return stats.__dict__

    pdf_info = middle_json.get("pdf_info")
    if not isinstance(pdf_info, list):
        return stats.__dict__

    try:
        pdf_doc = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return stats.__dict__

    correction_cache: dict[tuple[int, tuple[float, ...], str], str] = {}

    for page_info in pdf_info:
        page_index = int(page_info.get("page_idx", 0))
        if page_index < 0 or page_index >= len(pdf_doc):
            continue

        page_height = _page_size(page_info)[1]
        if page_height <= 0:
            continue

        page = pdf_doc[page_index]
        text_page = page.get_textpage()

        for span in _iter_html_spans(page_info):
            table_html = span.get("html")
            bbox = span.get("bbox")
            if not isinstance(table_html, str) or "<table" not in table_html.lower():
                continue
            if not _valid_bbox(bbox):
                continue

            stats.tables_checked += 1
            cache_key = (
                page_index,
                tuple(round(float(v), 3) for v in bbox),
                table_html,
            )
            if cache_key in correction_cache:
                span["html"] = correction_cache[cache_key]
                continue

            expanded_html = expand_colspan(expand_rowspan(table_html))
            corrected_html, matched, split_columns, corrected_rows = _correct_one_table(
                expanded_html=expanded_html,
                bbox=[float(v) for v in bbox],
                page=page,
                text_page=text_page,
                page_height=page_height,
                cv2=cv2,
                np=np,
            )
            if matched:
                stats.tables_matched += 1
            if split_columns:
                stats.tables_split += 1
                stats.columns_added += split_columns
            stats.rows_corrected += corrected_rows
            correction_cache[cache_key] = corrected_html
            span["html"] = corrected_html

    return stats.__dict__


def _correct_one_table(
    expanded_html: str,
    bbox: list[float],
    page: Any,
    text_page: Any,
    page_height: float,
    cv2: Any,
    np: Any,
) -> tuple[str, bool, int, int]:
    rows = _parse_expanded_rows(expanded_html)
    if len(rows) < 3:
        return expanded_html, False, 0, 0

    horizontal_lines = _detect_horizontal_lines(page, bbox, cv2, np)

    header_row_index, value_column_indexes = _find_min_max_header(rows)
    if header_row_index < 0:
        return _split_merged_min_max_columns(
            rows=rows,
            bbox=bbox,
            text_page=text_page,
            page_height=page_height,
            horizontal_lines=horizontal_lines,
        )
    if len(horizontal_lines) != len(rows) + 1:
        return expanded_html, False, 0, 0
    if len(value_column_indexes) < 2:
        return expanded_html, False, 0, 0

    labels = [_plain_text(rows[header_row_index][i]["inner"]).upper() for i in value_column_indexes]
    if not _alternating_min_max(labels):
        return expanded_html, False, 0, 0

    header_y0 = horizontal_lines[header_row_index]
    header_y1 = horizontal_lines[header_row_index + 1]
    pdf_headers = _find_pdf_min_max_headers(
        text_page,
        bbox,
        page_height,
        header_y0,
        header_y1,
    )
    if [item[0] for item in pdf_headers] != labels:
        return expanded_html, False, 0, 0

    centers = [item[1] for item in pdf_headers]
    x_ranges = _column_ranges_from_centers(centers, bbox[0], bbox[2])
    if len(x_ranges) != len(value_column_indexes):
        return expanded_html, False, 0, 0

    corrected_rows = 0
    for row_index in range(header_row_index + 1, len(rows)):
        cells = rows[row_index]
        if max(value_column_indexes) >= len(cells):
            continue

        html_values = [cells[i]["inner"] for i in value_column_indexes]
        html_mask = [bool(_plain_text(value)) for value in html_values]
        if not any(html_mask):
            continue

        row_y0 = horizontal_lines[row_index]
        row_y1 = horizontal_lines[row_index + 1]
        pdf_values = [
            _extract_pdf_cell_text(
                text_page,
                left=x0,
                right=x1,
                top_down_y0=row_y0,
                top_down_y1=row_y1,
                page_height=page_height,
            )
            for x0, x1 in x_ranges
        ]
        pdf_mask = [bool(value) for value in pdf_values]

        if html_mask == pdf_mask:
            continue

        html_non_empty = [value for value in html_values if _plain_text(value)]
        pdf_non_empty = [value for value in pdf_values if value]
        if len(html_non_empty) != len(pdf_non_empty):
            continue

        if [
            _normalize_value(_plain_text(value))
            for value in html_non_empty
        ] != [
            _normalize_value(value)
            for value in pdf_non_empty
        ]:
            continue

        value_iter = iter(html_non_empty)
        relocated = [next(value_iter) if occupied else "" for occupied in pdf_mask]
        for column_index, value in zip(value_column_indexes, relocated):
            cells[column_index]["inner"] = value
        corrected_rows += 1

    if corrected_rows == 0:
        return expanded_html, True, 0, 0
    return _rebuild_table(rows), True, 0, corrected_rows


def _split_merged_min_max_columns(
    rows: list[list[dict[str, str]]],
    bbox: list[float],
    text_page: Any,
    page_height: float,
    horizontal_lines: list[float],
) -> tuple[str, bool, int, int]:
    """Split cells where VLM collapsed one MIN/MAX pair into one HTML column."""
    header_row_index, merged_indexes = _find_merged_min_max_header(rows)
    if header_row_index < 0:
        return _rebuild_table(rows), False, 0, 0

    data_row_count = len(rows) - header_row_index - 1
    if data_row_count <= 0 or len(horizontal_lines) < data_row_count + 2:
        return _rebuild_table(rows), False, 0, 0

    # Expanded rowspan headers may occupy one physical band even though they
    # appear as several HTML rows. The final N+1 horizontal intervals always
    # belong to the N data rows; everything above them is the header band.
    data_lines = horizontal_lines[-(data_row_count + 1):]
    header_y0 = horizontal_lines[0]
    header_y1 = data_lines[0]
    pdf_headers = _find_pdf_min_max_headers(
        text_page,
        bbox,
        page_height,
        header_y0,
        header_y1,
    )
    labels = [item[0] for item in pdf_headers]
    if len(pdf_headers) != len(merged_indexes) * 2 or not _alternating_min_max(labels):
        return _rebuild_table(rows), False, 0, 0

    centers = [item[1] for item in pdf_headers]
    split_rows: list[list[dict[str, str]]] = []
    corrected_rows = 0

    for row_index, cells in enumerate(rows):
        new_cells: list[dict[str, str]] = []
        row_changed = False
        merged_set = set(merged_indexes)
        for column_index, cell in enumerate(cells):
            if column_index not in merged_set:
                new_cells.append(cell)
                continue

            group_index = merged_indexes.index(column_index)
            min_center, max_center = centers[group_index * 2:group_index * 2 + 2]

            if row_index < header_row_index:
                left_inner = right_inner = cell["inner"]
            elif row_index == header_row_index:
                prefix = _merged_header_prefix(cell["inner"])
                left_inner = f"{prefix} MIN".strip()
                right_inner = f"{prefix} MAX".strip()
            else:
                data_index = row_index - header_row_index - 1
                left_inner, right_inner = _place_merged_value(
                    cell["inner"],
                    text_page=text_page,
                    left=min_center - (max_center - min_center) / 2.0,
                    right=max_center + (max_center - min_center) / 2.0,
                    min_center=min_center,
                    max_center=max_center,
                    row_y0=data_lines[data_index],
                    row_y1=data_lines[data_index + 1],
                    page_height=page_height,
                )
                if _plain_text(cell["inner"]):
                    row_changed = True

            new_cells.extend([
                _clone_cell(cell, left_inner),
                _clone_cell(cell, right_inner),
            ])
        split_rows.append(new_cells)
        if row_changed:
            corrected_rows += 1

    return _rebuild_table(split_rows), True, len(merged_indexes), corrected_rows


def _parse_expanded_rows(table_html: str) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    for tr_match in TR_RE.finditer(table_html):
        row = []
        for cell_match in CELL_RE.finditer(tr_match.group(2)):
            row.append({
                "open": cell_match.group(1),
                "inner": cell_match.group(2),
                "close": cell_match.group(3),
            })
        if row:
            rows.append(row)
    return rows


def _find_min_max_header(rows: list[list[dict[str, str]]]) -> tuple[int, list[int]]:
    for row_index, cells in enumerate(rows):
        labels = [_plain_text(cell["inner"]).upper() for cell in cells]
        indexes = [i for i, label in enumerate(labels) if label in {"MIN", "MAX"}]
        if len(indexes) >= 2:
            return row_index, indexes
    return -1, []


def _find_merged_min_max_header(
    rows: list[list[dict[str, str]]],
) -> tuple[int, list[int]]:
    for row_index, cells in enumerate(rows):
        indexes = [
            index
            for index, cell in enumerate(cells)
            if re.search(r"MIN\s*MAX", _plain_text(cell["inner"]), re.IGNORECASE)
        ]
        if indexes:
            return row_index, indexes
    return -1, []


def _merged_header_prefix(value: str) -> str:
    text = _plain_text(value)
    return re.sub(r"MIN\s*MAX.*$", "", text, flags=re.IGNORECASE).strip()


def _clone_cell(cell: dict[str, str], inner: str) -> dict[str, str]:
    return {
        "open": cell["open"],
        "inner": inner,
        "close": cell["close"],
    }


def _place_merged_value(
    html_value: str,
    text_page: Any,
    left: float,
    right: float,
    min_center: float,
    max_center: float,
    row_y0: float,
    row_y1: float,
    page_height: float,
) -> tuple[str, str]:
    if not _plain_text(html_value):
        return "", ""

    value_center = _find_pdf_value_center(
        text_page=text_page,
        value=_plain_text(html_value),
        left=left,
        right=right,
        row_y0=row_y0,
        row_y1=row_y1,
        page_height=page_height,
    )
    if value_center is None:
        # Keep the recognized value even when native PDF text cannot be matched.
        return html_value, ""

    pair_midpoint = (min_center + max_center) / 2.0
    center_tolerance = (max_center - min_center) * 0.18
    if abs(value_center - pair_midpoint) <= center_tolerance:
        # A value centered across the unsplit pair semantically spans MIN/MAX.
        return html_value, html_value
    if value_center < pair_midpoint:
        return html_value, ""
    return "", html_value


def _find_pdf_value_center(
    text_page: Any,
    value: str,
    left: float,
    right: float,
    row_y0: float,
    row_y1: float,
    page_height: float,
) -> float | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        page_text = text_page.get_text_range()

    normalized_text, char_indexes = _normalized_text_with_indexes(page_text)
    needle = _normalize_value(value)
    if not needle:
        return None

    start = 0
    while True:
        match_index = normalized_text.find(needle, start)
        if match_index < 0:
            return None
        original_indexes = char_indexes[match_index:match_index + len(needle)]
        boxes = []
        for char_index in original_indexes:
            try:
                char_left, bottom, char_right, top = text_page.get_charbox(char_index)
            except Exception:
                boxes = []
                break
            boxes.append((
                char_left,
                page_height - top,
                char_right,
                page_height - bottom,
            ))
        if boxes:
            x0 = min(box[0] for box in boxes)
            x1 = max(box[2] for box in boxes)
            y0 = min(box[1] for box in boxes)
            y1 = max(box[3] for box in boxes)
            center_x = (x0 + x1) / 2.0
            center_y = (y0 + y1) / 2.0
            if left <= center_x <= right and row_y0 <= center_y <= row_y1:
                return center_x
        start = match_index + 1


def _normalized_text_with_indexes(value: str) -> tuple[str, list[int]]:
    normalized_chars = []
    indexes = []
    for index, char in enumerate(value):
        normalized = _normalize_value(char)
        for normalized_char in normalized:
            normalized_chars.append(normalized_char)
            indexes.append(index)
    return "".join(normalized_chars), indexes


def _alternating_min_max(labels: list[str]) -> bool:
    if len(labels) < 2 or len(labels) % 2:
        return False
    return all(
        label == ("MIN" if index % 2 == 0 else "MAX")
        for index, label in enumerate(labels)
    )


def _detect_horizontal_lines(page: Any, bbox: list[float], cv2: Any, np: Any) -> list[float]:
    scale = 4.0
    image = np.asarray(page.render(scale=scale).to_pil().convert("L"))
    x0, y0, x1, y1 = bbox
    crop = image[
        max(0, int(y0 * scale)):min(image.shape[0], int(y1 * scale)),
        max(0, int(x0 * scale)):min(image.shape[1], int(x1 * scale)),
    ]
    if crop.size == 0:
        return []

    binary = cv2.threshold(crop, 180, 255, cv2.THRESH_BINARY_INV)[1]
    kernel_width = max(20, int(crop.shape[1] * 0.55))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    projection = (horizontal > 0).mean(axis=1)
    candidate_rows = np.where(projection >= 0.5)[0].tolist()

    groups = _merge_consecutive(candidate_rows)
    return [
        y0 + ((start + end) / 2.0) / scale
        for start, end in groups
        if 1 <= end - start + 1 <= 20
    ]


def _find_pdf_min_max_headers(
    text_page: Any,
    bbox: list[float],
    page_height: float,
    row_y0: float,
    row_y1: float,
) -> list[tuple[str, float]]:
    # pypdfium2 的 bounded 全文顺序与 get_charbox() 的字符索引一致。
    # 显式 range 模式在部分 PDF 中使用不同的换行/字符计数，反而会让
    # MIN/MAX 文本索引和字符坐标错位，因此这里保留默认 bounded 行为。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        text = text_page.get_text_range()
    results: list[tuple[str, float]] = []
    for label in ("MIN", "MAX"):
        start = 0
        while True:
            index = text.find(label, start)
            if index < 0:
                break
            boxes = []
            for char_index in range(index, index + len(label)):
                left, bottom, right, top = text_page.get_charbox(char_index)
                boxes.append((left, page_height - top, right, page_height - bottom))
            x0 = min(box[0] for box in boxes)
            y0 = min(box[1] for box in boxes)
            x1 = max(box[2] for box in boxes)
            y1 = max(box[3] for box in boxes)
            center_y = (y0 + y1) / 2.0
            if (
                bbox[0] <= x0 <= bbox[2]
                and row_y0 <= center_y <= row_y1
            ):
                results.append((label, (x0 + x1) / 2.0))
            start = index + 1
    return sorted(results, key=lambda item: item[1])


def _column_ranges_from_centers(
    centers: list[float],
    table_left: float,
    table_right: float,
) -> list[tuple[float, float]]:
    if len(centers) < 2:
        return []
    midpoints = [(left + right) / 2.0 for left, right in zip(centers, centers[1:])]
    first_gap = centers[1] - centers[0]
    last_gap = centers[-1] - centers[-2]
    left_edge = max(table_left, centers[0] - first_gap / 2.0)
    right_edge = min(table_right, centers[-1] + last_gap / 2.0)
    edges = [left_edge, *midpoints, right_edge]
    return list(zip(edges, edges[1:]))


def _extract_pdf_cell_text(
    text_page: Any,
    left: float,
    right: float,
    top_down_y0: float,
    top_down_y1: float,
    page_height: float,
) -> str:
    value = text_page.get_text_bounded(
        left=left,
        bottom=page_height - top_down_y1,
        right=right,
        top=page_height - top_down_y0,
    )
    return re.sub(r"\s+", " ", value).strip()


def _rebuild_table(rows: list[list[dict[str, str]]]) -> str:
    row_html = []
    for cells in rows:
        row_html.append(
            "<tr>"
            + "".join(cell["open"] + cell["inner"] + cell["close"] for cell in cells)
            + "</tr>"
        )
    return "<table>" + "".join(row_html) + "</table>"


def _plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _normalize_value(value: str) -> str:
    value = value.replace("−", "-").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", "", value).lower()


def _iter_html_spans(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("html"), str) and "<table" in value["html"].lower():
            yield value
        for child in value.values():
            yield from _iter_html_spans(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_html_spans(child)


def _merge_consecutive(values: list[int]) -> list[tuple[int, int]]:
    if not values:
        return []
    groups = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        groups.append((start, previous))
        start = previous = value
    groups.append((start, previous))
    return groups


def _page_size(page_info: dict[str, Any]) -> tuple[float, float]:
    page_size = page_info.get("page_size")
    if isinstance(page_size, (list, tuple)) and len(page_size) >= 2:
        return float(page_size[0]), float(page_size[1])
    return 0.0, 0.0


def _valid_bbox(bbox: Any) -> bool:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    try:
        x0, y0, x1, y1 = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return False
    return x1 > x0 and y1 > y0
