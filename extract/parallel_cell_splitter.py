"""处理同一表格行中按换行和内嵌范围对应的 pin_no 与 pin_name。

这个模块只处理一种结构问题：MinerU 输出的 HTML 单元格中，``<br>``
可能不是普通排版换行，而是多个引脚值之间的明确边界。例如：

``P18<br>N16`` 与 ``MDINT_0<br>MDINT_1`` 应按位置配成两条记录。

当名称中包含数字总线范围时，先保留 ``<br>`` 分组，再在组内展开范围。例如：

``A1,A2,A3<br>B1,B2,B3`` 与 ``LED[2:0]_A<br>LED[0:2]_B``
应依次配成 ``LED2_A`` 至 ``LED0_A``、``LED0_B`` 至 ``LED2_B``。

处理原则：

* HTML 读取阶段保留显式 ``<br>``，不把它提前变成普通空格。
* pin_no 和 pin_name 的显式换行分组数相同时，允许在每组内部继续展开名称范围。
* 名称范围支持升序和降序，保留范围前后的完整文本，不写死 LED 等业务名称。
* 每组展开后的 pin_no 与 pin_name 数量必须相等，最终总数也必须相等。
* 任意一组数量不一致时放弃范围结果，不能猜测、广播或循环填充名称。
* 普通空格永远不是 pin_name 分隔符，避免拆坏 ``Power Supply`` 等名称。
* 如果没有可确认的换行对应关系，继续兼容逗号、分号、斜杠分隔规则。
* 本模块不判断表格和字段，不生成最终 JSON，也不处理 type/description。
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Callable


# 先把真正的 HTML <br> 替换为内部标记。原始 HTML 源码中的普通换行
# 只是代码排版，不能被误认为单元格内的结构换行。
_BR_RE = re.compile(r"<br\b[^>]*>", flags=re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_MARKER = "\ue000"
_EMBEDDED_NUMERIC_RANGE_RE = re.compile(
    r"^(?P<prefix>.*?)\[\s*(?P<start>\d+)\s*:\s*(?P<end>\d+)\s*\](?P<suffix>.*)$"
)
_NUMERIC_RANGE_TOKEN_RE = re.compile(r"\[\s*\d+\s*:\s*\d+\s*\]")


# 主提取器已经集中实现 pin_no 的列表和范围规则。通过回调复用该函数，
# 本模块不再复制一套编号拆分规则，避免两个入口后续产生不一致。
PinNumberSplitter = Callable[[str], list[str]]


def parse_html_cell_text(value: str) -> str:
    """将 HTML 单元格转成文本，同时保留显式 ``<br>`` 的边界。

    返回值使用 ``\n`` 表示原始 HTML 中的 ``<br>``。标签之间因为 HTML
    排版产生的普通空白仍会被折叠，因此只有明确的 ``<br>`` 能进入后续
    的并行值配对逻辑。
    """

    marked = _BR_RE.sub(_BR_MARKER, str(value or ""))
    unescaped = html_lib.unescape(_TAG_RE.sub("", marked))

    # 每个 <br> 分段独立清理空白，不能在这里调用会把换行折叠掉的
    # plain_text()。空分段不代表一个有效引脚值，因此不保留。
    parts = [re.sub(r"\s+", " ", part).strip() for part in unescaped.split(_BR_MARKER)]
    return "\n".join(part for part in parts if part)


def split_explicit_cell_lines(value: str) -> list[str]:
    """只按已经保留下来的显式 ``<br>`` 边界拆分单元格。"""

    if "\n" not in str(value or ""):
        return []
    return [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"\r?\n+", str(value))
        if re.sub(r"\s+", " ", part).strip()
    ]


def expand_embedded_pin_name_range(value: str) -> list[str]:
    """展开 pin_name 中唯一的数字方括号范围，未命中时返回原值。

    范围可以位于名称中间，并允许升序或降序。例如
    ``LED[4:0]_3`` 展开为 ``LED4_3`` 至 ``LED0_3``。函数只处理一个
    明确的 ``[数字:数字]``；其他括号表达式保持原样。
    """

    text = str(value or "").strip()
    if not text:
        return []

    # 同一名称出现多个范围时对应关系不唯一，禁止只展开其中一个。
    if len(_NUMERIC_RANGE_TOKEN_RE.findall(text)) != 1:
        return [text]

    match = _EMBEDDED_NUMERIC_RANGE_RE.fullmatch(text)
    if not match:
        return [text]

    start_text = match.group("start")
    end_text = match.group("end")
    start = int(start_text)
    end = int(end_text)
    if abs(end - start) > 1000:
        return [text]

    # 端点带前导零时保留原宽度，保证名称展开不会改变编号格式。
    width = max(len(start_text), len(end_text))
    preserve_width = start_text.startswith("0") or end_text.startswith("0")
    step = 1 if end >= start else -1
    prefix = match.group("prefix")
    suffix = match.group("suffix")
    return [
        f"{prefix}{str(number).zfill(width) if preserve_width else number}{suffix}"
        for number in range(start, end + step, step)
    ]


def _split_grouped_parallel_values(
    pin_no_value: str,
    pin_name_value: str,
    pin_no_lines: list[str],
    pin_name_lines: list[str],
    expected_count: int,
    split_pin_numbers: PinNumberSplitter,
) -> list[str]:
    """按 ``<br>`` 分组展开并校验 pin_no/pin_name 的位置关系。"""

    # 没有 <br> 时，整个单元格本身就是一个分组。这样同一行只有一个
    # ``GPIO[0:2]_A`` 范围时也能使用相同流程，不需要另设特殊分支。
    if not pin_no_lines:
        pin_no_text = re.sub(r"\s+", " ", str(pin_no_value or "")).strip()
        pin_no_lines = [pin_no_text] if pin_no_text else []
    if not pin_name_lines:
        pin_name_text = re.sub(r"\s+", " ", str(pin_name_value or "")).strip()
        pin_name_lines = [pin_name_text] if pin_name_text else []

    if not pin_no_lines or len(pin_no_lines) != len(pin_name_lines):
        return []

    expanded_names: list[str] = []
    expanded_pin_count = 0
    for pin_no_line, pin_name_line in zip(pin_no_lines, pin_name_lines):
        # pin_no 继续使用项目统一拆分规则；pin_name 只展开明确的内嵌范围。
        group_pin_numbers = split_pin_numbers(pin_no_line)
        group_pin_names = expand_embedded_pin_name_range(pin_name_line)
        if not group_pin_numbers or len(group_pin_numbers) != len(group_pin_names):
            return []
        expanded_pin_count += len(group_pin_numbers)
        expanded_names.extend(group_pin_names)

    # 分组内部都相等仍不够，最终数量还必须与行提取实际得到的 pin_no 一致。
    if expanded_pin_count != expected_count or len(expanded_names) != expected_count:
        return []
    return expanded_names


def split_parallel_pin_names(
    pin_name_value: str,
    pin_no_value: str,
    expected_count: int,
    *,
    split_pin_numbers: PinNumberSplitter | None = None,
) -> list[str]:
    """在能够确认一一对应关系时拆分 pin_name。

    ``expected_count`` 是 pin_no 按项目规则拆分后的最终数量。函数首先
    检查 pin_no 与 pin_name 是否能直接按显式 ``<br>`` 一一对应；如果
    ``<br>`` 只是分组边界，则调用统一 pin_no 拆分器，并展开每组 pin_name
    中的数字范围。两种方式都要求最终数量严格等于 ``expected_count``。
    无法确认对应关系时退回原有的显式标点分隔规则。
    """

    if expected_count <= 1 or not str(pin_name_value or "").strip():
        return []

    pin_no_lines = split_explicit_cell_lines(pin_no_value)
    pin_name_lines = split_explicit_cell_lines(pin_name_value)
    if (
        len(pin_no_lines) == expected_count
        and len(pin_name_lines) == expected_count
    ):
        return pin_name_lines

    # 直接一一对应失败后，尝试“换行分组 + 组内范围”结构。只有调用方
    # 提供统一 pin_no 拆分器时启用，防止本模块自行解释编号语法。
    if split_pin_numbers is not None:
        grouped_names = _split_grouped_parallel_values(
            pin_no_value,
            pin_name_value,
            pin_no_lines,
            pin_name_lines,
            expected_count,
            split_pin_numbers,
        )
        if grouped_names:
            return grouped_names

    # 没有确认换行对应关系时，把残留换行作为普通空格，再兼容此前已经
    # 明确允许的逗号、分号和斜杠。普通空格本身不参与名称拆分。
    collapsed_name = re.sub(r"\s+", " ", str(pin_name_value)).strip()
    parts = [
        part.strip()
        for part in re.split(r"[,，;；/／]+", collapsed_name)
        if part.strip()
    ]
    return parts if len(parts) == expected_count else []
