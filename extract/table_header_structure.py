"""解析候选表的多层表头，并判断多个名称列之间的结构关系。

本模块只处理已经通过宽松初筛的候选表，不判断表格是否需要提取，不调用
大模型，不判断真实 pkg，也不生成任何引脚记录。

固定处理流程：

1. 展开 HTML 单元格的 ``rowspan`` 和 ``colspan``，形成列对齐的二维表格。
2. 从字段语义种子行开始，结合当前行、后续分支行和首批数据行确定完整表头边界。
3. 同时处理一个名称对应多个编号、一个编号对应多个名称以及多层封装分支表头。
4. 根据确定的最后一行表头，为每个数据列建立完整表头路径。
5. 将 PIN NAME、BALL NAME、SIGNAL NAME、TERMINAL NAME 统一视为
   ``pin_name`` 语义。
6. 多个等价名称字段仍属于普通单封装字段；名称列具有共同语义父节点且存在
   不同子分支时，还要继续区分“物理封装分支”和“运行模式分支”。

特别重要的项目规则：

* BALL NAME 与 SIGNAL NAME 等价，不能因为两列同时出现就判定为多封装。
* 普通等价名称列最终只能选择一列，禁止使用 ``|`` 合并。
* ``NAME > Package A/Package B`` 和
  ``Package A/Package B > NAME`` 都属于封装分支结构。
* ``MII Mode Pin Name``、``PHY Mode Pin Name`` 等列属于运行模式分支；这些列
  可以分别提取名称，但绝不能据此增加 pkg 数量。
* 本模块返回的 ``branch_label`` 只是表内分支证据，不是最终公开 pkg 名称。
* ``-``、``--``、``—`` 和空单元格属于数据值；边界判断不能以占位符为由删行。
* 表头边界必须由整行结构和后续数据一致性共同决定，不能仅依赖某个关键词。
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


def resolve_header_boundary(
    rows: Sequence[Sequence[str]],
    seed_header_index: int,
) -> HeaderBoundary:
    """从字段种子行向下确定完整的多层表头边界。

    种子行只说明已经出现了 NAME/NO./TYPE 等字段语义，不能说明表头已经
    结束。本函数继续检查后续行是否在重复的编号轴或名称轴上提供封装分支，
    并用其后的稳定数据行反证。任何已经进入数据区的值，包括 ``-``，都不会
    在这里被过滤或改写。
    """

    if seed_header_index < 0 or seed_header_index >= len(rows):
        return HeaderBoundary(0, -1, 0, ("没有可用的字段表头种子行",))

    boundary = seed_header_index
    evidence = [f"字段语义种子行位于第 {seed_header_index + 1} 行"]

    # 每次只允许紧邻下一行通过完整结构校验；一旦命中真实数据立即停止。
    # 不限制表头层数，避免把合法的三层以上封装表头截断。
    while boundary + 1 < len(rows):
        candidate_index = boundary + 1
        reason = _header_refinement_reason(rows, boundary, candidate_index)
        if not reason:
            break
        boundary = candidate_index
        evidence.append(reason)

    return HeaderBoundary(
        header_start=0,
        header_end=boundary,
        data_start=boundary + 1,
        evidence=tuple(evidence),
    )


def extend_header_index_for_name_branches(
    rows: Sequence[Sequence[str]],
    header_index: int,
) -> int:
    """兼容旧调用；实际统一处理编号分支、名称分支和多层分支。"""

    return resolve_header_boundary(rows, header_index).header_end


def _header_refinement_reason(
    rows: Sequence[Sequence[str]],
    header_index: int,
    candidate_index: int,
) -> str:
    """判断紧邻行是否细化现有字段轴，而不是首条真实数据。"""

    parent_paths = build_header_paths(rows, header_index)
    roles = {
        path.column_index: _structural_column_role(path.parts)
        for path in parent_paths
    }
    candidate = rows[candidate_index]

    # 一行已经同时满足编号、名称/类型/描述的数据组合时必须立即停止。
    # ``-``在编号列中也算数据占位符，绝不能因为它不是普通编号而吞入表头。
    if _looks_like_complete_data_row(candidate, roles):
        return ""

    branch_groups = _repeated_semantic_axes(roles)
    if not branch_groups:
        return ""

    for role, column_indexes in branch_groups:
        labels = [_cell(candidate, index) for index in column_indexes]
        if not _labels_can_refine_axis(labels):
            continue
        if not _non_branch_cells_remain_headers(
            candidate,
            parent_paths,
            set(column_indexes),
        ):
            continue

        # 当前层可以是最终分支标签，也可以是多个子分支共享的中间父标签。
        # 中间父标签必须由下一行产生更细分区，否则相同文字不能单独证明表头。
        distinct_labels = {_normalize(label) for label in labels if _normalize(label)}
        if len(distinct_labels) < 2 and not _next_row_splits_axis(
            rows,
            candidate_index,
            column_indexes,
        ):
            continue

        prospective_roles = {
            path.column_index: _structural_column_role(path.parts)
            for path in build_header_paths(rows, candidate_index)
        }
        if not _following_rows_support_data_or_deeper_header(
            rows,
            candidate_index,
            prospective_roles,
            column_indexes,
        ):
            continue
        return f"第 {candidate_index + 1} 行继续细化 {role} 分支轴"
    return ""


def _repeated_semantic_axes(
    roles: dict[int, str],
) -> list[tuple[str, tuple[int, ...]]]:
    """找出可由下一层标签继续细化的重复编号轴或名称轴。"""

    grouped: dict[str, list[int]] = {}
    for column_index, role in roles.items():
        if role not in {"pin_no", "pin_axis", "pin_name"}:
            continue
        axis = "pin_no" if role in {"pin_no", "pin_axis"} else "pin_name"
        grouped.setdefault(axis, []).append(column_index)
    return [
        (role, tuple(indexes))
        for role, indexes in grouped.items()
        if len(indexes) >= 2
    ]


def _labels_can_refine_axis(labels: Sequence[str]) -> bool:
    """分支标签必须完整、较短，并且不能本身就是物理引脚数据。"""

    cleaned = [_header_text(label) for label in labels]
    if any(not label or len(label) > 80 for label in cleaned):
        return False
    normalized = [_normalize(label) for label in cleaned]
    if any(not label or _is_generic_branch_label(label) for label in normalized):
        return False
    return not any(_looks_like_pin_data_value(label) for label in cleaned)


def _next_row_splits_axis(
    rows: Sequence[Sequence[str]],
    candidate_index: int,
    column_indexes: Sequence[int],
) -> bool:
    """允许“共同器件名 -> 多个封装名”这种两级分支表头。"""

    next_index = candidate_index + 1
    if next_index >= len(rows):
        return False
    labels = [_cell(rows[next_index], index) for index in column_indexes]
    if not _labels_can_refine_axis(labels):
        return False
    return len({_normalize(label) for label in labels}) >= 2


def _following_rows_support_data_or_deeper_header(
    rows: Sequence[Sequence[str]],
    candidate_index: int,
    roles: dict[int, str],
    column_indexes: Sequence[int],
) -> bool:
    """用后续三行确认当前行后面确实存在稳定数据或更深分支。"""

    for row in rows[candidate_index + 1 : candidate_index + 4]:
        if _looks_like_complete_data_row(row, roles):
            return True
    return _next_row_splits_axis(rows, candidate_index, column_indexes)


def _non_branch_cells_remain_headers(
    candidate: Sequence[str],
    parent_paths: Sequence[HeaderColumnPath],
    branch_indexes: set[int],
) -> bool:
    """分支轴之外的列只能为空、重复父表头或继续写通用字段名。"""

    for path in parent_paths:
        if path.column_index in branch_indexes:
            continue
        child = _normalize(_cell(candidate, path.column_index))
        if not child:
            continue
        ancestors = {_normalize(part) for part in path.parts if _normalize(part)}
        if child in ancestors or _is_generic_header_label(child):
            continue
        return False
    return True


def _looks_like_complete_data_row(
    row: Sequence[str],
    roles: dict[int, str],
) -> bool:
    """根据字段组合判断一行是否已经进入数据区。"""

    number_indexes = [
        index for index, role in roles.items() if role in {"pin_no", "pin_axis"}
    ]
    name_indexes = [index for index, role in roles.items() if role == "pin_name"]
    type_indexes = [index for index, role in roles.items() if role == "type"]
    description_indexes = [
        index for index, role in roles.items() if role == "description"
    ]

    number_hits = sum(
        _looks_like_pin_data_value(_cell(row, index)) for index in number_indexes
    )
    if not number_hits:
        return False
    name_hits = sum(
        _looks_like_name_data_value(_cell(row, index)) for index in name_indexes
    )
    type_hits = sum(
        _looks_like_type_data_value(_cell(row, index)) for index in type_indexes
    )
    description_hits = sum(
        len(_header_text(_cell(row, index))) >= 12
        for index in description_indexes
    )
    return bool(name_hits or type_hits or description_hits)


def _structural_column_role(parts: Sequence[str]) -> str:
    """从完整表头路径识别边界判断所需的宽松字段角色。"""

    combined = _normalize(" ".join(parts))
    if not combined:
        return ""
    if _path_has_name_role(parts):
        return "pin_name"
    if re.search(
        r"\b(?:pin|ball|terminal)\s*(?:no\.?|number)\b",
        combined,
    ) or any(term in combined for term in ("引脚编号", "端子编号", "球编号")):
        return "pin_no"
    if combined in {"no", "no.", "number", "pins", "balls", "terminals"}:
        return "pin_no"
    if "description" in combined or "说明" in combined or "描述" in combined:
        return "description"
    if re.search(r"\b(?:i/?o|io|signal type|pin type|terminal type|type)\b", combined):
        return "type"
    # 多封装表可能只在父层写 PIN，子层直接写 PWP/RGE/器件型号。
    # 这些列属于编号轴，但尚未出现 NO. 文字，必须保留为可细化结构角色。
    if re.search(r"\b(?:pin|ball|terminal)\b", combined) or any(
        term in combined for term in ("引脚", "端子", "球")
    ):
        return "pin_axis"
    return ""


def _looks_like_pin_data_value(value: str) -> bool:
    """识别编号列中的真实编号、列表、范围和必须保留的占位符。"""

    text = _header_text(value)
    if not text:
        return False
    if text in {"-", "--", "—", "–"}:
        return True
    if re.fullmatch(r"\d{1,4}", text):
        return True
    if re.fullmatch(r"[A-Za-z]+\d+\s*-\s*[A-Za-z]+\d+", text):
        return True
    if re.fullmatch(r"[A-Za-z]+\s*\[\s*\d+\s*:\s*\d+\s*\]", text):
        return True
    # BGA球号通常是一到两个字母加数字；三字母以上更常见于器件/封装型号，
    # 例如 DRV8256E，不能在表头边界阶段把它误判成物理引脚数据。
    tokens = re.findall(r"\b[A-Za-z]{1,2}\d{1,4}\b", text)
    return bool(tokens)


def _looks_like_name_data_value(value: str) -> bool:
    """排除通用字段名后，非空短文本可以作为名称列的数据证据。"""

    text = _header_text(value)
    normalized = _normalize(text)
    return bool(text) and not _is_generic_header_label(normalized)


def _looks_like_type_data_value(value: str) -> bool:
    """识别常见引脚类型值，仅用于证明数据区开始。"""

    normalized = re.sub(r"\s+", "", _normalize(value))
    return normalized in {
        "i", "o", "io", "i/o", "i/o/z", "oz", "od", "odz", "p",
        "power", "ground", "gnd", "analog", "digital", "input", "output",
    }


def _is_generic_header_label(value: str) -> bool:
    """识别可以在多层表头中重复出现的字段角色文字。"""

    return value in {
        "pin", "ball", "signal", "terminal", "name", "no", "no.",
        "number", "type", "io", "i/o", "description", "pins", "balls",
        "terminals", "引脚", "端子", "信号", "名称", "编号", "类型", "说明", "描述",
    } or _is_generic_branch_label(value)


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
        # ``XXX Mode Pin Name`` 的横向分支描述的是同一物理封装在不同运行
        # 模式下的信号名称，不是多个物理封装。结构层必须先把这条轴标明，
        # 后续单表提取可以保留各分支，但文档级 pkg 目录不能把它们计数。
        if _branches_are_operating_modes(branches):
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
