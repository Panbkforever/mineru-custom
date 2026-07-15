"""处理同一表格行中按换行一一对应的 pin_no 与 pin_name。

这个模块只处理一种结构问题：MinerU 输出的 HTML 单元格中，``<br>``
可能不是普通排版换行，而是多个引脚值之间的明确边界。例如：

``P18<br>N16`` 与 ``MDINT_0<br>MDINT_1`` 应按位置配成两条记录。

处理原则：

* HTML 读取阶段保留显式 ``<br>``，不把它提前变成普通空格。
* 只有 pin_no 和 pin_name 都具有相同数量的显式换行项时才按位置配对。
* 普通空格永远不是 pin_name 分隔符，避免拆坏 ``Power Supply`` 等名称。
* 如果没有可确认的换行对应关系，继续兼容逗号、分号、斜杠分隔规则。
* 本模块不判断表格和字段，不生成最终 JSON，也不处理 type/description。
"""

from __future__ import annotations

import html as html_lib
import re


# 先把真正的 HTML <br> 替换为内部标记。原始 HTML 源码中的普通换行
# 只是代码排版，不能被误认为单元格内的结构换行。
_BR_RE = re.compile(r"<br\b[^>]*>", flags=re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_MARKER = "\ue000"


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


def split_parallel_pin_names(
    pin_name_value: str,
    pin_no_value: str,
    expected_count: int,
) -> list[str]:
    """在能够确认一一对应关系时拆分 pin_name。

    ``expected_count`` 是 pin_no 按项目规则拆分后的最终数量。函数首先
    检查 pin_no 与 pin_name 是否都由同样数量的显式 ``<br>`` 项组成；
    只有三者数量一致时才按行配对。否则退回原有的显式标点分隔规则。
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

    # 没有确认换行对应关系时，把残留换行作为普通空格，再兼容此前已经
    # 明确允许的逗号、分号和斜杠。普通空格本身不参与名称拆分。
    collapsed_name = re.sub(r"\s+", " ", str(pin_name_value)).strip()
    parts = [
        part.strip()
        for part in re.split(r"[,，;；/／]+", collapsed_name)
        if part.strip()
    ]
    return parts if len(parts) == expected_count else []
