"""根据 PDF 原生字符坐标恢复 HTML 表格单元格中的真实换行。

MinerU/VLM 有时会把同一单元格内原本分开的多条视觉文本行直接粘连。本模块
只负责修复解析结果，不参与任何引脚或封装抽取：

* 每个 HTML 单元格独立处理，不比较相邻列的行数。
* 不根据列名、逗号、空格、文本长度或抽取需求猜测换行。
* 只有单元格完整文本能与 PDF 中连续视觉文本行可靠对应时才插入 ``<br>``。
* 只在原 HTML 内容中插入 ``<br>``，不使用 PDF 文字覆盖 MinerU 识别结果。

MinerU 的 ``page_size`` 和表格 ``bbox`` 可能使用渲染像素坐标，而 PDFium
字符坐标使用 PDF 点坐标。读取字符前必须先把表格范围换算到 PDF 坐标系。
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
TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
SPAN_RE = re.compile(r"\b(rowspan|colspan)\s*=\s*[\"']?(\d+)", re.IGNORECASE)


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
    page_text_cache: dict[int, str] = {}
    for page_info in pdf_info:
        declared_page_index = int(page_info.get("page_idx", 0))
        if declared_page_index < 0 or declared_page_index >= len(pdf_doc):
            continue

        source_width, source_height = _page_size(page_info)
        for span in _iter_html_spans(page_info):
            table_html = span.get("html")
            bbox = span.get("bbox")
            if not isinstance(table_html, str) or not _valid_bbox(bbox):
                continue

            # 个别 MinerU 输出的 page_idx 与原 PDF 实际页码存在一页偏移。
            # 使用当前表格自身的文本锚点在相邻页面中校正，不依赖文档标题、
            # 表格业务类型或任何抽取字段。
            page_index = _resolve_pdf_page_index(
                pdf_doc,
                declared_page_index,
                table_html,
                page_text_cache,
            )
            page = pdf_doc[page_index]
            pdf_width, pdf_height = _pdf_page_size(page)
            if pdf_width <= 0 or pdf_height <= 0:
                continue

            effective_source_width = source_width or pdf_width
            effective_source_height = source_height or pdf_height
            # middle_json 的 bbox 与 PDFium 字符框不一定处于同一尺度。
            # 统一转换为 PDF 点坐标，并继续使用左上角为原点的方向。
            pdf_bbox = _scale_bbox_to_pdf(
                [float(value) for value in bbox],
                effective_source_width,
                effective_source_height,
                pdf_width,
                pdf_height,
            )
            stats["tables_checked"] += 1
            cache_key = (
                page_index,
                tuple(round(value, 3) for value in pdf_bbox),
                table_html,
            )
            cached = cache.get(cache_key)
            if cached is None:
                text_page = page.get_textpage()
                runs_by_line = _extract_visual_runs(
                    text_page,
                    pdf_bbox,
                    pdf_height,
                )
                logical_cells, column_count = _logical_cell_columns(table_html)
                column_boundaries = _detect_table_column_boundaries(
                    page,
                    pdf_bbox,
                    pdf_height,
                    column_count,
                )
                runs_by_column = None
                if column_boundaries is not None:
                    runs_by_column = []
                    for left, right in zip(
                        column_boundaries,
                        column_boundaries[1:],
                    ):
                        runs_by_column.append(_extract_visual_runs(
                            text_page,
                            [left, pdf_bbox[1], right, pdf_bbox[3]],
                            pdf_height,
                        ))
                cached = _restore_table_cells(
                    table_html,
                    runs_by_line,
                    logical_cells=logical_cells,
                    runs_by_column=runs_by_column,
                )
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
    *,
    logical_cells: list[tuple[int, int]] | None = None,
    runs_by_column: list[list[list[TextRun]]] | None = None,
) -> tuple[str, int, int]:
    changed_cells = 0
    added_breaks = 0
    cell_index = 0

    def replace_cell(match: re.Match[str]) -> str:
        nonlocal changed_cells, added_breaks, cell_index
        current_index = cell_index
        cell_index += 1
        inner = match.group(2)
        if "<br" in inner.lower():
            return match.group(0)

        plain = _plain_text(inner)
        if not _normalize(plain):
            return match.group(0)

        # 单元格独立匹配 PDF 中的视觉文本行。这里不读取列名，也不要求
        # 当前单元格与其他列具有相同的行数。
        candidate_runs = runs_by_line
        if (
            runs_by_column is not None
            and logical_cells is not None
            and current_index < len(logical_cells)
        ):
            column_start, column_end = logical_cells[current_index]
            if (
                column_end == column_start + 1
                and column_start < len(runs_by_column)
            ):
                candidate_runs = runs_by_column[column_start]

        parts = _match_visual_lines(plain, candidate_runs)
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

    # PDFium 返回的是紧字符框。逗号和下划线的中心通常明显低于同一行的
    # 字母数字，直接按全部字符聚类会把标点误判成独立视觉行。因此先用
    # 字母数字建立基线，再把标点归入距离最近的真实基线。
    anchor_characters = [
        item for item in characters
        if str(item["char"]).isalnum()
    ]
    floating_characters = [
        item for item in characters
        if not str(item["char"]).isalnum()
    ]
    if not anchor_characters:
        anchor_characters = characters
        floating_characters = []

    visual_lines: list[list[dict[str, Any]]] = []
    line_centers: list[float] = []
    for item in sorted(
        anchor_characters,
        key=lambda value: (value["cy"], value["x0"]),
    ):
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

    punctuation_tolerance = max(4.0, median_height * 1.5)
    for item in floating_characters:
        if not line_centers:
            break
        nearest_index = min(
            range(len(line_centers)),
            key=lambda index: abs(item["cy"] - line_centers[index]),
        )
        if abs(item["cy"] - line_centers[nearest_index]) <= punctuation_tolerance:
            visual_lines[nearest_index].append(item)

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


def _resolve_pdf_page_index(
    pdf_doc: Any,
    declared_page_index: int,
    table_html: str,
    page_text_cache: dict[int, str],
) -> int:
    """用表格文本锚点在相邻页面中校正 middle_json 页码。

    只比较解析文本是否出现在候选 PDF 页，不读取表格字段语义。若没有
    足够证据证明其他页面更匹配，则保留 middle_json 原页码。
    """

    anchors = _table_text_anchors(table_html)
    if not anchors:
        return declared_page_index

    candidate_indexes = range(
        max(0, declared_page_index - 2),
        min(len(pdf_doc), declared_page_index + 3),
    )
    scores: dict[int, int] = {}
    for candidate_index in candidate_indexes:
        normalized_page = page_text_cache.get(candidate_index)
        if normalized_page is None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    page_text = (
                        pdf_doc[candidate_index]
                        .get_textpage()
                        .get_text_range()
                    )
            except Exception:
                page_text = ""
            normalized_page = _normalize(page_text)
            page_text_cache[candidate_index] = normalized_page
        scores[candidate_index] = sum(
            min(len(anchor), 24)
            for anchor in anchors
            if anchor in normalized_page
        )

    declared_score = scores.get(declared_page_index, 0)
    best_index = max(scores, key=scores.get)
    best_score = scores[best_index]
    if best_score >= 24 and best_score > declared_score:
        return best_index
    return declared_page_index


def _table_text_anchors(table_html: str) -> list[str]:
    """从表格原文提取用于页码校正的非业务文本片段。"""

    plain = _plain_text(table_html)
    candidates = re.findall(
        r"[A-Za-z0-9][A-Za-z0-9_:\[\].+\-/]{4,}",
        plain,
    )
    generic = {
        "table",
        "signal",
        "name",
        "type",
        "description",
        "number",
    }
    anchors = []
    seen = set()
    for candidate in sorted(candidates, key=len, reverse=True):
        normalized = _normalize(candidate)
        if (
            len(normalized) < 5
            or normalized in generic
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        anchors.append(normalized)
        if len(anchors) >= 40:
            break
    return anchors


def _logical_cell_columns(
    table_html: str,
) -> tuple[list[tuple[int, int]], int]:
    """按 HTML 阅读顺序计算每个源单元格占用的逻辑列范围。"""

    result: list[tuple[int, int]] = []
    active_rowspans: dict[int, int] = {}
    maximum_column = 0

    for row_match in TR_RE.finditer(table_html):
        blocked_columns = set(active_rowspans)
        new_rowspans: dict[int, int] = {}
        logical_column = 0

        for cell_match in CELL_RE.finditer(row_match.group(1)):
            attrs = cell_match.group(1)
            spans = {
                name.lower(): max(1, int(value))
                for name, value in SPAN_RE.findall(attrs)
            }
            colspan = spans.get("colspan", 1)
            rowspan = spans.get("rowspan", 1)

            while logical_column in blocked_columns:
                logical_column += 1
            while any(
                column in blocked_columns
                for column in range(logical_column, logical_column + colspan)
            ):
                logical_column += 1
                while logical_column in blocked_columns:
                    logical_column += 1

            column_end = logical_column + colspan
            result.append((logical_column, column_end))
            maximum_column = max(maximum_column, column_end)
            if rowspan > 1:
                for column in range(logical_column, column_end):
                    new_rowspans[column] = max(
                        new_rowspans.get(column, 0),
                        rowspan - 1,
                    )
            logical_column = column_end

        active_rowspans = {
            column: remaining - 1
            for column, remaining in active_rowspans.items()
            if remaining > 1
        }
        for column, remaining in new_rowspans.items():
            active_rowspans[column] = max(
                active_rowspans.get(column, 0),
                remaining,
            )

    return result, maximum_column


def _detect_table_column_boundaries(
    page: Any,
    bbox: list[float],
    pdf_height: float,
    expected_columns: int,
) -> list[float] | None:
    """从 PDF 矢量竖线中读取表格列边界。

    只接受贯穿大部分表格高度、数量与 HTML 逻辑列完全一致的竖线集合。
    证据不足时返回 ``None``，由调用方退回整表范围匹配。
    """

    if expected_columns <= 0:
        return None

    table_width = bbox[2] - bbox[0]
    table_height = bbox[3] - bbox[1]
    if table_width <= 0 or table_height <= 0:
        return None

    candidates: list[tuple[float, float]] = []
    try:
        objects = page.get_objects()
    except Exception:
        return None

    for page_object in objects:
        # PDFium 类型 1 是文字对象。这里只读取路径/线框对象，避免把
        # 细长文字笔画误认为表格竖线。
        if getattr(page_object, "type", 1) == 1:
            continue
        try:
            left, bottom, right, top = [
                float(value) for value in page_object.get_pos()
            ]
        except Exception:
            continue

        object_y0 = pdf_height - top
        object_y1 = pdf_height - bottom
        object_width = right - left
        overlap_y = max(
            0.0,
            min(object_y1, bbox[3]) - max(object_y0, bbox[1]),
        )
        if (
            object_width <= max(2.5, table_width * 0.01)
            and overlap_y >= table_height * 0.75
            and bbox[0] - 3.0 <= (left + right) / 2.0 <= bbox[2] + 3.0
        ):
            candidates.append(((left + right) / 2.0, overlap_y))

    if not candidates:
        return None

    clusters: list[list[tuple[float, float]]] = []
    for candidate in sorted(candidates):
        if not clusters or candidate[0] - clusters[-1][-1][0] > 2.0:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)

    clustered = [
        (
            sum(x * length for x, length in cluster)
            / max(1.0, sum(length for _, length in cluster)),
            max(length for _, length in cluster),
        )
        for cluster in clusters
    ]
    if len(clustered) < expected_columns + 1:
        return None

    # 若候选竖线略多，优先保留贯穿高度最长的边界，再按横坐标排序。
    selected = sorted(
        sorted(clustered, key=lambda item: item[1], reverse=True)[
            :expected_columns + 1
        ],
        key=lambda item: item[0],
    )
    boundaries = [x for x, _ in selected]
    minimum_width = table_width * 0.015
    if any(
        right - left < minimum_width
        for left, right in zip(boundaries, boundaries[1:])
    ):
        return None
    return boundaries


def _pdf_page_size(page: Any) -> tuple[float, float]:
    """读取 PDFium 页面尺寸，返回 PDF 点坐标下的宽和高。"""

    try:
        width, height = page.get_size()
        return float(width), float(height)
    except Exception:
        return 0.0, 0.0


def _scale_bbox_to_pdf(
    bbox: list[float],
    source_width: float,
    source_height: float,
    pdf_width: float,
    pdf_height: float,
) -> list[float]:
    """把 middle_json 表格范围换算到 PDFium 使用的 PDF 点坐标。

    两个坐标系都按页面左上角表示表格范围，因此这里只做横纵尺度换算，
    不改变 y 轴方向。PDF 字符框的 y 轴转换仍由 ``_extract_visual_runs``
    统一完成。
    """

    if (
        source_width <= 0
        or source_height <= 0
        or pdf_width <= 0
        or pdf_height <= 0
    ):
        return list(bbox)

    scale_x = pdf_width / source_width
    scale_y = pdf_height / source_height
    x0, y0, x1, y1 = bbox
    return [
        x0 * scale_x,
        y0 * scale_y,
        x1 * scale_x,
        y1 * scale_y,
    ]


def _valid_bbox(bbox: Any) -> bool:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    try:
        x0, y0, x1, y1 = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return False
    return x1 > x0 and y1 > y0
