"""导出最终 Markdown 中的全部表格及其 PDF 页码。

表格 HTML 以 ``<pdf>.json`` 中的最终 Markdown 为准，因此包含现有表格
后处理结果；页码以 ``<pdf>_middle.json`` 中的页面元数据为准。

正常情况下，两份文件中的表格数量和顺序完全一致，可直接逐表对应。若表格
后处理把一张原始表拆成多张逻辑表，本模块会比较表格文本，将拆出的表都映射
回同一原始页，避免后续所有表格的页码发生整体偏移。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Iterable

from extract.pin_package_extractor import (
    TableCandidate,
    iter_table_candidates,
    iter_table_candidates_from_markdown,
)


@dataclass(frozen=True)
class TablePageMatch:
    """最终表格与原始页面表格的对应结果。"""

    final_table: TableCandidate
    source_tables: tuple[TableCandidate, ...]

    @property
    def page_no(self) -> int:
        """返回面向用户的 1 基页码。"""
        for table in self.source_tables:
            if isinstance(table.page_idx, int):
                return table.page_idx + 1
        raise ValueError("Table page metadata is missing from middle_json")


def export_tables_from_parse_artifacts(
    final_json_path: str | Path,
    middle_json_path: str | Path,
    source_file: str,
) -> dict:
    """读取一次 MinerU 解析产物，生成表格页码 JSON 对象。"""

    final_json_path = Path(final_json_path)
    middle_json_path = Path(middle_json_path)

    final_payload = json.loads(final_json_path.read_text(encoding="utf-8"))
    markdown = final_payload.get("markdown", "")
    if not isinstance(markdown, str):
        raise ValueError(f"Invalid final markdown JSON: {final_json_path}")

    middle_json = json.loads(middle_json_path.read_text(encoding="utf-8"))
    source_tables = iter_table_candidates(middle_json)
    final_tables = iter_table_candidates_from_markdown(markdown)

    matches = match_final_tables_to_pages(final_tables, source_tables)
    table_list = []
    for table_id, match in enumerate(matches, start=1):
        table_list.append(
            {
                "table_id": table_id,
                "page_no": match.page_no,
                "title": match.final_table.title,
                "html": match.final_table.html,
            }
        )

    return {
        "pdf_name": source_file,
        "table_count": len(table_list),
        "table_list": table_list,
    }


def match_final_tables_to_pages(
    final_tables: Iterable[TableCandidate],
    source_tables: Iterable[TableCandidate],
) -> list[TablePageMatch]:
    """按文档顺序把最终表格映射到带页码的 middle_json 表格。

    常规的一对一情况直接按顺序匹配。数量不同时，仅在当前相邻位置尝试：

    * 一张原表被后处理拆成 2 至 4 张最终表；
    * 相邻 2 至 4 张原表被合成一张最终表。

    匹配始终保持文档顺序，不会跨越远处寻找相似表格，从而避免重复表头造成
    错页。无法取得任何 middle_json 表格时直接报错，不生成伪造页码。
    """

    final_list = list(final_tables)
    source_list = list(source_tables)
    if not final_list:
        return []
    if not source_list:
        raise ValueError("No page-tagged tables found in middle_json")

    # 数量一致是绝大多数解析结果，表格顺序由 MinerU 和 Markdown 后处理共同保留。
    if len(final_list) == len(source_list):
        return [
            TablePageMatch(final_table=final, source_tables=(source,))
            for final, source in zip(final_list, source_list)
        ]

    matches: list[TablePageMatch] = []
    final_index = 0
    source_index = 0

    while final_index < len(final_list) and source_index < len(source_list):
        final_table = final_list[final_index]
        source_table = source_list[source_index]
        direct_score = _table_similarity(final_table.html, source_table.html)

        # 内容已经足够接近时优先一对一，避免不必要的组合判断。
        if direct_score >= 0.72:
            matches.append(TablePageMatch(final_table, (source_table,)))
            final_index += 1
            source_index += 1
            continue

        split_count, split_score = _best_split_match(
            source_table,
            final_list,
            final_index,
        )
        merge_count, merge_score = _best_merge_match(
            final_table,
            source_list,
            source_index,
        )

        # 组合结果必须明显优于当前一对一结果，防止普通相似表被误合并。
        if split_count > 1 and split_score >= max(0.68, direct_score + 0.12, merge_score):
            for offset in range(split_count):
                matches.append(
                    TablePageMatch(final_list[final_index + offset], (source_table,))
                )
            final_index += split_count
            source_index += 1
            continue

        if merge_count > 1 and merge_score >= max(0.68, direct_score + 0.12):
            merged_sources = tuple(source_list[source_index : source_index + merge_count])
            matches.append(TablePageMatch(final_table, merged_sources))
            final_index += 1
            source_index += merge_count
            continue

        # 保守回退仍按当前位置一对一推进，保证重复表格不会跳到远处页面。
        matches.append(TablePageMatch(final_table, (source_table,)))
        final_index += 1
        source_index += 1

    # 后处理额外产生的尾部表格继承最后一张原表所在页。
    if final_index < len(final_list):
        last_source = source_list[-1]
        for table in final_list[final_index:]:
            matches.append(TablePageMatch(table, (last_source,)))

    return matches


def find_parse_artifacts(parse_output_dir: str | Path, document_stem: str) -> tuple[Path, Path]:
    """在 MinerU 输出目录中定位最终 JSON 和对应的 middle_json。"""

    parse_output_dir = Path(parse_output_dir)
    final_candidates = [
        path
        for path in parse_output_dir.rglob(f"{document_stem}.json")
        if ".ipynb_checkpoints" not in path.parts
    ]
    if not final_candidates:
        raise FileNotFoundError(
            f"Final markdown JSON not found for {document_stem}: {parse_output_dir}"
        )

    # parse_doc.py 的最终 JSON 与 middle_json 位于同一模式目录。
    final_json_path = sorted(final_candidates)[0]
    middle_json_path = final_json_path.with_name(f"{document_stem}_middle.json")
    if not middle_json_path.exists():
        raise FileNotFoundError(f"Middle JSON not found: {middle_json_path}")
    return final_json_path, middle_json_path


def _best_split_match(
    source_table: TableCandidate,
    final_tables: list[TableCandidate],
    start: int,
) -> tuple[int, float]:
    """判断当前原表是否被拆成连续多张最终表。"""

    best_count = 1
    best_score = 0.0
    for count in range(2, min(4, len(final_tables) - start) + 1):
        combined = " ".join(table.html for table in final_tables[start : start + count])
        score = _table_similarity(source_table.html, combined)
        if score > best_score:
            best_count = count
            best_score = score
    return best_count, best_score


def _best_merge_match(
    final_table: TableCandidate,
    source_tables: list[TableCandidate],
    start: int,
) -> tuple[int, float]:
    """判断连续多张原表是否被合成一张最终表。"""

    best_count = 1
    best_score = 0.0
    for count in range(2, min(4, len(source_tables) - start) + 1):
        combined = " ".join(table.html for table in source_tables[start : start + count])
        score = _table_similarity(final_table.html, combined)
        if score > best_score:
            best_count = count
            best_score = score
    return best_count, best_score


def _table_similarity(left_html: str, right_html: str) -> float:
    """比较两张表的可见文本，忽略标签、空白和后处理属性变化。"""

    left = _normalized_table_text(left_html)
    right = _normalized_table_text(right_html)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _normalized_table_text(table_html: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", table_html)
    visible = unescape(without_tags).lower()
    return re.sub(r"[^\w]+", "", visible, flags=re.UNICODE)
