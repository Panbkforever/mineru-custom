#!/usr/bin/env python3
"""
Filter likely page headers and footers before Markdown generation.

This file is intentionally standalone. It can be imported by MinerU code later,
or used from the command line to test the effect on a middle-json file.

Core idea:
1. Explicit header/footer/page-number/discarded blocks are moved to discarded_blocks.
2. If a PDF path is provided, thick horizontal rules are detected on each page.
   Text above the upper rule and below the lower rule is treated as page
   header/footer material.
3. Repeated short text in the top or bottom page band is treated as running
   header/footer and moved to discarded_blocks.
4. Page-number-like text near the page bottom is moved to discarded_blocks.
5. First-page bottom legal notices, such as TI datasheet trademark/copyright
   notices, are treated as cover-page footer material and removed.
6. Large visual blocks, tables, images, equations, captions and footnotes are
   left untouched by default.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


EXPLICIT_DISCARD_TYPES = {
    "abandon",
    "discarded",
    "header",
    "header_image",
    "footer",
    "footer_image",
    "page_header",
    "page_footer",
    "page_number",
}

PROTECTED_TYPES = {
    "image",
    "image_body",
    "image_caption",
    "image_footnote",
    "table",
    "table_body",
    "table_caption",
    "table_footnote",
    "chart",
    "chart_body",
    "chart_caption",
    "chart_footnote",
    "interline_equation",
    "equation",
    "formula",
    "inline_equation",
    "code",
    "code_body",
    "code_caption",
    "footnote",
    "page_footnote",
    "caption",
}

TEXT_LIKE_TYPES = {
    "",
    "text",
    "title",
    "paragraph",
    "doc_title",
    "paragraph_title",
    "list",
    "index",
    "ref_text",
    "phonetic",
    "vertical_text",
}

PAGE_NUMBER_PATTERNS = [
    re.compile(r"^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$"),
    re.compile(r"^\s*\d{1,4}\s*/\s*\d{1,4}\s*$"),
    re.compile(r"^\s*page\s+\d{1,4}(\s+of\s+\d{1,4})?\s*$", re.I),
    re.compile(r"^\s*第\s*\d{1,4}\s*页\s*(共\s*\d{1,4}\s*页)?\s*$"),
]

FIRST_PAGE_NOTICE_KEYWORDS = {
    "please be aware",
    "important notice",
    "standard warranty",
    "critical applications",
    "texas instruments",
    "semiconductor products",
    "disclaimers",
    "all trademarks",
    "production data",
    "publication date",
    "copyright",
}


@dataclass
class FilterConfig:
    enable_line_boundary_filter: bool = True
    line_render_scale: float = 2.0
    line_dark_threshold: int = 80
    line_min_width_ratio: float = 0.55
    line_min_row_dark_ratio: float = 0.35
    line_min_thickness_px: int = 2
    line_max_thickness_px: int = 18
    line_top_search_ratio: float = 0.30
    line_bottom_search_start_ratio: float = 0.55
    line_bottom_search_end_ratio: float = 0.96
    line_padding_ratio: float = 0.003
    top_ratio: float = 0.075
    bottom_ratio: float = 0.075
    min_repeat_pages: int = 2
    max_repeated_text_chars: int = 80
    max_margin_block_height_ratio: float = 0.045
    max_margin_block_width_ratio: float = 0.90
    protect_first_page_top: bool = True
    first_page_notice_bottom_ratio: float = 0.26
    first_page_notice_min_keyword_hits: int = 2
    aggressive_margin_drop: bool = False
    mark_reason_field: str = "_header_footer_filter_reason"


@dataclass
class LineBoundary:
    top_y: float | None = None
    bottom_y: float | None = None


def filter_headers_and_footers(
    pdf_info_list: list[dict[str, Any]],
    pdf_path: str | Path | None = None,
    config: FilterConfig | None = None,
) -> dict[str, int]:
    """Move likely headers/footers/page numbers into each page's discarded_blocks.

    Args:
        pdf_info_list: MinerU middle-json page list. The function mutates it.
        pdf_path: Optional source PDF path. When available, the filter first
            detects thick horizontal rules and uses them as content boundaries.
        config: Filtering thresholds.

    Returns:
        A small stats dict for logging or debugging.
    """

    cfg = config or FilterConfig()
    repeated_texts = _find_repeated_margin_texts(pdf_info_list, cfg)
    first_page_notice_pages = _find_first_page_bottom_notice_pages(pdf_info_list, cfg)
    line_boundaries = detect_line_boundaries(pdf_info_list, pdf_path, cfg)
    stats = Counter()

    for page_index, page_info in enumerate(pdf_info_list):
        discarded_blocks = page_info.setdefault("discarded_blocks", [])

        for block_key in ("preproc_blocks", "para_blocks"):
            blocks = page_info.get(block_key)
            if not isinstance(blocks, list):
                continue

            kept_blocks = []
            for block in blocks:
                should_discard, reason = _should_discard_block(
                    block=block,
                    page_info=page_info,
                    page_index=page_index,
                    repeated_texts=repeated_texts,
                    first_page_notice_pages=first_page_notice_pages,
                    line_boundary=line_boundaries.get(page_index),
                    cfg=cfg,
                )
                if should_discard:
                    block[cfg.mark_reason_field] = reason
                    discarded_blocks.append(block)
                    stats[f"{block_key}:{reason}"] += 1
                else:
                    kept_blocks.append(block)

            page_info[block_key] = kept_blocks

    stats["total_removed"] = sum(value for key, value in stats.items() if key != "total_removed")
    return dict(stats)


def detect_line_boundaries(
    pdf_info_list: list[dict[str, Any]],
    pdf_path: str | Path | None,
    cfg: FilterConfig | None = None,
) -> dict[int, LineBoundary]:
    """Detect thick horizontal rules that bound the real content area.

    Datasheets often use a bold rule below the page header and another bold
    rule above the page footer. The text outside those two rules should be
    excluded from Markdown even when the layout model labels it as normal text.

    This function is deliberately optional. If the PDF cannot be rendered or no
    reliable rules are found, it returns an empty mapping and the caller falls
    back to text/repetition based filtering.
    """
    cfg = cfg or FilterConfig()
    if not cfg.enable_line_boundary_filter or not pdf_path:
        return {}

    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        return {}

    try:
        import numpy as np
        import pypdfium2 as pdfium
    except Exception:
        return {}

    boundaries: dict[int, LineBoundary] = {}
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return {}

    page_count = min(len(pdf), len(pdf_info_list))
    for page_index in range(page_count):
        page_width, page_height = _page_size(pdf_info_list[page_index])
        if page_width <= 0 or page_height <= 0:
            continue

        try:
            page = pdf[page_index]
            bitmap = page.render(scale=cfg.line_render_scale)
            pil_image = bitmap.to_pil().convert("L")
            gray = np.asarray(pil_image)
        except Exception:
            continue

        image_height, image_width = gray.shape[:2]
        top_rule_px = _detect_horizontal_rule_y(
            gray=gray,
            search_start_px=0,
            search_end_px=int(image_height * cfg.line_top_search_ratio),
            cfg=cfg,
        )
        bottom_rule_px = _detect_horizontal_rule_y(
            gray=gray,
            search_start_px=int(image_height * cfg.line_bottom_search_start_ratio),
            search_end_px=int(image_height * cfg.line_bottom_search_end_ratio),
            cfg=cfg,
        )

        top_rule_y = _pixel_y_to_page_y(top_rule_px, image_height, page_height)
        bottom_rule_y = _pixel_y_to_page_y(bottom_rule_px, image_height, page_height)
        if top_rule_y is None and bottom_rule_y is None:
            continue
        if top_rule_y is not None and bottom_rule_y is not None and top_rule_y >= bottom_rule_y:
            continue

        boundaries[page_index] = LineBoundary(top_y=top_rule_y, bottom_y=bottom_rule_y)

    return boundaries


def _detect_horizontal_rule_y(
    gray: Any,
    search_start_px: int,
    search_end_px: int,
    cfg: FilterConfig,
) -> int | None:
    image_height, image_width = gray.shape[:2]
    search_start_px = max(0, min(search_start_px, image_height))
    search_end_px = max(search_start_px, min(search_end_px, image_height))
    if search_end_px <= search_start_px:
        return None

    candidate_rows = []
    min_run_width = int(image_width * cfg.line_min_width_ratio)
    for row_y in range(search_start_px, search_end_px):
        dark_row = gray[row_y] <= cfg.line_dark_threshold
        dark_ratio = float(dark_row.mean())
        if dark_ratio < cfg.line_min_row_dark_ratio:
            continue
        if _longest_true_run(dark_row) >= min_run_width:
            candidate_rows.append(row_y)

    if not candidate_rows:
        return None

    bands = _merge_consecutive_rows(candidate_rows)
    valid_bands = [
        band
        for band in bands
        if cfg.line_min_thickness_px <= (band[1] - band[0] + 1) <= cfg.line_max_thickness_px
    ]
    if not valid_bands:
        return None

    # Prefer the strongest-looking rule: the longest/thickest band. This avoids
    # choosing short glyph strokes that happen to be dark and horizontal.
    best_band = max(valid_bands, key=lambda band: (band[1] - band[0] + 1, band[1]))
    return int((best_band[0] + best_band[1]) / 2)


def _longest_true_run(values: Any) -> int:
    longest = 0
    current = 0
    for value in values:
        if bool(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _merge_consecutive_rows(rows: list[int]) -> list[tuple[int, int]]:
    if not rows:
        return []

    bands = []
    start = previous = rows[0]
    for row in rows[1:]:
        if row == previous + 1:
            previous = row
            continue
        bands.append((start, previous))
        start = previous = row
    bands.append((start, previous))
    return bands


def _pixel_y_to_page_y(
    pixel_y: int | None,
    image_height: int,
    page_height: float,
) -> float | None:
    if pixel_y is None or image_height <= 0:
        return None
    return float(pixel_y) / float(image_height) * page_height


def _find_repeated_margin_texts(
    pdf_info_list: list[dict[str, Any]],
    cfg: FilterConfig,
) -> set[str]:
    text_pages: dict[str, set[int]] = defaultdict(set)

    for page_index, page_info in enumerate(pdf_info_list):
        page_width, page_height = _page_size(page_info)
        if page_width <= 0 or page_height <= 0:
            continue

        for block in _iter_candidate_blocks(page_info):
            if _is_protected_type(block):
                continue

            text = _normalize_text(_block_text(block))
            if not text or len(text) > cfg.max_repeated_text_chars:
                continue

            bbox = _block_bbox(block)
            if not bbox:
                continue

            band = _margin_band(bbox, page_width, page_height, cfg)
            if band:
                text_pages[text].add(page_index)

    return {
        text
        for text, pages in text_pages.items()
        if len(pages) >= cfg.min_repeat_pages
    }


def _find_first_page_bottom_notice_pages(
    pdf_info_list: list[dict[str, Any]],
    cfg: FilterConfig,
) -> set[int]:
    """Detect cover-page-only legal notice regions.

    Some datasheets put a unique legal/trademark/copyright block at the bottom
    of page 1. It is not repeated, so the normal running-footer rule cannot
    catch it. We only enable the special removal when multiple legal-notice
    keywords are found in the first page bottom band.
    """
    if not pdf_info_list:
        return set()

    page_info = pdf_info_list[0]
    page_width, page_height = _page_size(page_info)
    if page_width <= 0 or page_height <= 0:
        return set()

    bottom_text_parts = []
    for block in _iter_candidate_blocks(page_info):
        if _is_protected_type(block):
            continue

        bbox = _block_bbox(block)
        if not bbox or not _in_first_page_notice_band(bbox, page_width, page_height, cfg):
            continue

        text = _normalize_text(_block_text(block))
        if text:
            bottom_text_parts.append(text)

    bottom_text = " ".join(bottom_text_parts)
    keyword_hits = {
        keyword
        for keyword in FIRST_PAGE_NOTICE_KEYWORDS
        if keyword in bottom_text
    }
    if len(keyword_hits) >= cfg.first_page_notice_min_keyword_hits:
        return {0}
    return set()


def _iter_candidate_blocks(page_info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for block_key in ("preproc_blocks", "para_blocks"):
        blocks = page_info.get(block_key)
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict):
                    yield block


def _should_discard_block(
    block: dict[str, Any],
    page_info: dict[str, Any],
    page_index: int,
    repeated_texts: set[str],
    first_page_notice_pages: set[int],
    line_boundary: LineBoundary | None,
    cfg: FilterConfig,
) -> tuple[bool, str]:
    block_type = str(block.get("type") or block.get("block_type") or "").lower()
    if block_type in EXPLICIT_DISCARD_TYPES:
        return True, f"explicit_type:{block_type}"

    page_width, page_height = _page_size(page_info)
    if page_width <= 0 or page_height <= 0:
        return False, ""

    bbox = _block_bbox(block)
    if not bbox:
        return False, ""

    # A detected horizontal-rule boundary is a stronger signal than block type.
    # It removes the whole visual/text region outside the content frame, including
    # small header icons or footer marks that should not appear in Markdown.
    line_reason = _line_boundary_discard_reason(bbox, page_width, page_height, line_boundary, cfg)
    if line_reason:
        return True, line_reason

    if block_type in PROTECTED_TYPES:
        return False, ""

    if block_type not in TEXT_LIKE_TYPES:
        return False, ""

    text = _normalize_text(_block_text(block))
    if not text:
        return False, ""

    if (
        page_index in first_page_notice_pages
        and _in_first_page_notice_band(bbox, page_width, page_height, cfg)
    ):
        return True, "first_page_bottom_notice"

    band = _margin_band(bbox, page_width, page_height, cfg)
    if not band:
        return False, ""

    if cfg.protect_first_page_top and page_index == 0 and band == "top":
        if text not in repeated_texts:
            return False, ""

    if _looks_like_page_number(text) and band == "bottom":
        return True, "page_number_pattern"

    if text in repeated_texts and _is_small_margin_block(bbox, page_width, page_height, cfg):
        return True, f"repeated_{band}_text"

    if cfg.aggressive_margin_drop and _is_small_margin_block(bbox, page_width, page_height, cfg):
        return True, f"aggressive_{band}_margin"

    return False, ""


def _line_boundary_discard_reason(
    bbox: list[float],
    page_width: float,
    page_height: float,
    line_boundary: LineBoundary | None,
    cfg: FilterConfig,
) -> str:
    if line_boundary is None:
        return ""

    _x0, y0, _x1, y1 = _bbox_to_page_units(bbox, page_width, page_height)
    center_y = (y0 + y1) / 2.0
    padding = page_height * cfg.line_padding_ratio

    if line_boundary.top_y is not None and center_y < line_boundary.top_y - padding:
        return "above_top_horizontal_rule"
    if line_boundary.bottom_y is not None and center_y > line_boundary.bottom_y + padding:
        return "below_bottom_horizontal_rule"
    return ""


def _page_size(page_info: dict[str, Any]) -> tuple[float, float]:
    for key in ("page_size", "size"):
        value = page_info.get(key)
        if isinstance(value, dict):
            width = value.get("width") or value.get("w") or value.get("page_width")
            height = value.get("height") or value.get("h") or value.get("page_height")
            if width and height:
                return float(width), float(height)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])

    width = (
        page_info.get("width")
        or page_info.get("page_width")
        or page_info.get("w")
    )
    height = (
        page_info.get("height")
        or page_info.get("page_height")
        or page_info.get("h")
    )
    if width and height:
        return float(width), float(height)

    return 0.0, 0.0


def _block_bbox(block: dict[str, Any]) -> list[float] | None:
    bbox = block.get("bbox") or block.get("poly")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        return [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
    except (TypeError, ValueError):
        return None


def _margin_band(
    bbox: list[float],
    page_width: float,
    page_height: float,
    cfg: FilterConfig,
) -> str:
    x0, y0, x1, y1 = _bbox_to_page_units(bbox, page_width, page_height)
    block_height = max(0.0, y1 - y0)

    if block_height > page_height * cfg.max_margin_block_height_ratio:
        return ""
    if y1 <= page_height * cfg.top_ratio:
        return "top"
    if y0 >= page_height * (1.0 - cfg.bottom_ratio):
        return "bottom"
    return ""


def _is_small_margin_block(
    bbox: list[float],
    page_width: float,
    page_height: float,
    cfg: FilterConfig,
) -> bool:
    x0, y0, x1, y1 = _bbox_to_page_units(bbox, page_width, page_height)
    block_width = max(0.0, x1 - x0)
    block_height = max(0.0, y1 - y0)
    return (
        block_height <= page_height * cfg.max_margin_block_height_ratio
        and block_width <= page_width * cfg.max_margin_block_width_ratio
    )


def _in_first_page_notice_band(
    bbox: list[float],
    page_width: float,
    page_height: float,
    cfg: FilterConfig,
) -> bool:
    _x0, y0, _x1, _y1 = _bbox_to_page_units(bbox, page_width, page_height)
    return y0 >= page_height * (1.0 - cfg.first_page_notice_bottom_ratio)


def _bbox_to_page_units(
    bbox: list[float],
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) <= 1.5:
        return x0 * page_width, y0 * page_height, x1 * page_width, y1 * page_height
    return x0, y0, x1, y1


def _block_text(block: dict[str, Any]) -> str:
    direct_parts = []
    for key in ("text", "content", "md", "html"):
        value = block.get(key)
        if isinstance(value, str):
            direct_parts.append(value)

    line_parts = []
    for line in block.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans") or []:
            if not isinstance(span, dict):
                continue
            for key in ("text", "content"):
                value = span.get(key)
                if isinstance(value, str):
                    line_parts.append(value)

    return " ".join(direct_parts + line_parts)


def _normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().lower()
    text = text.strip("|-_=*#~`.,;:()[]{}<>")
    return text


def _looks_like_page_number(text: str) -> bool:
    return any(pattern.match(text) for pattern in PAGE_NUMBER_PATTERNS)


def _is_protected_type(block: dict[str, Any]) -> bool:
    block_type = str(block.get("type") or block.get("block_type") or "").lower()
    return block_type in PROTECTED_TYPES


def load_pdf_info_list(data: Any) -> list[dict[str, Any]]:
    """Accept common MinerU middle-json shapes and return the page list."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("Unsupported json root: expected a list or dict.")

    for key in ("pdf_info", "pdf_info_list", "pages", "page_info_list"):
        value = data.get(key)
        if isinstance(value, list):
            return value

    raise ValueError("Cannot find page list in json. Expected pdf_info/pdf_info_list/pages.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move likely PDF headers/footers/page numbers to discarded_blocks."
    )
    parser.add_argument("input_json", type=Path, help="Input MinerU middle-json file.")
    parser.add_argument(
        "-o",
        "--output-json",
        type=Path,
        help="Output path. Defaults to <input>.filtered.json.",
    )
    parser.add_argument("--top-ratio", type=float, default=FilterConfig.top_ratio)
    parser.add_argument("--bottom-ratio", type=float, default=FilterConfig.bottom_ratio)
    parser.add_argument("--min-repeat-pages", type=int, default=FilterConfig.min_repeat_pages)
    parser.add_argument(
        "--pdf",
        type=Path,
        help="Optional source PDF path. Enables thick horizontal-rule boundary filtering.",
    )
    parser.add_argument(
        "--no-line-boundary-filter",
        action="store_true",
        help="Disable PDF-rendered horizontal-rule boundary detection.",
    )
    parser.add_argument(
        "--aggressive-margin-drop",
        action="store_true",
        help="Also drop small text blocks in page margins even if they are not repeated.",
    )
    args = parser.parse_args()

    input_json = args.input_json
    output_json = args.output_json or input_json.with_suffix(".filtered.json")

    data = json.loads(input_json.read_text(encoding="utf-8"))
    filtered_data = copy.deepcopy(data)
    pdf_info_list = load_pdf_info_list(filtered_data)

    stats = filter_headers_and_footers(
        pdf_info_list,
        pdf_path=args.pdf,
        config=FilterConfig(
            top_ratio=args.top_ratio,
            bottom_ratio=args.bottom_ratio,
            min_repeat_pages=args.min_repeat_pages,
            enable_line_boundary_filter=not args.no_line_boundary_filter,
            aggressive_margin_drop=args.aggressive_margin_drop,
        ),
    )

    output_json.write_text(
        json.dumps(filtered_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output_json), "stats": stats}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
