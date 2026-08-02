"""在模型调用前排除“寄存器字段 + 相关引脚引用”表。

这类表的主轴是寄存器位、字段、访问权限和默认值，``Relevant Pins`` 等列
只是说明寄存器会影响哪些引脚，不能把该辅助列当成物理引脚清单。

过滤必须同时满足以下条件：

1. 表头或表题具有明确的寄存器结构证据；
2. 引脚只出现在 ``Relevant/Associated/Affected Pins`` 等辅助引用列中；
3. 表中不存在 ``Pin No.``、``Ball Number`` 等直接物理编号轴。

只在 Description 中提到 register 的普通引脚表不会命中本模块。
"""

from __future__ import annotations

import re
from collections.abc import Sequence


# 寄存器表的核心字段。使用字段组计数，避免一个长表头重复出现同一个词就误判。
REGISTER_SCHEMA_GROUPS = (
    ("register", "register name", "register description", "register field"),
    ("offset", "offset address", "register offset"),
    ("address", "register address"),
    ("bit", "bits", "bit field", "bit position", "bit range"),
    ("field", "field name"),
    ("access", "access type", "r/w", "read/write"),
    ("reset", "reset value", "reset state"),
    ("default", "default value"),
)

# 这些列只引用受影响的物理引脚，不是表格的主编号轴。
AUXILIARY_PIN_REFERENCE_PATTERNS = (
    r"\brelevant\s+(?:pin|pins|ball|balls|terminal|terminals)\b",
    r"\bassociated\s+(?:pin|pins|ball|balls|terminal|terminals)\b",
    r"\baffected\s+(?:pin|pins|ball|balls|terminal|terminals)\b",
    r"\brelated\s+(?:pin|pins|ball|balls|terminal|terminals)\b",
    r"\bapplicable\s+(?:pin|pins|ball|balls|terminal|terminals)\b",
    r"相关引脚|关联引脚|受影响引脚|适用引脚",
)

# 存在这些字段时，表格本身具有直接的物理引脚编号轴，不能按引用表过滤。
DIRECT_PHYSICAL_AXIS_PATTERNS = (
    r"\bpin\s*(?:no\.?|number|numbers|#)\b",
    r"\bball\s*(?:no\.?|number|numbers|#)\b",
    r"\bterminal\s*(?:no\.?|number|numbers|#)\b",
    r"引脚编号|球号|焊球编号|端子编号",
)

REGISTER_TITLE_PATTERN = re.compile(
    r"\b(?:register|registers|register map|bit description|word\s+\d+)\b|寄存器",
    re.IGNORECASE,
)


def is_register_pin_reference_table(
    title: str,
    headers: Sequence[str],
) -> bool:
    """判断候选表是否只是带引脚引用列的寄存器说明表。

    ``headers`` 必须是当前解析阶段已经识别出的列表头；函数不读取 Description
    单元格，因此正文中出现 register、bit 等词不会改变判断结果。
    """

    normalized_headers = [_normalize_text(header) for header in headers]
    normalized_title = _normalize_text(title)

    # 先确认存在“相关引脚”类辅助列，否则普通寄存器表交给原候选逻辑处理。
    has_auxiliary_pin_reference = any(
        _matches_any(header, AUXILIARY_PIN_REFERENCE_PATTERNS)
        for header in normalized_headers
    )
    if not has_auxiliary_pin_reference:
        return False

    # 直接 Pin No./Ball Number 轴优先级更高，避免过滤真正的引脚关系表。
    if any(
        _matches_any(header, DIRECT_PHYSICAL_AXIS_PATTERNS)
        for header in normalized_headers
    ):
        return False

    register_schema_score = sum(
        any(_contains_header_token(header, token) for header in normalized_headers for token in group)
        for group in REGISTER_SCHEMA_GROUPS
    )
    title_is_register_or_word = bool(REGISTER_TITLE_PATTERN.search(normalized_title))

    # 表题明确时仍要求至少一个寄存器字段；表题不明确时要求三个字段组共同证明。
    return (title_is_register_or_word and register_schema_score >= 1) or register_schema_score >= 3


def _normalize_text(value: object) -> str:
    """统一大小写和空白，保留 ``/``、``#`` 等字段符号用于角色判断。"""

    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _matches_any(value: str, patterns: Sequence[str]) -> bool:
    """检查一个完整表头是否命中任一语义模式。"""

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def _contains_header_token(header: str, token: str) -> bool:
    """按完整英文词或连续中文词匹配寄存器字段，避免子串误命中。"""

    if re.search(r"[\u4e00-\u9fff]", token):
        return token in header
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", header))
