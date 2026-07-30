"""解析候选表的多层表头，并判断多个名称列之间的结构关系。

本模块只处理已经通过宽松初筛的候选表，不判断表格是否需要提取，不调用
大模型，不判断真实 pkg，也不生成任何引脚记录。

固定处理流程：

1. 展开 HTML 单元格的 ``rowspan`` 和 ``colspan``，形成列对齐的二维表格。
2. 根据主提取器已经确定的最后一行表头，为每个数据列建立完整表头路径。
3. 当多个名称列共享同一父表头时，严格判断下一行是否为分支子表头。
4. 将 PIN NAME、BALL NAME、SIGNAL NAME、TERMINAL NAME 统一视为
   ``pin_name`` 语义。
5. 多个等价名称字段仍属于普通单封装字段；只有名称列具有共同语义父节点，
   并且存在互不相同的子分支标签时，才判定为多封装名称分支。

特别重要的项目规则：

* BALL NAME 与 SIGNAL NAME 等价，不能因为两列同时出现就判定为多封装。
* 普通等价名称列最终只能选择一列，禁止使用 ``|`` 合并。
* ``NAME > Package A/Package B`` 和
  ``Package A/Package B > NAME`` 都属于封装分支结构。
* 本模块返回的 ``branch_label`` 只是表内分支证据，不是最终公开 pkg 名称。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, Sequence


@dataclass(frozen=True)
class HeaderColumnPath:
    """一个最终数据列对应的完整多层表头路径。"""

    column_index: int
    parts: tuple[str, ...]

    @property
    def combined(self) -> str:
        """把路径转换成模型和现有字段判断可以读取的完整表头。"""

        return " ".join(self.parts)


@dataclass(frozen=True)
class NameColumnBranch:
    """一个名称分支及其可用的等价名称列。"""

    label: str
    column_indexes: tuple[int, ...]


@dataclass(frozen=True)
class NameColumnLayout:
    """候选表中名称列的结构分类结果。"""

    mode: str
    name_column_indexes: tuple[int, ...] = ()
    branches: tuple[NameColumnBranch, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RawCell:
    """HTML 解析阶段尚未展开跨行跨列属性的单元格。"""

    text: str
    rowspan: int
    colspan: int


class _RawTableParser(HTMLParser):
    """读取第一个顶层 table，保留单元格内显式换行和 span 属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.rows: list[list[_RawCell]] = []
        self._row: list[_RawCell] = []
        self._cell_parts: list[str] = []
        self._rowspan = 1
        self._colspan = 1

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag == "table":
            self.table_depth += 1
            return
        if self.table_depth != 1:
            return
        if tag == "tr":
            self.in_row = True
            self._row = []
            return
        if tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self._cell_parts = []
            attr_map = {name.lower(): value for name, value in attrs}
            self._rowspan = _positive_span(attr_map.get("rowspan"))
            self._colspan = _positive_span(attr_map.get("colspan"))
            return
        if tag == "br" and self.in_cell:
            self._cell_parts.append("\n")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "br" and self.table_depth == 1 and self.in_cell:
            self._cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            return
        if self.table_depth != 1:
            return
        if tag in {"td", "th"} and self.in_cell:
            self._row.append(
                _RawCell(
                    text=_clean_cell_text("".join(self._cell_parts)),
                    rowspan=self._rowspan,
                    colspan=self._colspan,
                )
            )
            self.in_cell = False
            self._cell_parts = []
            return
        if tag == "tr" and self.in_row:
            if self._row:
                self.rows.append(self._row)
            self.in_row = False
            self._row = []

    def handle_data(self, data: str) -> None:
        if self.table_depth == 1 and self.in_cell:
            self._cell_parts.append(data)


def parse_spanned_table(html: str) -> list[list[str]]:
    """展开候选表中的 rowspan/colspan，并返回列对齐的二维字符串数组。"""

    parser = _RawTableParser()
    parser.feed(str(html or ""))
    parser.close()
    if not parser.rows:
        return []

    # scheduled[row][column] 保存前面单元格通过 rowspan 延伸到当前行的值。
    scheduled: dict[int, dict[int, str]] = {}
    expanded_maps: list[dict[int, str]] = []
    max_width = 0

    for row_index, raw_row in enumerate(parser.rows):
        row_map = dict(scheduled.pop(row_index, {}))
        column_index = 0
        for cell in raw_row:
            # 当前列已经被上方 rowspan 占用时，移动到下一个空列。
            while column_index in row_map:
                column_index += 1

            # colspan 必须占据一组连续空列；若中间仍有 rowspan，则整体后移。
            while any(
                column_index + offset in row_map
                for offset in range(cell.colspan)
            ):
                column_index += 1

            for offset in range(cell.colspan):
                target_column = column_index + offset
                row_map[target_column] = cell.text
                for future_row in range(
                    row_index + 1,
                    row_index + cell.rowspan,
                ):
                    scheduled.setdefault(future_row, {})[target_column] = cell.text
            column_index += cell.colspan

        expanded_maps.append(row_map)
        if row_map:
            max_width = max(max_width, max(row_map) + 1)

    return [
        [row_map.get(column_index, "") for column_index in range(max_width)]
        for row_map in expanded_maps
    ]


def build_header_paths(
    rows: Sequence[Sequence[str]],
    header_index: int,
) -> tuple[HeaderColumnPath, ...]:
    """根据最后一行表头位置，为每个最终数据列建立去重后的父子路径。"""

    if header_index < 0 or not rows:
        return ()
    width = max(
        (len(row) for row in rows[: header_index + 1]),
        default=0,
    )
    result = []
    for column_index in range(width):
        parts: list[str] = []
        normalized_parts: set[str] = set()
        for row in rows[: header_index + 1]:
            value = (
                _header_text(row[column_index])
                if column_index < len(row)
                else ""
            )
            normalized = _normalize(value)
            # rowspan 展开后相同父表头会在多行重复；路径中只保存一次。
            if value and normalized not in normalized_parts:
                parts.append(value)
                normalized_parts.add(normalized)
        result.append(HeaderColumnPath(column_index, tuple(parts)))
    return tuple(result)


def extend_header_index_for_name_branches(
    rows: Sequence[Sequence[str]],
    header_index: int,
) -> int:
    """严格判断表头下一行是否是多个名称列的分支标签。

    MinerU 展开多层表头后，字段评分通常会停在 ``PIN > NAME`` 这一层，
    而真正区分分支的 ``BQ79616/BQ79614/BQ79612`` 仍位于下一行。只有
    多个名称列拥有完全相同的父路径，且下一行在这些列中给出互不相同的
    非通用短标签时，才把该行纳入表头。普通 ``BALL NAME + SIGNAL NAME``
    的父路径不同，因此不会把第一条数据误判为子表头。
    """

    next_index = header_index + 1
    if header_index < 0 or next_index >= len(rows):
        return header_index

    parent_paths = build_header_paths(rows, header_index)
    name_paths = [
        path
        for path in parent_paths
        if _path_has_name_role(path.parts)
    ]
    if len(name_paths) < 2:
        return header_index

    # 分支列必须来自同一个名称父表头；BALL NAME 与 SIGNAL NAME 虽然
    # 语义等价，但路径文字不同，属于普通等价字段而不是分支轴。
    parent_signatures = {
        _normalize(path.combined)
        for path in name_paths
    }
    if len(parent_signatures) != 1:
        return header_index

    candidate_row = rows[next_index]
    branch_labels = [
        _header_text(candidate_row[path.column_index])
        if path.column_index < len(candidate_row)
        else ""
        for path in name_paths
    ]
    normalized_labels = [_normalize(label) for label in branch_labels]
    if (
        any(not label for label in normalized_labels)
        or len(set(normalized_labels)) != len(normalized_labels)
        or any(_is_generic_branch_label(label) for label in normalized_labels)
        or any(len(label) > 80 for label in branch_labels)
    ):
        return header_index

    # 非名称字段若由 rowspan 延伸到当前行，其文字应与父表头一致。
    # 若这些列突然出现普通数据，则当前行就是首条数据，不能吞进表头。
    name_indexes = {path.column_index for path in name_paths}
    parent_by_column = {
        path.column_index: _normalize(path.parts[-1] if path.parts else "")
        for path in parent_paths
    }
    for column_index, parent_value in parent_by_column.items():
        if column_index in name_indexes:
            continue
        child_value = _normalize(
            candidate_row[column_index]
            if column_index < len(candidate_row)
            else ""
        )
        if child_value and child_value != parent_value:
            return header_index

    return next_index


def analyze_name_column_layout(
    header_paths: Sequence[HeaderColumnPath],
) -> NameColumnLayout:
    """区分单名称列、等价名称列和多封装名称分支。"""

    name_paths = [
        path
        for path in header_paths
        if _path_has_name_role(path.parts)
    ]
    indexes = tuple(path.column_index for path in name_paths)
    if not name_paths:
        return NameColumnLayout(
            mode="none",
            evidence=("表头路径中没有名称字段",),
        )
    if len(name_paths) == 1:
        return NameColumnLayout(
            mode="single",
            name_column_indexes=indexes,
            evidence=("只有一个名称列",),
        )

    residual_by_column = {
        path.column_index: _name_branch_residual(path.parts)
        for path in name_paths
    }
    common_parts = _common_residual_parts(residual_by_column.values())

    labels_by_column: dict[int, str] = {}
    for column_index, residual_parts in residual_by_column.items():
        unique_parts = [
            part
            for part in residual_parts
            if _normalize(part) not in common_parts
        ]
        labels_by_column[column_index] = " ".join(unique_parts).strip()

    grouped: dict[str, list[int]] = {}
    display_labels: dict[str, str] = {}
    for column_index, label in labels_by_column.items():
        normalized_label = _normalize(label)
        if not normalized_label or _is_generic_branch_label(normalized_label):
            continue
        grouped.setdefault(normalized_label, []).append(column_index)
        display_labels.setdefault(normalized_label, label)

    # 每个名称列都必须归入某个分支，且至少存在两个互不相同的分支。
    grouped_count = sum(len(column_indexes) for column_indexes in grouped.values())
    if len(grouped) >= 2 and grouped_count == len(name_paths):
        branches = tuple(
            NameColumnBranch(
                label=display_labels[label],
                column_indexes=tuple(column_indexes),
            )
            for label, column_indexes in grouped.items()
        )
        return NameColumnLayout(
            mode="package_branches",
            name_column_indexes=indexes,
            branches=branches,
            evidence=(
                "多个名称列共享名称语义父节点",
                f"识别出 {len(branches)} 个不同子分支",
            ),
        )

    # BALL NAME 与 SIGNAL NAME 等不同写法在项目中等价；没有分支轴时
    # 只能选择一列，绝不能把多个名称值拼接。
    return NameColumnLayout(
        mode="equivalent_names",
        name_column_indexes=indexes,
        evidence=("多个名称列没有形成完整且唯一的分支标签",),
    )


def name_layout_to_dict(layout: NameColumnLayout) -> dict[str, object]:
    """把名称布局转换成模型请求和调试文件使用的普通字典。"""

    return {
        "mode": layout.mode,
        "name_column_indexes": list(layout.name_column_indexes),
        "branches": [
            {
                "label": branch.label,
                "column_indexes": list(branch.column_indexes),
            }
            for branch in layout.branches
        ],
        "evidence": list(layout.evidence),
    }


def header_paths_to_lists(
    header_paths: Sequence[HeaderColumnPath],
) -> list[list[str]]:
    """把不可变表头路径转换成 JSON 可序列化的二维列表。"""

    return [list(path.parts) for path in header_paths]


def _path_has_name_role(parts: Sequence[str]) -> bool:
    """判断一条表头路径是否表示项目约定的等价名称字段。"""

    normalized_parts = [_normalize(part) for part in parts if _normalize(part)]
    combined = " ".join(normalized_parts)
    if re.search(
        r"\b(?:pin|ball|signal|terminal)\s*name\b",
        combined,
    ):
        return True
    if any(
        term in combined
        for term in ("引脚名称", "信号名称", "端子名称", "引脚名")
    ):
        return True
    # ``Package A > NAME`` 这类表头把对象/封装标签放在 NAME 上方，
    # 路径本身已提供分支父节点，因此裸 NAME 也属于名称字段。
    if "name" in normalized_parts and len(normalized_parts) >= 2:
        return True
    # 多层表头常把 PIN 和 NAME 分到上下两行。
    return (
        "name" in normalized_parts
        and any(
            part in {"pin", "ball", "signal", "terminal"}
            for part in normalized_parts
        )
    )


def _name_branch_residual(parts: Sequence[str]) -> tuple[str, ...]:
    """移除名称字段角色文字，只保留可能表示分支身份的路径部分。"""

    result = []
    for part in parts:
        normalized = _normalize(part)
        if not normalized:
            continue
        if _is_name_role_part(normalized):
            continue
        # 某些解析结果把角色和分支压在同一个单元格，例如
        # ``PIN NAME Package A``。只删除角色文字，保留 Package A。
        residual = _strip_name_role_text(part)
        if residual:
            result.append(residual)
    return tuple(result)


def _strip_name_role_text(value: str) -> str:
    """从组合表头中删除名称角色词，但保留同单元格内的分支标签。"""

    text = _header_text(value)
    text = re.sub(
        r"\b(?:pin|ball|signal|terminal)\s*name\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:引脚名称|信号名称|端子名称|引脚名)",
        " ",
        text,
    )
    # ``Package A > NAME`` 的 NAME 可能单独附在组合文字末尾。
    text = re.sub(r"\bname\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -:/")


def _is_name_role_part(value: str) -> bool:
    """识别不应被误当作封装分支标签的名称字段角色文字。"""

    if value in {"pin", "ball", "signal", "terminal", "name"}:
        return True
    if re.fullmatch(r"(?:pin|ball|signal|terminal)\s*name", value):
        return True
    return value in {"引脚", "信号", "端子", "名称", "引脚名称", "信号名称", "端子名称", "引脚名"}


def _common_residual_parts(
    residual_rows: Iterable[Sequence[str]],
) -> set[str]:
    """找出所有名称路径共有的非语义父标题，防止把章节名当作分支名。"""

    normalized_sets = [
        {_normalize(part) for part in row if _normalize(part)}
        for row in residual_rows
    ]
    if not normalized_sets:
        return set()
    common = set(normalized_sets[0])
    for values in normalized_sets[1:]:
        common.intersection_update(values)
    return common


def _is_generic_branch_label(value: str) -> bool:
    """排除只能表示字段角色、不能表示分支身份的通用标签。"""

    return value in {
        "pin",
        "ball",
        "signal",
        "terminal",
        "name",
        "pin name",
        "ball name",
        "signal name",
        "terminal name",
        "package",
        "packages",
        "封装",
    }


def _positive_span(value: str | None) -> int:
    """把非法或缺失的 span 属性统一转换成 1。"""

    try:
        return max(1, int(str(value or "1")))
    except ValueError:
        return 1


def _clean_cell_text(value: str) -> str:
    """清理单元格文本，同时保留由 br 产生的明确换行。"""

    value = str(value or "").replace("\xa0", " ")
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in value.splitlines()
    ]
    return "\n".join(line for line in lines if line)


def _header_text(value: str) -> str:
    """把表头中的视觉换行折叠成普通空格。"""

    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize(value: str) -> str:
    """生成只用于表头关系比较的规范化文本。"""

    value = _header_text(value).lower()
    value = re.sub(r"[\(\)\[\]{}†‡*]+", " ", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff/+.-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()
