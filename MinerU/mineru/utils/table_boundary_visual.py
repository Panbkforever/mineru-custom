"""用 PDF 页面网格和原生字符验证跨页表格边界行。"""

import re
from collections import OrderedDict

from loguru import logger

from mineru.utils.pdfium_guard import pdfium_guard


def _normalized(value):
    return "".join(char.lower() for char in value if char.isalnum())


def _groups(indices):
    groups = []
    for value in indices:
        value = int(value)
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


class TableBoundaryVisualVerifier:
    """仅在网格与原生文字均可可靠对应时确认两条物理行独立。"""

    def __init__(self, pdf_doc):
        self.pdf_doc = pdf_doc
        self._cache = OrderedDict()

    def _grid(self, page_info, span, expected_columns):
        import cv2
        import numpy as np

        page_idx = int(page_info["page_idx"])
        bbox = span.get("bbox")
        page_size = page_info.get("page_size")
        if not bbox or not page_size or len(bbox) != 4 or len(page_size) != 2:
            return None
        key = (page_idx, tuple(bbox), tuple(page_size), expected_columns)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        with pdfium_guard():
            page = self.pdf_doc[page_idx]
            width, height = page.get_size()
            scale = 2.0
            image = np.asarray(page.render(scale=scale).to_pil().convert("L"))
            text_page = page.get_textpage()
            try:
                text = text_page.get_text_range(0, text_page.count_chars())
                characters = []
                for index, char in enumerate(text):
                    if not char.strip():
                        continue
                    try:
                        left, bottom, right, top = text_page.get_charbox(index)
                    except Exception:
                        continue
                    characters.append((char, (left + right) / 2, height - (bottom + top) / 2))
            finally:
                text_page.close()

        sx, sy = width / page_size[0], height / page_size[1]
        x0, y0, x1, y1 = bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy
        left = max(0, int((x0 - 3) * scale))
        top = max(0, int((y0 - 3) * scale))
        right = min(image.shape[1], int((x1 + 3) * scale))
        bottom = min(image.shape[0], int((y1 + 3) * scale))
        crop = image[top:bottom, left:right]
        result = None
        if crop.size and crop.shape[0] > 20 and crop.shape[1] > 20:
            ink = (crop < 180).astype(np.uint8)
            horizontal = cv2.morphologyEx(
                ink, cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(crop.shape[1] * .08)), 1)),
            )
            vertical = cv2.morphologyEx(
                ink, cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(crop.shape[0] * .08)))),
            )
            ys = [(top + pos) / scale for pos in _groups(np.flatnonzero(horizontal.sum(axis=1) >= crop.shape[1] * .70))]
            xs = [(left + pos) / scale for pos in _groups(np.flatnonzero(vertical.sum(axis=0) >= crop.shape[0] * .40))]
            if (len(xs) == expected_columns + 1 and len(ys) >= 3
                    and abs(xs[0] - x0) < 6 and abs(xs[-1] - x1) < 6
                    and abs(ys[0] - y0) < 6 and abs(ys[-1] - y1) < 6):
                result = (xs, ys, characters)
        self._cache[key] = result
        while len(self._cache) > 4:
            self._cache.popitem(last=False)
        return result

    def _find_row(self, page_info, state, row, name_col, expected_columns):
        cells = row.find_all(["td", "th"])
        if len(cells) != expected_columns or name_col >= len(cells):
            return None
        if any(int(cell.get("rowspan", 1)) != 1 or int(cell.get("colspan", 1)) != 1 for cell in cells):
            return None
        name = _normalized(cells[name_col].get_text())
        identifiers = [(index, _normalized(cell.get_text())) for index, cell in enumerate(cells[:name_col])]
        identifiers = [(index, value) for index, value in identifiers if re.fullmatch(r"[a-z]*\d+[a-z0-9]*", value)]
        if len(name) < 2 or not identifiers:
            return None
        grid = self._grid(page_info, state.body_span, expected_columns)
        if grid is None:
            return None
        xs, ys, characters = grid
        matches = []
        for row_idx, (y0, y1) in enumerate(zip(ys, ys[1:])):
            def text_at(col):
                selected = [(ch, x, y) for ch, x, y in characters if xs[col] < x < xs[col + 1] and y0 < y < y1]
                selected.sort(key=lambda item: (round(item[2] / 3), item[1]))
                return _normalized("".join(item[0] for item in selected))
            if text_at(name_col) != name:
                continue
            if not any(text_at(col) == value for col, value in identifiers):
                continue
            matches.append(row_idx)
        return (matches[0], len(ys) - 1) if len(matches) == 1 else None

    def independent_rows(self, previous_info, current_info, previous, current, header_count, flags):
        if not flags or any(flag != 1 for flag in flags) or previous.tail_occupied:
            return False
        if not previous.rows or not 0 < header_count < len(current.rows):
            return False
        from mineru.utils.table_merge import build_visual_col_mapping

        name_cols = set()
        for index in range(header_count):
            for cell, col in zip(current.rows[index].find_all(["td", "th"]), build_visual_col_mapping(current.rows, index)):
                label = _normalized(cell.get_text())
                if ("pin" in label or "ball" in label) and "name" in label and int(cell.get("colspan", 1)) == 1:
                    name_cols.add(col)
        if len(name_cols) != 1:
            return False
        name_col = next(iter(name_cols))
        previous_cells = previous.rows[-1].find_all(["td", "th"])
        current_cells = current.rows[header_count].find_all(["td", "th"])
        if len(previous_cells) != len(flags) or len(current_cells) != len(flags):
            return False
        # 同一列重复相同编号可能是同一条引脚的续接，不能仅靠边框判定。
        for left, right in zip(previous_cells[:name_col], current_cells[:name_col]):
            first, second = _normalized(left.get_text()), _normalized(right.get_text())
            if first == second and re.fullmatch(r"[a-z]*\d+[a-z0-9]*", first):
                return False
        try:
            before = self._find_row(previous_info, previous, previous.rows[-1], name_col, len(flags))
            after = self._find_row(current_info, current, current.rows[header_count], name_col, len(flags))
        except Exception as exc:
            logger.warning("跨页表格视觉证据不可用，保留原有处理: {}", exc)
            return False
        return bool(before and after and before[0] == before[1] - 1 and 0 < after[0] <= 4)
