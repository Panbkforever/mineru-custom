"""Read proven Elan pin grids from native PDF text, without OCR or an LLM."""

from __future__ import annotations

from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
import re
import warnings


@dataclass
class NativeTable:
    page: int
    title: str
    rows: list[list[str]]
    columns: tuple[float, ...]


def _cluster(values: list[float], tolerance: float = 1.1) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _walk(item):
    yield item
    if hasattr(item, "__iter__"):
        for child in item:
            yield from _walk(child)


def _cell(chars, box) -> str:
    left, bottom, right, top = box
    selected = [char for char in chars if left < (char.x0 + char.x1) / 2 < right
                and bottom < (char.y0 + char.y1) / 2 < top]
    lines: list[list] = []
    for char in sorted(selected, key=lambda char: (-(char.y0 + char.y1) / 2, char.x0)):
        cy = (char.y0 + char.y1) / 2
        if not lines or abs(cy - (lines[-1][0].y0 + lines[-1][0].y1) / 2) > 2:
            lines.append([char])
        else:
            lines[-1].append(char)
    return "\n".join("".join(c.get_text() for c in sorted(line, key=lambda c: c.x0)).strip()
                     for line in lines).strip()


def _key(value: str) -> str:
    return re.sub(r"[\s.#]+", "", value).lower()


def is_pin_header(row: list[str]) -> bool:
    if len(row) < 4:
        return False
    return (_key(row[0]) in {"symbol", "pinname", "pinnames", "符号", "名称", "引脚名称"}
            and _key(row[1]) in {"pinno", "pinnumber", "引脚序号", "引脚编号", "引脚号", "引脚"}
            and _key(row[2]) in {"type", "pintype", "类型", "引脚类型"}
            and _key(row[3]) in {"function", "description", "功能", "说明"})


def _physical_row(row: list[str]) -> bool:
    return (len(row) == 4 and bool(re.search(r"[A-Za-z]", row[0]))
            and bool(re.fullmatch(r"[\d\s,;~\-\u2013\u2014\uff5e]+", row[1]))
            and bool(re.fullmatch(r"[IOP/\s\-\u2013\u2014]+", row[2], re.I)))


def scope_from_title(title: str) -> str:
    match = re.search(r"(?:\b\d+(?:\.\d+)+\s+|Pin\s+Description\s*[-:]\s*)"
                      r"([A-Z][A-Z0-9]*\d[A-Z0-9/]*(?:\s*[-\u2212]\s*\d+\s*pins?)?)", title, re.I)
    return re.sub(r"\s+", " ", match[0]).strip() if match else ""


def _page_grids(layout, page: int, carried: NativeTable | None) -> list[NativeTable]:
    from pdfminer.layout import LTChar, LTLine, LTRect

    objects = list(_walk(layout))
    chars = [obj for obj in objects if isinstance(obj, LTChar)]
    lines = [obj for obj in objects if isinstance(obj, (LTLine, LTRect))]
    vertical = [obj for obj in lines if obj.width < 2 and obj.height > 5]
    horizontal = [(obj.x0, obj.x1, (obj.y0 + obj.y1) / 2) for obj in lines if obj.height < 2 and obj.width > 10]
    xs = _cluster([(obj.x0 + obj.x1) / 2 for obj in vertical], 2.5)
    headers = []
    boundaries = sorted(_cluster([y for _, _, y in horizontal], 2), reverse=True)
    for top, bottom in zip(boundaries, boundaries[1:]):
        if not 7 < top - bottom < 60:
            continue
        mid = (top + bottom) / 2
        row_xs = _cluster([(obj.x0 + obj.x1) / 2 for obj in vertical if obj.y0 <= mid <= obj.y1], 2.5)
        for start in range(len(row_xs) - 4):
            columns = tuple(row_xs[start:start + 5])
            if min(b - a for a, b in zip(columns, columns[1:])) < 15:
                continue
            row = [_cell(chars, (a, bottom + .4, b, top - .4)) for a, b in zip(columns, columns[1:])]
            if is_pin_header(row):
                above = _cell(chars, (40, top, layout.x1 - 20, min(layout.y1 - 50, top + 110)))
                scopes = [line for line in above.splitlines() if scope_from_title(line)]
                headers.append((top, bottom, columns, scopes[-1] if scopes else ""))
    headers.sort(reverse=True)
    blocks = list(headers)
    if carried and all(any(abs(x - known) < 2 for known in xs) for x in carried.columns):
        first_header = max((item[0] for item in headers), default=50)
        if first_header < layout.y1 - 130:
            blocks.insert(0, (layout.y1 - 70, layout.y1 - 70, carried.columns, carried.title))
    result = []
    for n, (top, bottom, columns, title) in enumerate(blocks):
        lower = blocks[n + 1][0] if n + 1 < len(blocks) else 45
        ys = sorted(_cluster([y for x0, x1, y in horizontal
                              if x0 <= columns[0] + 2 and x1 >= columns[1] - 2
                              and lower - 2 <= y <= bottom + 2], 2), reverse=True)
        rows = []
        for row_top, row_bottom in zip(ys, ys[1:]):
            if row_top - row_bottom < 4:
                continue
            row = [_cell(chars, (a, row_bottom + .4, b, row_top - .4)) for a, b in zip(columns, columns[1:])]
            if _physical_row(row):
                rows.append(row)
        if rows:
            result.append(NativeTable(page, title or (carried.title if carried else ""), rows, columns))
    return result


def recover_elan_tables(tables: list, source_pdf_path: str | Path | None) -> list:
    """Replace only pin candidates backed by a fully identified native grid."""
    if source_pdf_path is None or not Path(source_pdf_path).is_file():
        return tables
    from extract.pin_package_extractor import parse_spanned_table

    eligible = []
    for index, table in enumerate(tables):
        rows = parse_spanned_table(table.html)
        if any(is_pin_header(row) for row in rows[:3]) or (rows and any(_physical_row(row[:4]) for row in rows[:5])):
            if isinstance(table.page_idx, int):
                eligible.append((index, table))
    if not eligible:
        return tables
    from pdfminer.high_level import extract_pages
    from pypdf import PdfReader

    page_count = len(PdfReader(str(source_pdf_path)).pages)
    pages = {p for _, table in eligible for p in (table.page_idx, table.page_idx + 1) if p < page_count}
    recovered = []
    previous: NativeTable | None = None
    for page, layout in zip(sorted(pages), extract_pages(str(source_pdf_path), page_numbers=pages)):
        if previous and previous.page + 1 != page:
            previous = None
        grids = _page_grids(layout, page, previous)
        recovered.extend(grids)
        previous = grids[-1] if grids else None
    replaced_pages = {table.page for table in recovered}
    originals = [(index, table) for index, table in eligible if table.page_idx in replaced_pages]
    if not originals:
        warnings.warn("Elan native pin-grid recovery found no matching source page; retaining MinerU tables.")
        return tables
    removed = {index for index, _ in originals}
    native_candidates = []
    for native in recovered:
        template = min(originals, key=lambda item: abs(item[1].page_idx - native.page))[1]
        html_rows = [["Pin name", "Pin number", "Pin type", "Description"]] + native.rows
        html = "<table>" + "".join("<tr>" + "".join("<td>" + escape(v).replace("\n", "<br>") + "</td>"
                                                              for v in row) + "</tr>" for row in html_rows) + "</table>"
        native_candidates.append(replace(template, html=html, page_idx=native.page,
                                         title=native.title or template.title,
                                         group_context=template.group_context +
                                         f"\nElan native PDF pin-grid recovery: page {native.page + 1}, columns {native.columns}"))
    insertion = min(removed)
    return [table for i, table in enumerate(tables[:insertion]) if i not in removed] + native_candidates + [
        table for i, table in enumerate(tables[insertion:], insertion) if i not in removed]
