"""识别可以绕过模型、直接保留或直接过滤的特殊表格。

本模块只处理格式和语义都高度确定的特殊情况。每一种特殊表格必须使用一个
独立函数，未完全满足该函数全部条件时必须返回 ``None``，继续交给模型判断。

当前特殊情况：

* ``unused_pins_connection_table_filter``：处理
  ``Connections for Unused Pins`` / ``Connection for Unused Pins and Modules``
  这类未使用引脚连接建议表；整表直接过滤，禁止把板级连接建议并入正式
  物理 pin list。
* ``mii_rmii_rgmii_pin_mux_table_filter``：处理表头固定为 ``Pin No.``、
  ``MII MAC``、``MII PHY``、``RMII MAC``、``RMII PHY``、``RGMII`` 的
  以太网接口复用矩阵；整表直接过滤，禁止把编号列误当成独立引脚清单。
* ``supplemental_characteristics_table_filter``：处理
  ``Multiplexing Characteristics`` 和 ``Power Supplies Description`` 这类
  复用/电源补充说明表；整表直接过滤，禁止污染已经存在的主 pin list。
* ``reserved_pin_table_handler``：处理只有物理引脚/球号和连接要求的
  Reserved/NC 表。只保留明确要求悬空或禁止连接的行；明确说明封装中不存在
  的位置不作为物理引脚输出。
* ``register_word_pin_affected_table_filter``：处理以 Word/位字段为主轴、
  ``PIN AFFECTED`` 仅表示受寄存器影响引脚的特殊寄存器表；整表直接过滤，
  禁止把辅助引用列当成物理引脚清单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SpecialColumnMapping:
    """特殊表命中后提供给统一提取流程的列映射。"""

    index: int
    header: str
    field_name: str


@dataclass(frozen=True)
class SpecialTableMatch:
    """特殊表的确定性判断结果，不包含最终 pin 记录。

    ``should_extract=False`` 表示该专用规则确认整张表必须过滤；此时
    ``columns`` 和 ``included_row_indexes`` 都为空，也不会进入模型判断。
    """

    handler_name: str
    columns: tuple[SpecialColumnMapping, ...]
    included_row_indexes: frozenset[int]
    should_extract: bool = True


def find_special_table_match(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """依次调用每一种特殊处理函数，返回第一个完整命中的结果。"""

    handlers = (
        unused_pins_connection_table_filter,
        mii_rmii_rgmii_pin_mux_table_filter,
        supplemental_characteristics_table_filter,
        register_word_pin_affected_table_filter,
        reserved_pin_table_handler,
    )
    for handler in handlers:
        match = handler(title, headers, data_rows)
        if match is not None:
            return match
    return None


def unused_pins_connection_table_filter(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """过滤未使用引脚连接建议表。

    ``Connections for Unused Pins`` 这类表描述的是未用引脚在板级设计中如何
    连接，不是器件完整物理引脚定义。它经常有 Pin/Name/Connection 表头，
    容易被字段模型误判为正式 pin table，因此在模型前直接拒绝。

    该规则只依赖明确表题，不靠表头猜测，避免误伤普通引脚功能表。
    """

    normalized_title = normalize_text(title)
    if not normalized_title:
        return None
    if re.search(
        r"\bconnections?\s+for\s+unused\s+pins?(?:\s+and\s+modules)?\b",
        normalized_title,
    ) or re.search(
        r"\bunused\s+pins?(?:\s+and\s+modules)?\s+connections?\b",
        normalized_title,
    ):
        return SpecialTableMatch(
            handler_name="unused_pins_connection_table_filter",
            columns=(),
            included_row_indexes=frozenset(),
            should_extract=False,
        )
    return None


def supplemental_characteristics_table_filter(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """过滤复用/电源补充说明表。

    ``Multiplexing Characteristics`` 和 ``Power Supplies Description`` 描述
    的是功能复用或电源连接说明，不是完整物理封装 pin list。它们经常带有
    Ball/Pin 相关列，容易被字段模型误判成可抽表，因此在模型前直接拒绝。

    该规则只依赖明确表题短语，不使用表头或行内容扩展判断范围，避免误伤
    普通 ``Pin Functions`` 或 ``Signal Descriptions – XXX Package`` 主表。
    """

    normalized_title = normalize_text(title)
    if not normalized_title:
        return None
    if (
        re.search(r"\bmultiplexing\s+characteristics\b", normalized_title)
        or re.search(r"\bpower\s+supplies\s+description\b", normalized_title)
    ):
        return SpecialTableMatch(
            handler_name="supplemental_characteristics_table_filter",
            columns=(),
            included_row_indexes=frozenset(),
            should_extract=False,
        )
    return None


def mii_rmii_rgmii_pin_mux_table_filter(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """过滤固定六列表头的 MII/RMII/RGMII 引脚复用矩阵。

    这种表以 ``Pin No.`` 为行主轴，其余五列描述同一物理引脚在不同以太网
    工作模式下的复用功能，不是“每行一个编号、名称和类型”的物理引脚定义表。

    该规则只按完整表头结构命中，不依赖中文或英文表题。必须同时满足：

    1. 恰好有六个非空表头；
    2. 第一列是明确的 Pin/Ball/Terminal 物理编号表头；
    3. 后五列按顺序严格为 MII MAC、MII PHY、RMII MAC、RMII PHY、RGMII。

    少列、多列、改序或普通 ``Pin No.`` 引脚表都不会命中，继续走原流程。
    """

    normalized_headers = [
        normalize_header_text(header)
        for header in headers
        if normalize_header_text(header)
    ]
    if len(normalized_headers) != 6:
        return None
    if not is_direct_physical_number_header(normalized_headers[0]):
        return None
    if normalized_headers[1:] != [
        "mii mac",
        "mii phy",
        "rmii mac",
        "rmii phy",
        "rgmii",
    ]:
        return None

    return SpecialTableMatch(
        handler_name="mii_rmii_rgmii_pin_mux_table_filter",
        columns=(),
        included_row_indexes=frozenset(),
        should_extract=False,
    )


def register_word_pin_affected_table_filter(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """过滤 ``Word`` 位字段表中的 ``PIN AFFECTED`` 辅助引用列。

    该规则有意保持严格，必须同时满足：

    1. 表题明确为 ``Table n. Word n`` 或等价的独立 ``Word n`` 表题；
    2. 表头同时具有 ``BIT``、``BIT NAME``、``DESCRIPTION/FUNCTION``；
    3. 表头具有 ``PIN AFFECTED``，但没有直接 ``PIN NO.``/``BALL NUMBER`` 轴；
    4. 至少存在一行非空数据，避免只凭空表头作出决定。

    命中后整表拒绝，不返回列映射，也不发送给字段判断模型。
    """

    normalized_title = normalize_text(title)
    if not re.search(r"(?:^|\b)table\s+\d+(?:[.-]\d+)?\s*[.:]?\s*word\s+\d+\b", normalized_title):
        if not re.fullmatch(r"word\s+\d+", normalized_title):
            return None

    normalized_headers = [normalize_header_text(header) for header in headers]
    header_set = {header for header in normalized_headers if header}

    required_headers = {"bit", "bit name", "description / function", "pin affected"}
    if not required_headers.issubset(header_set):
        return None

    # 真正的物理编号主轴优先，防止误过滤同时描述寄存器控制信息的引脚表。
    if any(is_direct_physical_number_header(header) for header in normalized_headers):
        return None

    if not any(any(cell_value(row, index) for index in range(len(row))) for row in data_rows):
        return None

    return SpecialTableMatch(
        handler_name="register_word_pin_affected_table_filter",
        columns=(),
        included_row_indexes=frozenset(),
        should_extract=False,
    )


def reserved_pin_table_handler(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """识别 Reserved/NC 引脚表，并选出其中真实存在的 Reserved 行。

    必须同时满足以下条件才直接保留：

    1. 标题明确同时表达 Reserved 和 Ball/Pin/Terminal。
    2. 有明确的物理编号列和 Connection Requirements 列。
    3. 没有独立的 pin/ball/signal/terminal name 列。
    4. 每个非空数据行都能明确归类为 Reserved 行或“封装中不存在”行。
    5. 至少存在一行 Reserved，且该行编号列符合物理引脚列表形态。

    返回的行号相对于 ``data_rows``。调用方仍使用统一行提取逻辑，因此
    pin_name 缺失时会由既有规则填充为 ``Reserved``。
    """

    normalized_title = normalize_text(title)
    has_reserved_subject = (
        "reserved" in normalized_title
        and any(word in normalized_title for word in ("ball", "pin", "terminal"))
    )
    if not has_reserved_subject:
        return None

    normalized_headers = [normalize_header_text(header) for header in headers]
    if any(is_name_header(header) for header in normalized_headers):
        return None

    pin_index = find_pin_number_column(normalized_headers)
    requirement_index = find_connection_requirement_column(normalized_headers)
    if pin_index is None or requirement_index is None:
        return None

    included_rows: set[int] = set()
    classified_row_count = 0
    for row_index, row in enumerate(data_rows):
        pin_value = cell_value(row, pin_index)
        requirement = normalize_text(cell_value(row, requirement_index))
        if not pin_value and not requirement:
            continue

        # “封装中不存在”优先于 Reserved 关键词，防止混合描述被误保留。
        if is_nonexistent_package_position(requirement):
            classified_row_count += 1
            continue
        if is_reserved_connection_requirement(requirement) and looks_like_pin_list(pin_value):
            included_rows.add(row_index)
            classified_row_count += 1
            continue

        # 出现任何无法确定语义的行，整张表都不走特殊通道，交回模型。
        return None

    if not included_rows or classified_row_count == 0:
        return None

    return SpecialTableMatch(
        handler_name="reserved_pin_table_handler",
        columns=(SpecialColumnMapping(pin_index, headers[pin_index], "pin_no"),),
        included_row_indexes=frozenset(included_rows),
    )


def find_pin_number_column(headers: list[str]) -> int | None:
    """寻找明确表示物理 Pin/Ball/Terminal 编号的列。"""

    exact_headers = {
        "balls",
        "ball numbers",
        "pins",
        "pin numbers",
        "terminals",
        "terminal numbers",
        "球号",
        "引脚编号",
        "端子编号",
    }
    for index, header in enumerate(headers):
        if header in exact_headers:
            return index
    return None


def find_connection_requirement_column(headers: list[str]) -> int | None:
    """寻找用于判定 Reserved/不存在语义的连接要求列。"""

    for index, header in enumerate(headers):
        if "connection requirement" in header or "连接要求" in header:
            return index
    return None


def is_name_header(header: str) -> bool:
    """判断表中是否已经存在独立的引脚或信号名称列。"""

    return any(
        phrase in header
        for phrase in (
            "pin name",
            "ball name",
            "signal name",
            "terminal name",
            "引脚名称",
            "信号名称",
            "端子名称",
        )
    )


def is_direct_physical_number_header(header: str) -> bool:
    """识别真正作为表格主轴的 Pin/Ball/Terminal 编号表头。"""

    return bool(
        re.fullmatch(
            r"(?:pin|ball|terminal)\s*(?:no\.?|number|numbers|#)",
            header,
        )
        or header in {"引脚编号", "球号", "焊球编号", "端子编号"}
    )


def is_reserved_connection_requirement(value: str) -> bool:
    """判断连接要求是否明确表示 Reserved、NC 或必须悬空。"""

    return bool(
        re.search(r"\breserved\b", value)
        or re.search(r"\b(?:no[ -]?connect|n/?c)\b", value)
        or "left unconnected" in value
        or "leave unconnected" in value
        or "must not be connected" in value
        or "shall not be connected" in value
        or "do not connect" in value
    )


def is_nonexistent_package_position(value: str) -> bool:
    """判断编号是否明确表示封装中不存在，而不是真实 Reserved 引脚。"""

    return any(
        phrase in value
        for phrase in (
            "do not exist on the package",
            "does not exist on the package",
            "not exist on the package",
            "not present on the package",
            "not available on the package",
            "no ball exists",
            "no pin exists",
        )
    )


def looks_like_pin_list(value: str) -> bool:
    """保守确认单元格由一个或多个物理引脚/球号组成。"""

    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not value:
        return False
    tokens = [token for token in re.split(r"[\s,，;/／、|]+", value) if token]
    return bool(tokens) and all(
        re.fullmatch(r"(?:[A-Za-z]{1,3}\d{1,4}[A-Za-z]?|\d{1,4})", token)
        for token in tokens
    )


def cell_value(row: list[str], index: int) -> str:
    """安全读取指定列。"""

    return str(row[index]).strip() if index < len(row) else ""


def normalize_text(value: str) -> str:
    """生成供特殊规则比较的规范化小写文本。"""

    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def normalize_header_text(value: str) -> str:
    """规范化表头并移除脚注编号，避免 ``BALLS [1]`` 漏判。"""

    value = re.sub(r"\[[^]]+\]", " ", str(value or ""))
    return normalize_text(value)
