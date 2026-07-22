"""从 MinerU 最终解析结果中提取文档级 NOTE/注释文本。

本模块只处理独立注释块，不参与引脚、封装、表格字段或分组判断。

处理流程：

1. 根据最终 ``<pdf>.json`` 或 ``<pdf>_middle.json`` 找到同目录的
   ``<pdf>_content_list.json``，使用其中带页码、类型和坐标的阅读顺序数据。
2. 严格识别去除首尾空白后完全等于 ``NOTE`` 或 ``注`` 的独立标记。
   ``NOTE:``、``NOTES``、``NOTE 1``、``注：``、``注1`` 均不命中。
3. 从标记后的第一个正文文本块开始收集，遇到下一条严格标记、章节标题、
   表格、图片、页面切换或明显离开注释正文缩进区域时停止。
4. 按文档阅读顺序生成 ``note1``、``note2``，不调用大模型，也不根据
   NOTE 正文内容做主观筛选。

项目规则：

- NOTE 属于整份 PDF，而不是某个封装或某个引脚。
- 只提取独立标记下面的正文；表格单元格或普通句子中的 NOTE 不提取。
- 同一注释的多个正文文本块用换行符连接，正文原有内容不做语义改写。
- 找不到结构化 ``content_list`` 时不从 Markdown 猜测边界，避免多提取。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


# 只允许项目明确规定的两个标记。这里有意区分大小写和全角标点。
NOTE_MARKERS = {"NOTE", "注"}

# 这些元素不是注释正文；遇到它们说明当前注释区域已经结束。
CONTENT_BOUNDARY_TYPES = {"table", "image", "equation", "interline_equation"}

# 页眉、页脚和页码可能排在当前页正文之后，不能被写入 NOTE 正文。
IGNORED_PAGE_TYPES = {"header", "footer", "page_number"}


def extract_notes_from_source_files(source_files: Iterable[Path]) -> dict[str, str]:
    """从本次提取使用的解析 JSON 中收集全部文档注释。

    ``source_files`` 与引脚提取使用的是同一批最终解析文件，因此批处理、
    单文件命令和接口不会走不同的 NOTE 数据源。
    """

    note_texts: list[str] = []
    for source_file in source_files:
        content_list_path = _find_content_list_path(Path(source_file))
        if content_list_path is None:
            continue

        content_items = _read_content_list(content_list_path)
        note_texts.extend(extract_notes_from_content_items(content_items))

    # 编号只由最终文档阅读顺序决定，不根据正文内容排序或合并。
    return {f"note{index}": text for index, text in enumerate(note_texts, start=1)}


def extract_notes_from_content_items(content_items: list[dict[str, Any]]) -> list[str]:
    """从 MinerU content_list 的顺序元素中提取每一个独立注释块。"""

    notes: list[str] = []
    item_index = 0

    while item_index < len(content_items):
        marker_body = _match_strict_note_marker(content_items[item_index])
        if marker_body is None:
            item_index += 1
            continue

        marker_item = content_items[item_index]
        marker_page = _page_index(marker_item)
        body_parts = [marker_body] if marker_body else []
        first_body_left: float | None = None
        next_index = item_index + 1

        while next_index < len(content_items):
            candidate = content_items[next_index]

            # 注释不跨页推断。跨页时边界不够确定，宁可停止也不混入下一页正文。
            candidate_page = _page_index(candidate)
            if (
                marker_page is not None
                and candidate_page is not None
                and candidate_page != marker_page
            ):
                break

            candidate_type = str(candidate.get("type", "")).strip().lower()
            if candidate_type in IGNORED_PAGE_TYPES:
                next_index += 1
                continue

            # 下一条 NOTE/注 由外层循环独立处理，不能并入当前正文。
            if _match_strict_note_marker(candidate) is not None:
                break

            if candidate_type in CONTENT_BOUNDARY_TYPES:
                break

            if candidate_type != "text":
                break

            # MinerU 的 text_level 表示章节标题。正文收集到标题前结束。
            if candidate.get("text_level") is not None:
                break

            candidate_text = _normalize_body_text(candidate.get("text", ""))
            if not candidate_text:
                next_index += 1
                continue

            candidate_left = _bbox_left(candidate)
            if first_body_left is None:
                first_body_left = candidate_left
            elif _leaves_note_indent(first_body_left, candidate_left):
                # 普通章节正文通常恢复到更靠左的正文边界；这属于注释结束信号。
                break

            body_parts.append(candidate_text)
            next_index += 1

        note_text = "\n".join(part for part in body_parts if part).strip()
        if note_text:
            notes.append(note_text)

        # 继续从未消费的边界元素判断，保证紧邻的第二个 NOTE 不会被跳过。
        item_index = max(next_index, item_index + 1)

    return notes


def _find_content_list_path(source_file: Path) -> Path | None:
    """将最终 JSON 或 middle JSON 映射到同目录的 content_list JSON。"""

    source_name = source_file.name
    if source_name.endswith("_middle.json"):
        document_stem = source_name[: -len("_middle.json")]
    else:
        document_stem = source_file.stem

    # 优先使用标准 content_list；v2 仅作为兼容旧输出的后备数据源。
    candidates = [
        source_file.with_name(f"{document_stem}_content_list.json"),
        source_file.with_name(f"{document_stem}_content_list_v2.json"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def _read_content_list(path: Path) -> list[dict[str, Any]]:
    """读取 content_list；格式异常时返回空列表，不影响原有引脚提取。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _match_strict_note_marker(item: dict[str, Any]) -> str | None:
    """严格匹配 NOTE/注；返回同一文本块中标记后的正文。"""

    if str(item.get("type", "")).strip().lower() != "text":
        return None

    raw_text = str(item.get("text", "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = raw_text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or lines[0].strip() not in NOTE_MARKERS:
        return None

    # 标记和正文偶尔会被 MinerU 合成一个文本块；只有第一行严格命中才接受。
    return _normalize_body_text("\n".join(lines[1:]))


def _normalize_body_text(value: Any) -> str:
    """清理排版空白，同时保留文本块内部真实的段落换行。"""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    current_lines: list[str] = []

    for line in text.split("\n"):
        stripped = re.sub(r"[ \t]+", " ", line).strip()
        if stripped:
            current_lines.append(stripped)
        elif current_lines:
            paragraphs.append(" ".join(current_lines))
            current_lines = []
    if current_lines:
        paragraphs.append(" ".join(current_lines))

    return "\n".join(paragraphs).strip()


def _page_index(item: dict[str, Any]) -> int | None:
    """读取页序号；缺失时返回 None，不制造伪页码。"""

    value = item.get("page_idx")
    return value if isinstance(value, int) else None


def _bbox_left(item: dict[str, Any]) -> float | None:
    """读取文本块左边界，用于识别注释结束后恢复的普通正文缩进。"""

    bbox = item.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    left = bbox[0]
    return float(left) if isinstance(left, (int, float)) else None


def _leaves_note_indent(first_left: float | None, candidate_left: float | None) -> bool:
    """判断后续文本是否明显回到注释框之外的左侧正文区域。"""

    if first_left is None or candidate_left is None:
        return False
    return candidate_left < first_left - 60
