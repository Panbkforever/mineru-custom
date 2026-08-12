"""解析候选表的多层表头，并判断多个名称列之间的结构关系。

本模块只处理已经通过宽松初筛的候选表，不判断表格是否需要提取，不调用
大模型，不判断真实 pkg，也不生成任何引脚记录。

固定处理流程：

1. 展开 HTML 单元格的 ``rowspan`` 和 ``colspan``，形成列对齐的二维表格。
2. 只依据父子行的跨列结构、重复父节点、子节点分裂关系和后续稳定数据结构，
   独立确定完整表头与数据起始行；字段语义不参与边界判断。
3. 同时处理一个名称对应多个编号、一个编号对应多个名称以及多层封装分支表头。
4. 根据确定的最后一行表头，为每个数据列建立完整表头路径。
5. 将 PIN NAME、BALL NAME、SIGNAL NAME、TERMINAL NAME 统一视为
   ``pin_name`` 语义。
6. 多个等价名称字段仍属于普通单封装字段；名称列具有共同语义父节点且存在
   不同子分支时，还要继续区分“物理封装分支”和“运行模式分支”。

特别重要的项目规则：

* BALL NAME 与 SIGNAL NAME 等价，不能因为两列同时出现就判定为多封装。
* 名称角色后的 ``[2]``、``(3)``、†、‡ 等脚注只属于表头注释，不能作为
  package 分支标签。
* 普通等价名称列最终只能选择一列，禁止使用 ``|`` 合并。
* ``NAME > Package A/Package B`` 和
  ``Package A/Package B > NAME`` 都属于封装分支结构。
* ``MII Mode Pin Name``、``PHY Mode Pin Name`` 等列属于运行模式分支；这些列
  可以分别提取名称，但绝不能据此增加 pkg 数量。
* ``SOP Mode Signal Name`` 与 ``Pinlist Signal Name`` 这类“模式 + 清单视图”
  并排名称列同样不是物理封装轴。
* 本模块返回的 ``branch_label`` 只是表内分支证据，不是最终公开 pkg 名称。
* ``-``、``--``、``—`` 和空单元格属于数据值；边界判断不能以占位符为由删行。
* 表头边界不能根据 ``PIN``、``NAME`` 等固定文字，也不能根据单元格内容
  “像不像引脚号”来判断；器件型号中的 ``Q1`` 等片段不是边界证据。
* 只有“父行连续重复覆盖多列、子行把该区域拆成多个标签、区域外列保持原有
  表头”同时成立时，才把下一行并入表头。
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
class HeaderBoundary:
    """一个候选表已经确认的连续表头区域。"""

    header_start: int
    header_end: int
    data_start: int
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableHeaderStructure:
    """候选表冻结后的表头结构，是后续所有字段判断的唯一边界来源。"""

    header_rows: tuple[int, ...]
    data_start_row: int
    columns: tuple[HeaderColumnPath, ...]
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0


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
    header_rows = _repair_structural_header_blanks(
        rows[: header_index + 1],
        width,
    )
    result = []
    for column_index in range(width):
        parts: list[str] = []
        normalized_parts: set[str] = set()
        for row in header_rows:
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


def analyze_table_header_structure(
    rows: Sequence[Sequence[str]],
) -> TableHeaderStructure:
    """不读取字段关键词，纯结构确定表头区域和数据起始行。

    默认第一行属于表头。随后只在相邻两行形成明确的父子拆分关系时向下
    延伸。父节点覆盖整张表、没有区域外稳定列时，还必须由下一行证明：候选
    子表头与后续数据行的列形态明显不同。这样既支持任意语言和任意字段名，
    也不会把普通两列数据误当成第二层表头。
    """

    if not rows:
        return TableHeaderStructure((), 0, (), ("表格没有可分析行",), 0.0)

    boundary = 0
    evidence = ["第 1 行作为结构分析起点"]
    while boundary + 1 < len(rows):
        child_index = boundary + 1
        reason = _structural_header_refinement_reason(
            rows[boundary],
            rows[child_index],
            child_index,
            following_rows=rows[child_index + 1 : child_index + 5],
        )
        if not reason:
            break
        boundary = child_index
        evidence.append(reason)

    header_rows = tuple(range(boundary + 1))
    columns = build_header_paths(rows, boundary)
    confidence = min(1.0, 0.65 + 0.1 * max(0, len(header_rows) - 1))
    if boundary + 1 >= len(rows):
        # 没有正文行时仍返回结构，但降低可信度，后续候选筛选会拒绝空数据表。
        confidence = min(confidence, 0.35)
        evidence.append("表头之后没有正文行")
    else:
        evidence.append(f"第 {boundary + 2} 行是冻结后的数据起始行")

    return TableHeaderStructure(
        header_rows=header_rows,
        data_start_row=boundary + 1,
        columns=columns,
        evidence=tuple(evidence),
        confidence=confidence,
    )


def _repair_structural_header_blanks(
    rows: Sequence[Sequence[str]],
    width: int,
) -> list[list[str]]:
    """用明确的父子分组关系补齐 span 展开后遗留的内部空父节点。

    只在下一层同一标签同时覆盖当前列和相邻列时，才从相邻列继承父节点。
    例如父行第 4 列为 ``PIN``、第 5 列异常为空，而子行第 4、5 列都属于
    ``RGE``，此时可确认两列共享同一父节点。普通空单元格不会被左填充。
    """

    matrix = [
        [_header_text(row[index]) if index < len(row) else "" for index in range(width)]
        for row in rows
    ]
    for level in range(len(matrix) - 2, -1, -1):
        child = matrix[level + 1]
        parent = matrix[level]
        for index in range(width):
            if parent[index] or not child[index]:
                continue
            neighbors = []
            if index > 0 and child[index - 1] == child[index]:
                neighbors.append(index - 1)
            if index + 1 < width and child[index + 1] == child[index]:
                neighbors.append(index + 1)
            inherited = {
                parent[neighbor]
                for neighbor in neighbors
                if parent[neighbor]
            }
            if len(inherited) == 1:
                parent[index] = inherited.pop()
    return matrix


def resolve_header_boundary(
    rows: Sequence[Sequence[str]],
    seed_header_index: int,
) -> HeaderBoundary:
    """兼容旧调用；边界现在统一由纯结构分析生成。

    ``seed_header_index`` 仅用于兼容旧接口，不再影响边界。这样调用方即使
    先做过字段识别，也不能把字段关键词重新带回表头/数据边界判断。
    """

    if seed_header_index < 0 or not rows:
        return HeaderBoundary(0, -1, 0, ("没有可用的字段表头种子行",))
    structure = analyze_table_header_structure(rows)
    boundary = structure.data_start_row - 1
    return HeaderBoundary(
        header_start=0,
        header_end=boundary,
        data_start=structure.data_start_row,
        evidence=structure.evidence,
    )


def extend_header_index_for_name_branches(
    rows: Sequence[Sequence[str]],
    header_index: int,
) -> int:
    """兼容旧调用；实际统一处理编号分支、名称分支和多层分支。"""

    return resolve_header_boundary(rows, header_index).header_end


def _structural_header_refinement_reason(
    parent: Sequence[str],
    child: Sequence[str],
    child_index: int,
    *,
    following_rows: Sequence[Sequence[str]] = (),
) -> str:
    """只依据相邻两行的列结构判断子行是否仍属于表头。

    父行中同一文字连续覆盖两个及以上列，代表一个由 colspan 或视觉合并
    产生的父节点。子行只有把至少一个父节点拆成多个非空子标签，并且父节点
    之外的非空列仍重复原值，才会被接受为下一层表头。
    """

    width = max(len(parent), len(child))
    # 先按明确的共同子分组修复父行内部空位，再分析父子关系。这个步骤只
    # 依赖相邻单元格的 span 结构，不会把普通正文空值向左或向右填充。
    repaired = _repair_structural_header_blanks((parent, child), width)
    parent_values = [_normalize(value) for value in repaired[0]]
    child_values = [_normalize(value) for value in repaired[1]]
    groups = _contiguous_repeated_groups(parent_values)
    split_groups: list[tuple[int, ...]] = []
    intermediate_groups: list[tuple[int, ...]] = []
    for group in groups:
        labels = [child_values[index] for index in group]
        if all(labels) and len(set(labels)) >= 2:
            split_groups.append(group)
        elif (
            all(labels)
            and len(set(labels)) == 1
            and labels[0] != parent_values[group[0]]
            and _following_row_splits_group(group, child_values, following_rows)
        ):
            # 例如 ``PIN -> NAME -> Device A/Device B``：中间 NAME 行本身
            # 尚未分裂，但紧邻下一行会分裂同一区域，因此 NAME 仍是表头。
            intermediate_groups.append(group)
    if not split_groups and not intermediate_groups:
        return ""

    structural_groups = split_groups + intermediate_groups
    branch_indexes = {index for group in structural_groups for index in group}
    unchanged_outside = 0
    for index in range(width):
        if index in branch_indexes or not child_values[index]:
            continue
        if child_values[index] != parent_values[index]:
            return ""
        unchanged_outside += 1

    # 纯两列表在没有外部稳定列时无法仅靠结构区分“子表头”和第一条数据。
    # 多个独立父节点同时分裂本身是足够强的结构证据。
    if unchanged_outside == 0 and len(structural_groups) < 2:
        # 父节点覆盖整张表时没有“区域外稳定列”可供验证。此时必须检查
        # 下一批行的无语义列形态，只有候选子行与后续正文存在明显结构变化
        # 才接受，避免 AXIS/AXIS 后的 VALUE-A/VALUE-B 被误当表头。
        if not _child_differs_from_following_data(child, following_rows):
            return ""

    spans = ", ".join(
        f"{group[0] + 1}-{group[-1] + 1}"
        for group in structural_groups
    )
    return f"第 {child_index + 1} 行按重复父节点拆分列 {spans}"


def _following_row_splits_group(
    group: Sequence[int],
    child_values: Sequence[str],
    following_rows: Sequence[Sequence[str]],
) -> bool:
    """确认中间父层在下一行确实拆成多个不同子标签。"""

    if not following_rows:
        return False
    next_row = following_rows[0]
    labels = [_normalize(_cell(next_row, index)) for index in group]
    if not all(labels) or len(set(labels)) < 2:
        return False
    # 中间行覆盖该区域的标签必须保持一致，避免从普通数据行跨层寻找模式。
    return len({child_values[index] for index in group}) == 1


def _child_differs_from_following_data(
    child: Sequence[str],
    following_rows: Sequence[Sequence[str]],
) -> bool:
    """用不含字段词典的单元格形态确认整表父节点下的子表头。"""

    usable = [row for row in following_rows if any(str(value or "").strip() for value in row)]
    if not usable:
        return False
    child_profile = _row_shape_profile(child)
    similarities = [
        _profile_similarity(child_profile, _row_shape_profile(row))
        for row in usable
    ]
    return sum(similarities) / len(similarities) < 0.72


def _row_shape_profile(row: Sequence[str]) -> tuple[str, ...]:
    """把一行转换成通用形态，不判断内容是否像引脚或字段名。"""

    return tuple(_cell_shape(value) for value in row)


def _cell_shape(value: object) -> str:
    """按空值、数值、紧凑标识符、短语和长文本区分单元格形态。"""

    text = " ".join(str(value or "").split())
    if not text:
        return "empty"
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", text):
        return "number"
    if len(text) <= 40 and not re.search(r"\s", text):
        return "compact"
    if len(text) <= 80:
        return "phrase"
    return "long_text"


def _profile_similarity(
    left: Sequence[str],
    right: Sequence[str],
) -> float:
    """计算两个无语义行形态的逐列相似度。"""

    width = max(len(left), len(right))
    if width == 0:
        return 1.0
    matches = 0.0
    for index in range(width):
        left_value = left[index] if index < len(left) else "empty"
        right_value = right[index] if index < len(right) else "empty"
        if left_value == right_value:
            matches += 1.0
        elif {left_value, right_value} <= {"compact", "number"}:
            # 数值与紧凑标识符都属于原子值，但仍只计半分。
            matches += 0.5
    return matches / width


def _contiguous_repeated_groups(values: Sequence[str]) -> list[tuple[int, ...]]:
    """返回同一非空值连续覆盖的列组，不合并相隔较远的同名数据。"""

    groups: list[tuple[int, ...]] = []
    start = 0
    while start < len(values):
        value = values[start]
        end = start + 1
        while end < len(values) and value and values[end] == value:
            end += 1
        if value and end - start >= 2:
            groups.append(tuple(range(start, end)))
        start = end
    return groups


def _cell(row: Sequence[str], column_index: int) -> str:
    """按列号读取结构判断值，越界列统一返回空字符串。"""

    return _header_text(row[column_index]) if column_index < len(row) else ""


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
        # ``BALL NAME [2]`` 与 ``SIGNAL NAME [3]`` 中的 2/3 只是脚注。
        # 名称角色被删除后不能再把这些纯数字残留解释成两个封装分支。
        if (
            not normalized_label
            or _is_generic_branch_label(normalized_label)
            or _is_footnote_only_branch_label(normalized_label)
        ):
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
        # ``XXX Mode Pin Name`` 的横向分支描述的是同一物理封装在不同运行
        # 模式下的信号名称，不是多个物理封装。结构层必须先把这条轴标明，
        # 后续单表提取可以保留各分支，但文档级 pkg 目录不能把它们计数。
        if _branches_are_operating_modes(branches) or _branches_mix_mode_and_view_labels(
            branches
        ):
            return NameColumnLayout(
                mode="parallel_name_branches",
                name_column_indexes=indexes,
                branches=branches,
                evidence=(
                    "多个名称列共享名称语义父节点",
                    f"识别出 {len(branches)} 个运行模式名称分支",
                    "运行模式分支不构成物理封装分支",
                ),
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


def _branches_are_operating_modes(
    branches: Sequence[NameColumnBranch],
) -> bool:
    """确认全部分支标签都明确以 Mode/模式描述运行方式。

    这里只接受显式 ``Mode`` 或 ``模式`` 文字，不根据 MAC、PHY、RGMII 等
    协议名称猜测，避免把恰好含有协议缩写的真实封装标签错误降级。
    """

    if len(branches) < 2:
        return False
    return all(
        bool(
            re.search(r"(?:^|\s)mode(?:\s|$)", _normalize(branch.label))
            or "模式" in _normalize(branch.label)
        )
        for branch in branches
    )


def _branches_mix_mode_and_view_labels(
    branches: Sequence[NameColumnBranch],
) -> bool:
    """识别“运行模式名称列 + 普通引脚清单名称列”的非封装横轴。

    某些表把 ``SOP Mode Signal Name`` 与 ``Pinlist Signal Name`` 并排放置。
    两列描述的是同一物理封装的不同视图，不是两个 package。只要分支中明确
    出现 Mode/模式，同时没有 Package/封装等物理封装轴文字，就保持多名称
    读取，但禁止这些名称列增加文档级 pkg 数量。
    """

    if len(branches) < 2:
        return False
    labels = [_normalize(branch.label) for branch in branches]
    has_mode_label = any(
        re.search(r"(?:^|\s)mode(?:\s|$)", label) or "模式" in label
        for label in labels
    )
    has_explicit_package_axis = any(
        re.search(r"(?:^|\s)(?:package|pkg)(?:\s|$)", label)
        or "封装" in label
        for label in labels
    )
    return has_mode_label and not has_explicit_package_axis


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
    # 中文父字段和叶子字段经 rowspan/colspan 展开后通常位于不同层，
    # 完整路径会表现为“引脚 名称”，不能只识别没有空格的合并字符串。
    if "名称" in normalized_parts and any(
        part in {"引脚", "端子", "信号", "球"}
        for part in normalized_parts
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


def _is_footnote_only_branch_label(value: str) -> bool:
    """判断名称角色删除后是否只剩脚注编号或脚注符号。

    ``_normalize`` 已经删除括号、方括号和 †/‡/*，所以 ``[2]``、``(1)(2)``
    在这里分别表现为 ``2``、``1 2``。这类值没有字母或汉字，不具备任何
    封装身份语义，必须在名称分支分组前排除。
    """

    return not bool(re.search(r"[a-z\u4e00-\u9fff]", value))


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
