"""分析一张引脚表中的多封装关系，并生成确定性的提取计划。

本模块位于“表格/字段判断”与“逐行提取”之间，只负责回答两个问题：

1. 当前表是否包含多个封装的物理引脚映射；
2. 如果是，每个封装应该读取哪一列 pin_no、哪一列 pin_name/type，
   以及应该读取哪些数据行。

本模块不判断表格是否需要提取，不调用大模型，不生成最终 pin JSON，也不
清洗 pin_no/pin_name。普通单封装表继续走原提取逻辑；只有严格命中下列结构
之一时，才返回多封装绑定：

* package_columns：多个封装各有独立 pin_no 列，共享 pin_name/type；
* package_rows：一个 package 控制列把数据行划分给不同封装；
* package_sections：表内用“XXX Package”分段行切换当前封装；
* shared_packages：标题明确说明同一套引脚映射同时适用于多个封装。

横向重复的 ``Pin# | Pin Name | Type`` 字段块由主流程在表格判断阶段直接
过滤，因此不会进入本模块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence


class ColumnLike(Protocol):
    """主提取器列判断对象需要提供的最小接口。"""

    index: int
    raw_header: str
    field_name: str


@dataclass(frozen=True)
class PackageBinding:
    """一个封装与表格列、数据行之间的确定性绑定。"""

    package: str
    pin_no_column: int
    pin_name_column: int | None
    type_column: int | None
    # None 表示使用全部数据行；否则只读取集合内的行号。
    row_indexes: frozenset[int] | None = None


@dataclass(frozen=True)
class MultiPackagePlan:
    """多封装分析结果；这里只保存计划，不保存任何已提取的引脚。"""

    is_multi_package: bool
    mode: str
    bindings: tuple[PackageBinding, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundPackageRow:
    """按绑定计划读取到的一行原始值，后续仍需统一拆分和清洗。"""

    package: str
    row_index: int
    pin_no: str
    pin_name: str
    pin_type: str


@dataclass
class _Section:
    """package_sections 分支在分析过程中使用的可变行集合。"""

    package: str
    row_indexes: set[int] = field(default_factory=set)


def analyze_multi_package_table(
    *,
    title: str,
    header_rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    columns: Sequence[ColumnLike],
) -> MultiPackagePlan:
    """按固定优先级判断表格结构，并返回一个多封装提取计划。

    判断顺序不能随意调整：多个 package 专属 pin_no 列是最明确的多封装
    证据；按行和按分段判断只在表格只有一套 pin_no/pin_name 字段时执行。
    """

    # 完整多行表头不能在进入本模块前丢失。headers 是主提取器当前选中的
    # 逐列表头；某些 PDF 的最后一层子表头会暂时位于 data_rows 第一行，
    # package_columns 分支会在严格确认后把该行补回列标题。

    package_column_plan = detect_package_specific_pin_columns(
        header_rows=header_rows,
        headers=headers,
        data_rows=data_rows,
        columns=columns,
    )
    if package_column_plan is not None:
        return package_column_plan

    package_row_plan = detect_package_selector_column(
        headers=headers,
        data_rows=data_rows,
        columns=columns,
    )
    if package_row_plan is not None:
        return package_row_plan

    package_section_plan = detect_package_section_rows(
        data_rows=data_rows,
        columns=columns,
    )
    if package_section_plan is not None:
        return package_section_plan

    shared_plan = detect_explicit_shared_packages(
        title=title,
        data_rows=data_rows,
        columns=columns,
    )
    if shared_plan is not None:
        return shared_plan

    return MultiPackagePlan(
        False,
        "single_package",
        evidence=("未发现足够的多封装结构证据",),
    )


def detect_package_specific_pin_columns(
    *,
    header_rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    columns: Sequence[ColumnLike],
) -> MultiPackagePlan | None:
    """识别“多个封装编号列 + 一个共享名称列”的多封装表。"""

    pin_columns = _selected_columns(columns, "pin_no")
    name_columns = _selected_columns(columns, "pin_name")
    type_columns = _selected_columns(columns, "type")
    if len(pin_columns) < 2 or len(name_columns) != 1:
        return None

    # 每个封装编号列必须至少包含一个非空值；空列不能构成封装证据。
    if any(not _column_has_values(data_rows, column.index) for column in pin_columns):
        return None

    raw_headers, leading_header_rows = _resolve_package_column_headers(
        header_rows=header_rows,
        headers=headers,
        data_rows=data_rows,
        pin_columns=pin_columns,
        name_column=name_columns[0],
        type_column=type_columns[0] if len(type_columns) == 1 else None,
    )
    package_labels = derive_package_labels_from_headers(raw_headers)
    if len(package_labels) != len(pin_columns):
        return None
    if any(not label for label in package_labels):
        return None
    if len({_normalize_text(label) for label in package_labels}) != len(package_labels):
        return None

    pin_name_column = name_columns[0].index
    type_column = type_columns[0].index if len(type_columns) == 1 else None
    usable_rows = (
        frozenset(range(leading_header_rows, len(data_rows)))
        if leading_header_rows
        else None
    )
    bindings = tuple(
        PackageBinding(
            package=package_label,
            pin_no_column=pin_column.index,
            pin_name_column=pin_name_column,
            type_column=type_column,
            row_indexes=usable_rows,
        )
        for package_label, pin_column in zip(package_labels, pin_columns)
    )
    return MultiPackagePlan(
        True,
        "package_columns",
        bindings,
        evidence=(
            f"模型选中 {len(pin_columns)} 个 pin_no 列",
            "这些编号列共享一个 pin_name 列",
            "各编号列的完整表头产生了不同封装标签",
            *(
                (f"从 data_rows 前 {leading_header_rows} 行补回多层子表头",)
                if leading_header_rows
                else ()
            ),
        ),
    )


def detect_package_selector_column(
    *,
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    columns: Sequence[ColumnLike],
) -> MultiPackagePlan | None:
    """识别由 PACKAGE/PKG 列按数据行区分封装的表。"""

    common_columns = _single_mapping_columns(columns)
    if common_columns is None:
        return None
    pin_column, name_column, type_column = common_columns
    selected_indexes = {column.index for column in columns}

    candidates = []
    for index, header in enumerate(headers):
        if index in selected_indexes:
            continue
        score = _package_dimension_header_score(header)
        if score:
            candidates.append((score, index))
    if not candidates:
        return None

    # 同分最高分列意味着 package 维度不唯一，此时不猜测。
    candidates.sort(reverse=True)
    best_score = candidates[0][0]
    best_indexes = [index for score, index in candidates if score == best_score]
    if len(best_indexes) != 1:
        return None
    package_column = best_indexes[0]

    rows_by_package: dict[str, set[int]] = {}
    current_package = ""
    for row_index, row in enumerate(data_rows):
        value = _cell(row, package_column)
        if value:
            current_package = clean_package_label(value)
        # package 列由 rowspan 展开失败时，空单元格继承前一个明确封装值。
        if current_package:
            rows_by_package.setdefault(current_package, set()).add(row_index)

    rows_by_package = {
        package: indexes
        for package, indexes in rows_by_package.items()
        if package and indexes
    }
    if len(rows_by_package) < 2:
        return None

    bindings = tuple(
        PackageBinding(
            package=package,
            pin_no_column=pin_column.index,
            pin_name_column=name_column.index,
            type_column=type_column.index if type_column else None,
            row_indexes=frozenset(sorted(row_indexes)),
        )
        for package, row_indexes in rows_by_package.items()
    )
    return MultiPackagePlan(
        True,
        "package_rows",
        bindings,
        evidence=(
            f"第 {package_column + 1} 列是明确的 package 维度列",
            f"该列把数据行划分为 {len(bindings)} 个封装",
        ),
    )


def detect_package_section_rows(
    *,
    data_rows: Sequence[Sequence[str]],
    columns: Sequence[ColumnLike],
) -> MultiPackagePlan | None:
    """识别由“XXX Package”分段标题行切换封装的表。"""

    common_columns = _single_mapping_columns(columns)
    if common_columns is None:
        return None
    pin_column, name_column, type_column = common_columns

    sections: list[_Section] = []
    current_section: _Section | None = None
    for row_index, row in enumerate(data_rows):
        package = _package_from_section_row(row)
        if package:
            current_section = _Section(package)
            sections.append(current_section)
            continue
        if current_section is not None:
            current_section.row_indexes.add(row_index)

    # 至少两个不同封装分段才属于多封装表。
    package_names = {_normalize_text(section.package) for section in sections if section.package}
    if len(package_names) < 2:
        return None

    bindings = tuple(
        PackageBinding(
            package=section.package,
            pin_no_column=pin_column.index,
            pin_name_column=name_column.index,
            type_column=type_column.index if type_column else None,
            row_indexes=frozenset(sorted(section.row_indexes)),
        )
        for section in sections
        if section.package and section.row_indexes
    )
    if len(bindings) < 2:
        return None
    return MultiPackagePlan(
        True,
        "package_sections",
        bindings,
        evidence=(f"发现 {len(bindings)} 个明确的 Package 分段",),
    )


def detect_explicit_shared_packages(
    *,
    title: str,
    data_rows: Sequence[Sequence[str]],
    columns: Sequence[ColumnLike],
) -> MultiPackagePlan | None:
    """识别标题明确声明同一套引脚适用于多个 Packages 的情况。"""

    common_columns = _single_mapping_columns(columns)
    if common_columns is None:
        return None
    package_names = _explicit_package_list_from_title(title)
    if len(package_names) < 2:
        return None
    pin_column, name_column, type_column = common_columns
    all_rows = frozenset(range(len(data_rows)))
    bindings = tuple(
        PackageBinding(
            package=package,
            pin_no_column=pin_column.index,
            pin_name_column=name_column.index,
            type_column=type_column.index if type_column else None,
            row_indexes=all_rows,
        )
        for package in package_names
    )
    return MultiPackagePlan(
        True,
        "shared_packages",
        bindings,
        evidence=("标题明确使用复数 Packages 声明同一套引脚映射的适用封装",),
    )


def iter_bound_package_rows(
    plan: MultiPackagePlan,
    data_rows: Sequence[Sequence[str]],
) -> Iterable[BoundPackageRow]:
    """严格按照多封装计划读取原始值，不重新判断封装或字段。"""

    if not plan.is_multi_package:
        return
    for binding in plan.bindings:
        allowed_rows = binding.row_indexes
        for row_index, row in enumerate(data_rows):
            if allowed_rows is not None and row_index not in allowed_rows:
                continue
            if _is_structural_group_row(row):
                continue
            pin_no = _cell(row, binding.pin_no_column)
            # 绑定计划已经确定该列就是当前封装的 pin_no。单元格为空属于
            # 原始行数据，交给统一行提取函数保留为 pin_no=""，不能在此丢行。
            yield BoundPackageRow(
                package=binding.package,
                row_index=row_index,
                pin_no=pin_no,
                pin_name=(
                    _cell(row, binding.pin_name_column)
                    if binding.pin_name_column is not None
                    else ""
                ),
                pin_type=(
                    _cell(row, binding.type_column)
                    if binding.type_column is not None
                    else ""
                ),
            )


def plan_to_debug(plan: MultiPackagePlan) -> dict[str, Any]:
    """把计划转换成可写入信息文件的普通字典。"""

    return {
        "is_multi_package": plan.is_multi_package,
        "mode": plan.mode,
        "evidence": list(plan.evidence),
        "bindings": [
            {
                "package": binding.package,
                "pin_no_column": binding.pin_no_column,
                "pin_name_column": binding.pin_name_column,
                "type_column": binding.type_column,
                "row_indexes": (
                    sorted(binding.row_indexes)
                    if binding.row_indexes is not None
                    else None
                ),
            }
            for binding in plan.bindings
        ],
    }


def _resolve_package_column_headers(
    *,
    header_rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str]],
    pin_columns: Sequence[ColumnLike],
    name_column: ColumnLike,
    type_column: ColumnLike | None,
) -> tuple[list[str], int]:
    """补齐被主表头选择暂时留在 data_rows 开头的 package 子表头。

    只有当第一行同时满足以下条件时才当作子表头：共享名称列仍写着名称字段，
    所有 package 编号列都非空且互不相同，并且这些值不像物理引脚编号。
    因此正常的第一条数据行不会因为文本较短而被错误删除。
    """

    base_headers = [_column_header(headers, column) for column in pin_columns]
    if not data_rows:
        return base_headers, 0

    candidate = data_rows[0]
    name_header = _header_role(_cell(candidate, name_column.index)) == "pin_name"
    type_header = (
        type_column is None
        or _header_role(_cell(candidate, type_column.index)) == "type"
    )
    package_cells = [_cell(candidate, column.index) for column in pin_columns]
    normalized_cells = {_normalize_text(value) for value in package_cells if value}
    package_cells_valid = (
        all(package_cells)
        and len(normalized_cells) == len(package_cells)
        and all(not _looks_like_pin_value(value) for value in package_cells)
    )
    if not (name_header and type_header and package_cells_valid):
        return base_headers, 0

    # header_rows 作为已确认父表头存在性的证据；即使父表头为空，子表头本身
    # 仍可形成完整列名，因此这里只保留调用参数而不强制其具体文字。
    _ = header_rows
    combined_headers = []
    for base_header, child_header in zip(base_headers, package_cells):
        if _normalize_text(child_header) in _normalize_text(base_header):
            combined_headers.append(base_header)
        else:
            combined_headers.append(f"{base_header} {child_header}".strip())
    return combined_headers, 1


def derive_package_labels_from_headers(headers: Sequence[str]) -> list[str]:
    """从多个 package 专属编号列的完整表头中提取互不相同的封装标签。"""

    cleaned_headers = [_clean_header_text(header) for header in headers]
    token_rows = [_header_tokens(header) for header in cleaned_headers]
    common_prefix_length = _common_prefix_length(token_rows)

    labels = []
    for header, tokens in zip(cleaned_headers, token_rows):
        # 先移除所有编号列共有的父表头，例如器件型号 GL852G-60。
        unique_tokens = tokens[common_prefix_length:]
        candidate = " ".join(unique_tokens).strip() or header
        labels.append(clean_package_label(_remove_pin_number_role(candidate)))

    # 通用字段名不能作为封装名；此时说明表头证据不足。
    return ["" if _is_generic_pin_header(label) else label for label in labels]


def clean_package_label(value: str) -> str:
    """清理封装标签的脚注和 Package 后缀，保留封装家族、编号及 pin count。"""

    value = _clean_header_text(value)
    value = re.sub(r"\bpackages?\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -:/,;&")


def _selected_columns(columns: Sequence[ColumnLike], field_name: str) -> list[ColumnLike]:
    """按标准字段名读取模型/规则已经选中的列，保持原列顺序。"""

    result = [
        column
        for column in columns
        if _normalize_field_name(column.field_name) == field_name
    ]
    return sorted(result, key=lambda column: column.index)


def _single_mapping_columns(
    columns: Sequence[ColumnLike],
) -> tuple[ColumnLike, ColumnLike, ColumnLike | None] | None:
    """取得单套 pin_no/pin_name/type 映射；多套字段时不做猜测。"""

    pin_columns = _selected_columns(columns, "pin_no")
    name_columns = _selected_columns(columns, "pin_name")
    type_columns = _selected_columns(columns, "type")
    if len(pin_columns) != 1 or len(name_columns) != 1 or len(type_columns) > 1:
        return None
    return pin_columns[0], name_columns[0], type_columns[0] if type_columns else None


def _normalize_field_name(value: str) -> str:
    """只处理本模块需要的字段别名，不重新执行字段判断。"""

    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    aliases = {
        "ball_no": "pin_no",
        "terminal_no": "pin_no",
        "package_pin_no": "pin_no",
        "ball_name": "pin_name",
        "signal_name": "pin_name",
        "terminal_name": "pin_name",
        "io_type": "type",
    }
    return aliases.get(normalized, normalized)


def _header_role(value: str) -> str:
    """识别多封装子表头中的共享名称列和类型列。

    该函数仍被 ``_resolve_package_column_headers()`` 使用，用来确认
    ``SSOP/QFN/LQFP`` 等封装列名所在行确实是第二层表头，而不是数据行。
    横向重复表格的过滤已经迁移到独立模块，不再由本函数负责。
    """

    text = _normalize_text(value)
    if re.search(r"\b(?:pin|ball|terminal)\s*(?:no|number)\b", text) or "pin#" in text:
        return "pin_no"
    if re.search(r"\b(?:pin|ball|signal|terminal)\s*name\b", text):
        return "pin_name"
    if text in {"type", "io", "i o", "i/o"} or re.search(
        r"\b(?:pin|signal|io|i o)\s*type\b", text
    ):
        return "type"
    return ""


def _package_dimension_header_score(value: str) -> int:
    """给明确的 package 控制列表头评分，分数只用于解决列选择。"""

    text = _normalize_text(value)
    if text in {"package", "pkg", "封装"}:
        return 4
    if any(phrase in text for phrase in ("package drawing", "package name", "package type")):
        return 3
    if re.search(r"\b(?:package|pkg)\b", text) or "封装" in text:
        return 2
    return 0


def _package_from_section_row(row: Sequence[str]) -> str:
    """仅接受内容唯一且明确包含 Package/封装的分段行。"""

    values = [str(cell).strip() for cell in row if str(cell).strip()]
    if not values or len({_normalize_text(value) for value in values}) != 1:
        return ""
    value = values[0]
    if not (re.search(r"\bpackage\b", value, re.IGNORECASE) or "封装" in value):
        return ""
    return clean_package_label(value)


def _explicit_package_list_from_title(title: str) -> list[str]:
    """从非常明确的“X and Y Packages”标题结构中读取共享封装列表。"""

    text = _clean_header_text(title)
    match = re.search(
        r"(?:for|applicable\s+to)\s+([^:;]{1,120}?)\s+packages\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return []
    parts = [
        clean_package_label(part)
        for part in re.split(r"\s*(?:,|/|&|\band\b)\s*", match.group(1), flags=re.IGNORECASE)
    ]
    result = []
    for part in parts:
        normalized = _normalize_text(part)
        if part and normalized not in {_normalize_text(item) for item in result}:
            result.append(part)
    return result if len(result) >= 2 else []


def _column_header(headers: Sequence[str], column: ColumnLike) -> str:
    """优先读取逐列完整表头，缺失时使用字段判断对象携带的原表头。"""

    if 0 <= column.index < len(headers) and str(headers[column.index]).strip():
        return str(headers[column.index]).strip()
    return str(column.raw_header or "").strip()


def _column_has_values(data_rows: Sequence[Sequence[str]], column_index: int) -> bool:
    """确认列中确实存在数据，防止把空占位列识别成封装。"""

    return any(_cell(row, column_index) for row in data_rows)


def _remove_pin_number_role(value: str) -> str:
    """从表头首尾移除 PIN NO/BALL NUMBER 等字段角色文字。"""

    role = r"(?:pin|ball|terminal)\s*(?:no\.?|number|#)"
    value = re.sub(rf"^{role}\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(rf"\s*{role}$", "", value, flags=re.IGNORECASE)
    return value.strip()


def _is_generic_pin_header(value: str) -> bool:
    """判断标签是否仍然只是通用字段名，而不是封装名称。"""

    text = _normalize_text(value)
    return text in {
        "",
        "pin",
        "pin no",
        "pin number",
        "ball",
        "ball no",
        "ball number",
        "terminal",
        "terminal no",
        "terminal number",
        "number",
        "no",
    }


def _is_structural_group_row(row: Sequence[str]) -> bool:
    """跳过只有一个重复文本的结构标题行，不把标题当成物理引脚。"""

    values = [str(cell).strip() for cell in row if str(cell).strip()]
    # 完全空白行是排版占位，也必须在绑定前跳过；有其他字段内容但
    # pin_no 为空的行不在这里跳过，后续会保留为 pin_no=""。
    if not values:
        return True
    if not (len(values) == 1 or len(set(values)) == 1):
        return False
    # 只有 pin_no 的 Reserved 行仍是合法数据，不能因为其他字段为空就跳过。
    return not _looks_like_pin_value(values[0])


def _looks_like_pin_value(value: str) -> bool:
    """保守识别数字、BGA 坐标及项目支持的两类范围编号。"""

    value = str(value).strip()
    token_pattern = (
        r"(?:\d{1,5}|[A-Za-z]+\d+|"
        r"[A-Za-z]+\d+\s*-\s*[A-Za-z]+\d+|"
        r"[A-Za-z]+\s*\[\s*\d+\s*:\s*\d+\s*\])"
    )
    return bool(re.fullmatch(rf"{token_pattern}(?:\s*[,，;/／、|]\s*{token_pattern})*", value))


def _common_prefix_length(token_rows: Sequence[Sequence[str]]) -> int:
    """计算多个完整表头共有的 token 前缀长度。"""

    if not token_rows or any(not tokens for tokens in token_rows):
        return 0
    limit = min(len(tokens) for tokens in token_rows)
    length = 0
    while length < limit:
        values = {_normalize_text(tokens[length]) for tokens in token_rows}
        if len(values) != 1:
            break
        # 每个表头至少保留一个独有 token，不能把整列名称全部删掉。
        if any(length + 1 >= len(tokens) for tokens in token_rows):
            break
        length += 1
    return length


def _header_tokens(value: str) -> list[str]:
    """把表头拆成可比较 token，同时保留封装代码中的连字符。"""

    return re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u4e00-\u9fff]+", value)


def _clean_header_text(value: str) -> str:
    """清理表头脚注和空白，不改变封装名称主体。"""

    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\[[^]]+\]", " ", value)
    value = re.sub(r"[†‡*]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_text(value: str) -> str:
    """生成仅用于比较的规范化文本。"""

    value = _clean_header_text(value).lower()
    value = re.sub(r"[_\-/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _cell(row: Sequence[str], index: int | None) -> str:
    """安全读取单元格，越界或无列映射时返回空字符串。"""

    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()
