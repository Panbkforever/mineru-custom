#!/usr/bin/env python3
"""
Filter likely page headers and footers before Markdown generation.

This file is intentionally standalone. It can be imported by MinerU code later,
or used from the command line to test the effect on a middle-json file.

Core idea:
1. Explicit header/footer/page-number/discarded blocks are moved to discarded_blocks.
2. Repeated short text in the top or bottom page band is treated as running
   header/footer and moved to discarded_blocks.
3. Page-number-like text near the page bottom is moved to discarded_blocks.
4. Large visual blocks, tables, images, equations, captions and footnotes are
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


@dataclass
class FilterConfig:
    top_ratio: float = 0.075
    bottom_ratio: float = 0.075
    min_repeat_pages: int = 2
    max_repeated_text_chars: int = 80
    max_margin_block_height_ratio: float = 0.045
    max_margin_block_width_ratio: float = 0.90
    protect_first_page_top: bool = True
    aggressive_margin_drop: bool = False
    mark_reason_field: str = "_header_footer_filter_reason"


def filter_headers_and_footers(
    pdf_info_list: list[dict[str, Any]],
    config: FilterConfig | None = None,
) -> dict[str, int]:
    """Move likely headers/footers/page numbers into each page's discarded_blocks.

    Args:
        pdf_info_list: MinerU middle-json page list. The function mutates it.
        config: Filtering thresholds.

    Returns:
        A small stats dict for logging or debugging.
    """

    cfg = config or FilterConfig()
    repeated_texts = _find_repeated_margin_texts(pdf_info_list, cfg)
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
    cfg: FilterConfig,
) -> tuple[bool, str]:
    block_type = str(block.get("type") or block.get("block_type") or "").lower()
    if block_type in EXPLICIT_DISCARD_TYPES:
        return True, f"explicit_type:{block_type}"

    if block_type in PROTECTED_TYPES:
        return False, ""

    if block_type not in TEXT_LIKE_TYPES:
        return False, ""

    page_width, page_height = _page_size(page_info)
    if page_width <= 0 or page_height <= 0:
        return False, ""

    bbox = _block_bbox(block)
    if not bbox:
        return False, ""

    text = _normalize_text(_block_text(block))
    if not text:
        return False, ""

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
        FilterConfig(
            top_ratio=args.top_ratio,
            bottom_ratio=args.bottom_ratio,
            min_repeat_pages=args.min_repeat_pages,
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
