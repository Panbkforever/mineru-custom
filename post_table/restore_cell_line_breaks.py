"""Restore table-cell line breaks from PDF-native character coordinates.

VLM table recognition sometimes concatenates visually separate lines inside a
single cell. This module does not wrap text by length. It inserts ``<br>`` only
when the complete HTML cell text matches consecutive visual text runs in the
original PDF table image.
"""

from __future__ import annotations

import html as html_lib
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CELL_RE = re.compile(
    r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class TextRun:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    line_index: int


def restore_cell_line_breaks_in_middle_json(
    middle_json: dict[str, Any],
    pdf_path: str | Path | None,
) -> dict[str, int]:
    stats = {
        "tables_checked": 0,
        "tables_changed": 0,
        "cells_changed": 0,
        "breaks_added": 0,
    }
    if not pdf_path:
        return stats

    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        return stats

    try:
        import pypdfium2 as pdfium
    except Exception:
        return stats

    pdf_info = middle_json.get("pdf_info")
    if not isinstance(pdf_info, list):
        return stats

    try:
        pdf_doc = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return stats

    cache: dict[tuple[int, tuple[float, ...], str], tuple[str, int, int]] = {}
    for page_info in pdf_info:
        page_index = int(page_info.get("page_idx", 0))
        if page_index < 0 or page_index >= len(pdf_doc):
            continue
        page_height = _page_size(page_info)[1]
        if page_height <= 0:
            continue

        text_page = pdf_doc[page_index].get_textpage()
        for span in _iter_html_spans(page_info):
            table_html = span.get("html")
            bbox = span.get("bbox")
            if not isinstance(table_html, str) or not _valid_bbox(bbox):
                continue

            stats["tables_checked"] += 1
            cache_key = (
                page_index,
                tuple(round(float(value), 3) for value in bbox),
                table_html,
            )
            cached = cache.get(cache_key)
            if cached is None:
                runs_by_line = _extract_visual_runs(
                    text_page,
                    [float(value) for value in bbox],
                    page_height,
                )
                cached = _restore_table_cells(table_html, runs_by_line)
                cache[cache_key] = cached

            corrected_html, changed_cells, added_breaks = cached
            span["html"] = corrected_html
            if changed_cells:
                stats["tables_changed"] += 1
                stats["cells_changed"] += changed_cells
                stats["breaks_added"] += added_breaks

    return stats


def _restore_table_cells(
    table_html: str,
    runs_by_line: list[list[TextRun]],
) -> tuple[str, int, int]:
    changed_cells = 0
    added_breaks = 0

    def replace_cell(match: re.Match[str]) -> str:
        nonlocal changed_cells, added_breaks
        inner = match.group(2)
        if "<br" in inner.lower():
            return match.group(0)

        plain = _plain_text(inner)
        if len(_normalize(plain)) < 5:
            return match.group(0)

        parts = _match_visual_lines(plain, runs_by_line)
        if len(parts) < 2:
            return match.group(0)

        boundaries = []
        normalized_length = 0
        for part in parts[:-1]:
            normalized_length += len(_normalize(part))
            boundaries.append(normalized_length)

        corrected = _insert_breaks_by_normalized_offsets(inner, boundaries)
        if corrected == inner:
            return match.group(0)
        changed_cells += 1
        added_breaks += len(boundaries)
        return match.group(1) + corrected + match.group(3)

    return CELL_RE.sub(replace_cell, table_html), changed_cells, added_breaks


def _match_visual_lines(
    cell_text: str,
    runs_by_line: list[list[TextRun]],
) -> list[str]:
    target = _normalize(cell_text)
    if not target:
        return []

    max_line_window = min(40, len(runs_by_line))
    for start_line, runs in enumerate(runs_by_line):
        for first_run in _line_run_variants(runs):
            first = _normalize(first_run.text)
            if not first or not target.startswith(first) or first == target:
                continue

            candidates = [(first_run, [first_run.text], first, 0)]
            for line_index in range(
                start_line + 1,
                min(len(runs_by_line), start_line + max_line_window),
            ):
                next_candidates = []
                line_variants = _line_run_variants(runs_by_line[line_index])
                for previous_run, parts, combined, skipped_lines in candidates:
                    matched_current_line = False
                    same_column_present = any(
                        _normalize(run.text)
                        and _same_cell_column(previous_run, run)
                        for run in runs_by_line[line_index]
                    )
                    for run in line_variants:
                        if not _same_cell_column(previous_run, run):
                            continue
                        run_text = _normalize(run.text)
                        if not run_text:
                            continue
                        next_combined = combined + run_text
                        if not target.startswith(next_combined):
                            continue
                        next_parts = [*parts, run.text]
                        if next_combined == target:
                            return next_parts
                        matched_current_line = True
                        next_candidates.append((run, next_parts, next_combined, 0))
                    if (
                        not matched_current_line
                        and not same_column_present
                        and skipped_lines < 2
                    ):
                        # Other columns can have slightly different baselines.
                        # Skip such a visual line only when it has no text run
                        # overlapping the current cell's horizontal region.
                        next_candidates.append((
                            previous_run,
                            parts,
                            combined,
                            skipped_lines + 1,
                        ))
                candidates = next_candidates
                if not candidates:
                    break
    return []


def _line_run_variants(runs: list[TextRun]) -> list[TextRun]:
    variants = list(runs)
    for start in range(len(runs)):
        for end in range(start + 2, min(len(runs), start + 3) + 1):
            group = runs[start:end]
            variants.append(TextRun(
                text=" ".join(run.text for run in group),
                x0=min(run.x0 for run in group),
                x1=max(run.x1 for run in group),
                y0=min(run.y0 for run in group),
                y1=max(run.y1 for run in group),
                line_index=group[0].line_index,
            ))
    return variants


def _same_cell_column(previous: TextRun, current: TextRun) -> bool:
    overlap = max(0.0, min(previous.x1, current.x1) - max(previous.x0, current.x0))
    narrower = max(1.0, min(previous.x1 - previous.x0, current.x1 - current.x0))
    previous_center = (previous.x0 + previous.x1) / 2.0
    current_center = (current.x0 + current.x1) / 2.0
    wider = max(previous.x1 - previous.x0, current.x1 - current.x0)
    return overlap / narrower >= 0.2 or abs(previous_center - current_center) <= wider * 0.55


def _extract_visual_runs(
    text_page: Any,
    bbox: list[float],
    page_height: float,
) -> list[list[TextRun]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        page_text = text_page.get_text_range()

    characters = []
    for char_index, char in enumerate(page_text):
        if char in "\r\n" or not char.strip():
            continue
        try:
            left, bottom, right, top = text_page.get_charbox(char_index)
        except Exception:
            continue
        top_down_y0 = page_height - top
        top_down_y1 = page_height - bottom
        center_x = (left + right) / 2.0
        center_y = (top_down_y0 + top_down_y1) / 2.0
        if not (
            bbox[0] <= center_x <= bbox[2]
            and bbox[1] <= center_y <= bbox[3]
        ):
            continue
        characters.append({
            "char": char,
            "x0": left,
            "x1": right,
            "y0": top_down_y0,
            "y1": top_down_y1,
            "cy": center_y,
        })
    if not characters:
        return []

    heights = sorted(max(0.5, item["y1"] - item["y0"]) for item in characters)
    median_height = heights[len(heights) // 2]
    y_tolerance = max(1.5, median_height * 0.45)

    visual_lines: list[list[dict[str, Any]]] = []
    line_centers: list[float] = []
    for item in sorted(characters, key=lambda value: (value["cy"], value["x0"])):
        best_index = -1
        best_distance = float("inf")
        for index, center in enumerate(line_centers):
            distance = abs(item["cy"] - center)
            if distance <= y_tolerance and distance < best_distance:
                best_index = index
                best_distance = distance
        if best_index < 0:
            visual_lines.append([item])
            line_centers.append(item["cy"])
        else:
            visual_lines[best_index].append(item)
            line_centers[best_index] = sum(
                value["cy"] for value in visual_lines[best_index]
            ) / len(visual_lines[best_index])

    ordered = sorted(zip(line_centers, visual_lines), key=lambda value: value[0])
    result: list[list[TextRun]] = []
    for line_index, (_, line_chars) in enumerate(ordered):
        line_chars.sort(key=lambda value: value["x0"])
        widths = sorted(max(0.2, value["x1"] - value["x0"]) for value in line_chars)
        median_width = widths[len(widths) // 2]
        run_gap = max(5.0, median_width * 3.2)

        groups = [[line_chars[0]]]
        for item in line_chars[1:]:
            if item["x0"] - groups[-1][-1]["x1"] > run_gap:
                groups.append([item])
            else:
                groups[-1].append(item)

        runs = []
        for group in groups:
            text = _characters_to_text(group)
            if not text:
                continue
            runs.append(TextRun(
                text=text,
                x0=min(value["x0"] for value in group),
                x1=max(value["x1"] for value in group),
                y0=min(value["y0"] for value in group),
                y1=max(value["y1"] for value in group),
                line_index=line_index,
            ))
        result.append(runs)
    return result


def _characters_to_text(characters: list[dict[str, Any]]) -> str:
    if not characters:
        return ""
    widths = sorted(max(0.2, value["x1"] - value["x0"]) for value in characters)
    median_width = widths[len(widths) // 2]
    word_gap = max(1.5, median_width * 0.7)
    parts = [characters[0]["char"]]
    for previous, current in zip(characters, characters[1:]):
        if current["x0"] - previous["x1"] > word_gap:
            parts.append(" ")
        parts.append(current["char"])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _insert_breaks_by_normalized_offsets(
    value: str,
    boundaries: list[int],
) -> str:
    if not boundaries:
        return value

    visible = _visible_character_map(value)
    normalized_count = 0
    insert_positions = []
    boundary_index = 0
    for char, _, raw_end in visible:
        normalized_count += len(_normalize(char))
        while (
            boundary_index < len(boundaries)
            and normalized_count >= boundaries[boundary_index]
        ):
            insert_positions.append(raw_end)
            boundary_index += 1
    if boundary_index != len(boundaries):
        return value

    corrected = value
    for position in reversed(insert_positions):
        corrected = corrected[:position] + "<br>" + corrected[position:]
    return corrected


def _visible_character_map(value: str) -> list[tuple[str, int, int]]:
    result = []
    index = 0
    while index < len(value):
        if value[index] == "<":
            close = value.find(">", index + 1)
            if close < 0:
                break
            index = close + 1
            continue
        if value[index] == "&":
            close = value.find(";", index + 1)
            if close >= 0:
                entity = value[index:close + 1]
                decoded = html_lib.unescape(entity)
                for char in decoded:
                    result.append((char, index, close + 1))
                index = close + 1
                continue
        result.append((value[index], index, index + 1))
        index += 1
    return result


def _plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _normalize(value: str) -> str:
    value = html_lib.unescape(value)
    value = value.replace("−", "-").replace("–", "-").replace("—", "-")
    value = "".join(char for char in value if char.isprintable())
    value = value.replace("_", "")
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
