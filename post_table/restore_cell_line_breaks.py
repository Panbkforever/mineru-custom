"""根据 PDF 原生字符坐标恢复 HTML 表格单元格中的真实换行。

MinerU/VLM 有时会把同一单元格内原本分开的多条视觉文本行直接粘连。本模块
只负责修复解析结果，不参与任何引脚或封装抽取：

* 每个 HTML 单元格独立处理，不比较相邻列的行数。
* 不根据列名、逗号、空格、文本长度或抽取需求猜测换行。
* 只有单元格完整文本能与 PDF 中连续视觉文本行可靠对应时才插入 ``<br>``。
* 只在原 HTML 内容中插入 ``<br>``，不使用 PDF 文字覆盖 MinerU 识别结果。
* 行列边界可靠时，只在当前物理单元格范围内寻找视觉行，禁止借用其他行。
* 若当前文本在 PDF 中存在完整单行，保留原样，不再尝试拆成多个视觉行。
* 无法恢复物理单元格范围时，仅接受位置唯一且中间没有跳行的多行匹配。
* 一个 HTML 表跨 PDF 续表页时，同时检查相关页面的完整单行证据，避免
  当前页中的零散字符把 ``35``、``57`` 等正常值错误拆开。

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


@dataclass(frozen=True)
class LogicalCell:
    """HTML 源单元格在展开 rowspan/colspan 后占用的逻辑网格范围。"""

    row_start: int
    row_end: int
    column_start: int
    column_end: int


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
                table_characters = _extract_pdf_characters(
                    text_page,
                    pdf_bbox,
                    pdf_height,
                )
                runs_by_line = _visual_runs_from_characters(table_characters)
                # MinerU 可能把多个 PDF 续表页合并成一个 HTML <table>，
                # 但只保留起始页的 page_idx/bbox。先收集相邻相关页面中
                # 已经完整位于同一视觉行的文本，供宽范围回退匹配避错。
                known_single_line_texts = _single_line_texts(runs_by_line)
                for related_page_index in _related_pdf_page_indexes(
                    pdf_doc,
                    declared_page_index,
                    table_html,
                    page_text_cache,
                ):
                    if related_page_index == page_index:
                        continue
                    related_page = pdf_doc[related_page_index]
                    related_width, related_height = _pdf_page_size(
                        related_page
                    )
                    if related_width <= 0 or related_height <= 0:
                        continue
                    related_bbox = _scale_bbox_to_pdf(
                        [float(value) for value in bbox],
                        effective_source_width,
                        effective_source_height,
                        related_width,
                        related_height,
                    )
                    related_characters = _extract_pdf_characters(
                        related_page.get_textpage(),
                        related_bbox,
                        related_height,
                    )
                    known_single_line_texts.update(
                        _single_line_texts(
                            _visual_runs_from_characters(
                                related_characters
                            )
                        )
                    )
                logical_cells, row_count, column_count = _logical_cell_layout(
                    table_html
                )
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
                        column_characters = _characters_in_bbox(
                            table_characters,
                            [left, pdf_bbox[1], right, pdf_bbox[3]],
                        )
                        runs_by_column.append(
                            _visual_runs_from_characters(column_characters)
                        )

                # 只有行、列边界都能可靠恢复时，才生成当前单元格专属的
                # 视觉文本行。边界不足时不猜测单元格位置，后面使用保守的
                # 唯一匹配回退逻辑。
                row_boundaries = _detect_table_row_boundaries(
                    page,
                    pdf_bbox,
                    pdf_height,
                    row_count,
                )
                runs_by_cell = None
                if (
                    column_boundaries is not None
                    and row_boundaries is not None
                ):
                    runs_by_cell = []
                    for logical_cell in logical_cells:
                        cell_bbox = [
                            column_boundaries[logical_cell.column_start],
                            row_boundaries[logical_cell.row_start],
                            column_boundaries[logical_cell.column_end],
                            row_boundaries[logical_cell.row_end],
                        ]
                        cell_characters = _characters_in_bbox(
                            table_characters,
                            cell_bbox,
                        )
                        runs_by_cell.append(
                            _visual_runs_from_characters(cell_characters)
                        )
                cached = _restore_table_cells(
                    table_html,
                    runs_by_line,
                    logical_cells=logical_cells,
                    runs_by_column=runs_by_column,
                    runs_by_cell=runs_by_cell,
                    known_single_line_texts=known_single_line_texts,
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
    logical_cells: list[LogicalCell | tuple[int, int]] | None = None,
    runs_by_column: list[list[list[TextRun]]] | None = None,
    runs_by_cell: list[list[list[TextRun]]] | None = None,
    known_single_line_texts: set[str] | None = None,
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
        using_cell_scope = (
            runs_by_cell is not None
            and current_index < len(runs_by_cell)
        )
        candidate_runs = (
            runs_by_cell[current_index]
            if using_cell_scope
            else runs_by_line
        )
        if (
            runs_by_cell is None
            and runs_by_column is not None
            and logical_cells is not None
            and current_index < len(logical_cells)
        ):
            logical_cell = logical_cells[current_index]
            if isinstance(logical_cell, LogicalCell):
                column_start = logical_cell.column_start
                column_end = logical_cell.column_end
            else:
                column_start, column_end = logical_cell
            if (
                column_end == column_start + 1
                and column_start < len(runs_by_column)
            ):
                candidate_runs = runs_by_column[column_start]

        parts = _match_visual_lines(
            plain,
            candidate_runs,
            # 单元格物理边界可靠时，只相信当前单元格；只有退回整列或
            # 整表搜索时，才用续表页的完整单行证据阻止误拆。
            known_single_line_texts=(
                None
                if using_cell_scope
                else known_single_line_texts
            ),
        )
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
    *,
    known_single_line_texts: set[str] | None = None,
) -> list[str]:
    """查找唯一、连续的多视觉行匹配。

    完整单行匹配优先级最高：只要 PDF 范围中存在一条视觉行已经等于
    当前单元格文本，就不能再从其他行拼出同样文本后插入换行。多行匹配
    必须使用连续视觉行，不允许跳过中间行，并且只能存在一种匹配位置。
    """

    target = _normalize(cell_text)
    if not target:
        return []
    if known_single_line_texts and target in known_single_line_texts:
        return []

    single_line_match = False
    multi_line_matches: dict[
        tuple[tuple[int, str], ...],
        list[str],
    ] = {}

    for start_line, runs in enumerate(runs_by_line):
        for first_run in _line_run_variants(runs):
            first = _normalize(first_run.text)
            if not first:
                continue
            if first == target:
                single_line_match = True
                continue
            if not target.startswith(first):
                continue

            candidates = [(first_run, [first_run.text], first)]
            for line_index in range(
                start_line + 1,
                len(runs_by_line),
            ):
                next_candidates = []
                line_variants = _line_run_variants(runs_by_line[line_index])
                for previous_run, parts, combined in candidates:
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
                            match_key = tuple(
                                (start_line + offset, _normalize(part))
                                for offset, part in enumerate(next_parts)
                            )
                            multi_line_matches[match_key] = next_parts
                            continue
                        next_candidates.append((run, next_parts, next_combined))
                candidates = next_candidates
                if not candidates:
                    break

    if single_line_match or len(multi_line_matches) != 1:
        return []
    return next(iter(multi_line_matches.values()))


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


def _single_line_texts(
    runs_by_line: list[list[TextRun]],
) -> set[str]:
    """收集 PDF 中已经完整处于同一视觉行的文本片段。"""

    return {
        normalized
        for runs in runs_by_line
        for run in _line_run_variants(runs)
        if (normalized := _normalize(run.text))
    }


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
    """兼容旧调用：读取范围内字符并按视觉基线组织为文本行。"""

    return _visual_runs_from_characters(
        _extract_pdf_characters(text_page, bbox, page_height)
    )


def _extract_pdf_characters(
    text_page: Any,
    bbox: list[float],
    page_height: float,
) -> list[dict[str, Any]]:
    """一次性读取表格范围内的 PDF 原生字符及其左上角坐标。"""

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
    return characters


def _characters_in_bbox(
    characters: list[dict[str, Any]],
    bbox: list[float],
) -> list[dict[str, Any]]:
    """按字符中心点截取当前逻辑单元格范围，避免读取相邻行列。"""

    return [
        item
        for item in characters
        if (
            bbox[0] <= (item["x0"] + item["x1"]) / 2.0 <= bbox[2]
            and bbox[1] <= item["cy"] <= bbox[3]
        )
    ]


def _visual_runs_from_characters(
    characters: list[dict[str, Any]],
) -> list[list[TextRun]]:
    """把已经限定范围的字符聚类为从上到下的视觉文本行。"""

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


def _related_pdf_page_indexes(
    pdf_doc: Any,
    declared_page_index: int,
    table_html: str,
    page_text_cache: dict[int, str],
) -> list[int]:
    """找出同一个 HTML 表覆盖的相邻 PDF 续表页。

    这里只把包含足够长表格文本锚点的相邻页面视为相关页。普通相邻页面
    不参与保护，避免无关页面中的偶然同名文本干扰当前表格。
    """

    anchors = _table_text_anchors(table_html)
    if not anchors:
        return [declared_page_index]

    related = []
    for candidate_index in range(
        max(0, declared_page_index - 2),
        min(len(pdf_doc), declared_page_index + 3),
    ):
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

        score = sum(
            min(len(anchor), 24)
            for anchor in anchors
            if anchor in normalized_page
        )
        if score >= 24:
            related.append(candidate_index)

    if declared_page_index not in related:
        related.append(declared_page_index)
    return sorted(set(related))


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
    """兼容旧调用：返回每个源单元格占用的逻辑列范围。"""

    cells, _, column_count = _logical_cell_layout(table_html)
    return [
        (cell.column_start, cell.column_end)
        for cell in cells
    ], column_count


def _logical_cell_layout(
    table_html: str,
) -> tuple[list[LogicalCell], int, int]:
    """按 HTML 阅读顺序展开 rowspan/colspan，记录每格的行列范围。"""

    result: list[LogicalCell] = []
    # 保存每个逻辑列被 rowspan 占用到哪一行，结束行使用开区间。
    active_rowspans: dict[int, int] = {}
    maximum_row = 0
    maximum_column = 0

    for row_index, row_match in enumerate(TR_RE.finditer(table_html)):
        active_rowspans = {
            column: row_end
            for column, row_end in active_rowspans.items()
            if row_end > row_index
        }
        blocked_columns = set(active_rowspans)
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
            row_end = row_index + rowspan
            result.append(LogicalCell(
                row_start=row_index,
                row_end=row_end,
                column_start=logical_column,
                column_end=column_end,
            ))
            maximum_row = max(maximum_row, row_end)
            maximum_column = max(maximum_column, column_end)
            if rowspan > 1:
                for column in range(logical_column, column_end):
                    active_rowspans[column] = max(
                        active_rowspans.get(column, 0),
                        row_end,
                    )
            logical_column = column_end

        maximum_row = max(maximum_row, row_index + 1)

    return result, maximum_row, maximum_column


def _detect_table_column_boundaries(
    page: Any,
    bbox: list[float],
    pdf_height: float,
    expected_columns: int,
) -> list[float] | None:
    """从 PDF 矢量竖线中读取表格列边界。

    同一条列边界可能由每个表格行各自绘制的短线段组成，因此先按横坐标
    聚类，再计算所有线段在表格高度方向上的联合覆盖率。
    """

    return _detect_table_vector_boundaries(
        page,
        bbox,
        pdf_height,
        expected_segments=expected_columns,
        axis="vertical",
    )


def _detect_table_row_boundaries(
    page: Any,
    bbox: list[float],
    pdf_height: float,
    expected_rows: int,
) -> list[float] | None:
    """从 PDF 矢量横线中读取表格行边界。"""

    return _detect_table_vector_boundaries(
        page,
        bbox,
        pdf_height,
        expected_segments=expected_rows,
        axis="horizontal",
    )


def _detect_table_vector_boundaries(
    page: Any,
    bbox: list[float],
    pdf_height: float,
    *,
    expected_segments: int,
    axis: str,
) -> list[float] | None:
    """按线段联合覆盖率读取横向或纵向表格边界。

    ``expected_segments`` 是 HTML 展开后的行数或列数，边界数量必须为
    ``expected_segments + 1``。证据不足或边界数量不一致时返回 ``None``，
    禁止使用不可靠边界裁剪字符。
    """

    if expected_segments <= 0 or axis not in {"horizontal", "vertical"}:
        return None

    table_width = bbox[2] - bbox[0]
    table_height = bbox[3] - bbox[1]
    if table_width <= 0 or table_height <= 0:
        return None

    try:
        page_objects = page.get_objects()
    except Exception:
        return None

    candidates: list[tuple[float, tuple[float, float]]] = []
    for page_object in page_objects:
        # PDFium 类型 1 是文字对象，只读取路径和线框对象。
        if getattr(page_object, "type", 1) == 1:
            continue
        bounds = _page_object_bounds(page_object)
        if bounds is None:
            continue
        left, bottom, right, top = bounds
        object_y0 = pdf_height - top
        object_y1 = pdf_height - bottom
        object_width = right - left
        object_height = object_y1 - object_y0

        if axis == "vertical":
            position = (left + right) / 2.0
            interval = (
                max(object_y0, bbox[1]),
                min(object_y1, bbox[3]),
            )
            thickness = object_width
            maximum_thickness = max(2.5, table_width * 0.01)
            inside = bbox[0] - 3.0 <= position <= bbox[2] + 3.0
        else:
            position = (object_y0 + object_y1) / 2.0
            interval = (
                max(left, bbox[0]),
                min(right, bbox[2]),
            )
            thickness = object_height
            maximum_thickness = max(2.5, table_height * 0.01)
            inside = bbox[1] - 3.0 <= position <= bbox[3] + 3.0

        if (
            inside
            and thickness <= maximum_thickness
            and interval[1] - interval[0] > 2.0
        ):
            candidates.append((position, interval))

    if not candidates:
        return None

    clusters: list[list[tuple[float, tuple[float, float]]]] = []
    for candidate in sorted(candidates, key=lambda value: value[0]):
        if (
            not clusters
            or candidate[0] - clusters[-1][-1][0] > 2.0
        ):
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)

    orthogonal_length = table_height if axis == "vertical" else table_width
    clustered: list[tuple[float, float]] = []
    for cluster in clusters:
        intervals = [interval for _, interval in cluster]
        coverage = _interval_union_length(intervals)
        if coverage < orthogonal_length * 0.55:
            continue
        weighted_position = sum(
            position * max(0.1, interval[1] - interval[0])
            for position, interval in cluster
        ) / sum(
            max(0.1, interval[1] - interval[0])
            for _, interval in cluster
        )
        clustered.append((weighted_position, coverage))

    required_boundaries = expected_segments + 1
    if len(clustered) != required_boundaries:
        return None

    # 必须与 HTML 网格数量完全一致。出现额外边界也说明当前 bbox 或
    # HTML 结构存在歧义，不能通过主观选择若干条边界继续修改。
    selected = sorted(clustered, key=lambda value: value[0])
    boundaries = [position for position, _ in selected]
    minimum_segment = (
        table_width * 0.015
        if axis == "vertical"
        else table_height * 0.005
    )
    if any(
        right - left < minimum_segment
        for left, right in zip(boundaries, boundaries[1:])
    ):
        return None
    return boundaries


def _page_object_bounds(page_object: Any) -> tuple[float, float, float, float] | None:
    """兼容不同 pypdfium2 版本的页面对象边界接口。"""

    for method_name in ("get_bounds", "get_pos"):
        method = getattr(page_object, method_name, None)
        if method is None:
            continue
        try:
            left, bottom, right, top = [
                float(value)
                for value in method()
            ]
        except Exception:
            continue
        if right > left and top > bottom:
            return left, bottom, right, top
    return None


def _interval_union_length(
    intervals: list[tuple[float, float]],
) -> float:
    """计算同一边界上多个短线段的联合覆盖长度。"""

    valid = sorted(
        (start, end)
        for start, end in intervals
        if end > start
    )
    if not valid:
        return 0.0

    total = 0.0
    current_start, current_end = valid[0]
    for start, end in valid[1:]:
        if start <= current_end + 1.0:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    return total + current_end - current_start


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
